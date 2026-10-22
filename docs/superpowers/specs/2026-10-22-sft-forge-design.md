# SFT Forge — 基于文件名真值的多轮纠错 SFT Label 生成器

日期：2026-10-22 · 分支：dev · 状态：待评审

## 1. 背景与目标

用远程部署的 qwen VL 模型（复用 `traffic_analyzer/config/.env` 现有 provider 配置）对**视频本体**直接推理，生成符合训练格式要求的 SFT label，用于后续 GRPO 强化学习 9b 模型。

核心诉求：**提高推理思维链的准确性**。正向推理后与文件名标注真值对比，对"真值存在但推理未检出"的事件做定向重推（告知该事件确实存在，要求重新观察并输出该事件段落）；对"推理检出但真值不存在"的事件做定向复核（误报则剔除）。多轮合并得到最终修正 SFT label。全程轮次 trace 可在 Web 面板可视化。

## 2. 已确认的需求决策

| 决策点 | 结论 |
|---|---|
| 模型接入 | 复用现有 VLM 配置（`VLMInferenceEngine` 的 provider 配置体系） |
| 模型输入 | **直接传视频**（不抽帧、不传图片），第一版仅此一种输入 |
| 交付形态 | 独立批处理脚本 + Web 面板（右上角独立入口，可视化纠错轮次） |
| 纠错机制 | 按事件定向重推（方案 A：无状态重推 + 代码级段落合并） |
| 纠错方向 | 双向：漏检补段落，误报复核剔除；最终结论集合与真值一致 |
| 真值来源 | 文件名前缀解析（`01-02-04_Event_...` → `{1,2,4}`）；`09` 前缀（正常）视频跳过 |
| 规模 | 第一版小批量验证（单视频/小目录），架构支持断点续跑 |
| 输出格式 | 训练 JSONL，与 `/media/wanji/Elements/大模型应用/高速交通事件Agent测试视频V4/sft_grpo_train_label/lora27b_v15_mnt4096.jsonl` 一致：`{index, video, output, gt_classes, gt_class_names}`，`output` = `<think>…</think><answer>…</answer>` |

## 3. 关键技术依据（来自代码调研）

- 真值解析现成：`traffic_analyzer/evaluation/__init__.py` `extract_gt_from_filename()`。
- 事件中文名：`traffic_analyzer/config/event_categories.yaml` 的 `name_zh`（1 违法停车、2 应急车道占用、3 交通事故、4 高速公路行人出现、5 摩托车出现、6 拥堵、7 道路施工、8 车辆逆行/倒车、10 抛洒物、11 实线变道）。
- 视频直传模式已验证：`traffic_analyzer/rag/embed_client.py` 以 `{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,…"}}` 调远程 OpenAI 兼容端点；`scripts/probe_wemm_endpoint.py` 探测过该模式。
- 现有 GT 仅用于评估指标，无"真值驱动纠错重生成"链路——本功能是新链路，不动现有 analyze 管线。

## 4. 架构

### 4.1 核心包 `traffic_analyzer/sftforge/`（纯 Python，可单测）

| 模块 | 职责 |
|---|---|
| `models.py` | Pydantic：`RoundTrace`（round_no、kind: forward/correct_missing/review_extra、event_ids、prompt、response、patched）、`VideoForgeResult`（video、gt、final_output、rounds、match、skipped/quarantine 原因） |
| `gt.py` | 真值解析（复用 `extract_gt_from_filename`）+ `diff_predictions(pred, gt) -> (missing, extra)` |
| `parser.py` | 纯函数：解析 `<think>`/`<answer>`；think 按事件标题切分段落；answer 提取 `classN` 列表与天气/时间/场景字段 |
| `merger.py` | 纯函数：段落级替换/插入/删除；按修正后事件集合重建 `<answer>` class 列表 |
| `engine.py` | `SftForgeEngine.run(video_path)` 编排：forward → diff →（missing 逐事件 `correct_missing`、extra 逐事件 `review_extra`）→ merge → 重复至多 `max_rounds`（默认 2）；仍不一致则 `match=false` 并记 quarantine 原因。VLM 调用经注入的 caller（便于 fake 测试） |
| `vlm_video.py` | 视频 chat 调用：OpenAI 兼容 chat/completions，user content = `[{type: video_url, video_url: {url: data:video/mp4;base64,…}}, {type: text, …}]`；复用 provider 配置（base_url/key/model/timeout）、重试与 failover；磁盘缓存 key 含视频字节 SHA-256 |
| `exporter.py` | 训练 JSONL 行构造（`index/video/output/gt_classes/gt_class_names`）+ 每视频 trace JSON 落盘 |

### 4.2 Prompt 模板 `traffic_analyzer/config/prompts/sft_forge.yaml`

配置驱动，三个模板：
- `forward`：盲推（**不注入真值**，保证思维链真实性），要求对 10 个事件逐一给出"观察/分析/结论"或"不满足判定条件"，输出 `<think>`+`<answer>`（天气/时间/场景/最终结论 classN 列表），格式对齐现有训练样本。
- `correct_missing`：告知"经人工标注确认，本视频确实存在【事件名】，你在首轮未检出。请重新仔细观察视频，按相同格式输出该事件的完整段落（观察/分析/结论）"。
- `review_extra`：给出事件定义，告知"你首轮检出了【事件名】，请严格对照判定条件复核；若确属误报，输出否定段落"。

### 4.3 CLI `scripts/sft_forge.py`

`--video / --dir / --list-file`、`--max-rounds`（默认 2）、`--out labels.jsonl`、`--trace-dir`、`--workers`、断点续跑（按 video stem 跳过已完成）。quarantine 结果写到 `--out` 同级的 `quarantine/`。

### 4.4 Web 后端 `traffic_analyzer/web/sftforge/`

遵循现有 web 层结构（参照 jobs 包 + `realtime.py` SSE）：
- `POST /api/sftforge/jobs` `{dir 或 videos[], max_rounds}` → `job_id`，后台线程执行；
- `GET /api/sftforge/jobs/{job_id}/events`（SSE）：逐视频逐轮推送 `RoundTrace` 事件（round_started / round_completed / video_completed / job_completed）；
- `GET /api/sftforge/jobs/{job_id}/result`：最终 JSONL 与汇总统计。

### 4.5 前端 `frontend/src/`

- 右上角新增「SFT 生成」入口，独立视图/分栏；
- 轮次时间线：视频列表 → 每视频逐轮卡片（轮次号、kind、missing/extra 事件标签、prompt 折叠展示、修复后段落 diff 高亮）；
- 纪律：组合式 API、`frontend/src/styles/tokens.css` token 唯一源、禁 inline hex/emoji、Naive UI 组件；若新增 API 类型遵循现有 contracts 生成模式。

### 4.6 验证脚本 `scripts/probe_sft_forge_video.py`

M1 第一步：对现有 provider 端点发一次最小 video_url chat 请求，确认端点支持视频输入。**若不支持，立即回报用户决策**（回退方案：复用 `VideoPreprocessor` 抽帧 + 多图输入，架构上仅替换 `vlm_video.py` 内部）。

## 5. 测试

- `traffic_analyzer/tests/test_sftforge_*.py`：parser/merger/gt 纯函数单测；engine 用 fake caller 覆盖（正向全对、漏检重推补齐、误报剔除、达到 max_rounds 仍不一致 → quarantine）；
- web 层 TestClient 测试（fake engine，验证 API + SSE 事件序列）；
- 前端 vitest（遵循现有测试模式）；
- 改 YAML 后必跑 `python3 -m traffic_analyzer validate-config --config-dir ./traffic_analyzer/config`。

## 6. 非目标（第一版）

- 不做抽帧多图输入（仅作端点不支持视频时的回退预案）；
- 不动现有 analyze 管线与 `sft_label_rewrite.py`；
- 不产出 `event_attributes`/`attr_mentions` 封闭枚举字段（训练 JSONL 格式不含）；
- 不接 agent 运行时（TS 侧）。

## 7. 风险

- **端点视频输入支持未知** → §4.6 probe 先行，失败则回报用户；
- **大视频 base64 体积**（20–70MB → 30–95MB/请求）→ 超时调大；embed_client 同规模已验证可行；
- **多轮成本**：每视频 1 + missing + extra 次调用，小批量阶段可控。

## 8. Tower 拆分

- **M1 sftforge-core-engine**：`traffic_analyzer/sftforge/`、`traffic_analyzer/config/prompts/sft_forge.yaml`、`traffic_analyzer/tests/test_sftforge_*`、`scripts/sft_forge.py`、`scripts/probe_sft_forge_video.py`；
- **M2 sftforge-web-api**（deps M1）：`traffic_analyzer/web/sftforge/`、web 接线与测试；
- **M3 sftforge-frontend-panel**（deps M2）：`frontend/src/` 新面板与接线。
