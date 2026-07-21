[English](README.md) | [简体中文](README.zh-CN.md)

# 交通事件分析系统

基于多模态视觉语言模型（VLM）的高速公路监控视频事件检测框架。输入一段视频，输出一份 Markdown / JSON 分析报告和一个 **N 位二进制事件编码**（当前配置 10 位，bit i ↔ event_id i）。事件定义、Prompt 模板、裁决规则、标注规范全部通过 YAML 配置驱动，新增事件无需修改代码。

架构概要：视频预处理后，系统为每个激活事件并行运行一个独立的 **ExpertAgent**（单事件 VLM 检测，部分事件带远距离 ROI 证据增强），每个候选结果再经一次可选的**反思一致性检查**；随后由一次**裁决（Adjudication）VLM 调用**按业务规则对所有候选做跨事件裁决，最终生成报告与二进制编码。

> 当前版本：v4.0.0（见 `traffic_analyzer/__init__.py`）。唯一有执行路径的检测模式是 `expert_agent`；工具层框架保留但注册表为空。

---

## 核心特性

- **YAML 驱动的事件体系** — 事件定义、Prompt 模板、裁决规则、标注规范均在 `traffic_analyzer/config/` 下配置；`validate-config` 子命令提供加载期与交叉引用双重校验。
- **专家代理检测（ExpertAgent）** — 每个激活事件一次专用 VLM 调用，只做事实识别（看到就报），不做排除判断；所有专家通过 `ThreadPoolExecutor` 并行执行。
- **远距离 ROI 证据增强** — 在 Prompt 模板中开启 `far_object_enhancement.enabled: true` 的事件（当前 event_id 1/3/4/6）走 ROI 两阶段流程：先定位候选区域并合成放大/对比证据图，再由最终分类器判定。
- **反思一致性检查（Reflection）** — 每个专家候选默认再经一次纯文本 VLM 核查（`expert_response_reflection` 模板），纠正 `detected` 与 summary/instances 相互矛盾的结果；失败时保留原候选（fail-open），可用 `EXPERT_ENABLE_REFLECTION=false` 关闭。
- **多提供者 VLM 引擎** — 支持 anthropic / google / aliyun；每提供者独立重试 + 指数退避，限流/配额/鉴权/5xx 类错误自动故障转移到下一个提供者；所有提供者耗尽时抛出 `FatalAPIError`，分析明确中止而不是输出全零报告。
- **两层响应缓存** — 内存 LRU + SQLite 磁盘缓存（跨进程共享），按 prompt+图像内容寻址，并按 provider+model 过滤；损坏行自动清除自愈。
- **劣质视频拒绝路径** — 预过滤器（可选）与"零可用帧"检查会生成 reject report，CLI 以退出码 2 结束且不保存报告文件。
- **裁决重试与审计** — 裁决结果缺事件时最多重试 5 轮（异常专家单独重跑），仍缺失则从专家候选回填；每条排除决策记录 `rule_id` 与理由到审计日志。

---

## 快速开始

### 1. 环境要求与安装

- Python 3.10+（本仓库在 3.10 上运行通过）
- 安装依赖：

```bash
pip install -r requirements.txt
# 开发/测试再加：
pip install -r requirements-dev.txt
```

仓库根目录另有 `Dockerfile` / `docker-compose.yml`（CPU 开发环境，Python 3.11-slim + ffmpeg；compose 文件内含硬编码的主机挂载路径，使用前请按本机情况修改）。

### 2. 配置 LLM 提供者（.env）

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# 编辑 .env，填入 API Key 与模型
```

LLM 配置**只从 `.env` 文件读取**（先 `traffic_analyzer/config/.env`，兼容回退到仓库根目录 `.env`），**不读取 shell 环境变量**。最小单提供者配置：

```ini
VLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_MODEL=claude-sonnet-4-6
```

推荐配置多提供者故障转移（见下文"VLM 提供者与缓存"）。

### 3. 验证配置

```bash
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config
```

输出 `Configuration is valid.` 并以退出码 0 结束即通过。也可安装 pre-commit hook，在配置变更时自动执行该校验（`.pre-commit-config.yaml` 已内置）：

```bash
pip install pre-commit && pre-commit install
```

### 4. 运行分析

```bash
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md
```

常用选项（`python3 -m traffic_analyzer analyze --help` 查看完整列表）：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--video, -v` | 输入视频路径（必需） | - |
| `--format, -f` | 输出格式 `json` / `markdown` | `json` |
| `--output, -o` | 输出文件；省略时打印到 stdout | - |
| `--min-frames, -m` | VLM 最大输入帧数（同时设置 `SCENE_UNDERSTANDING_MIN_FRAMES` 与 `VLM_MAX_FRAMES`） | 10（由 `VLM_MAX_FRAMES` 决定） |
| `--config-dir, -d` | 配置目录 | `./traffic_analyzer/config` |
| `--scene-understanding, -s` | 外部场景理解 JSON（可选，跳过内置推断） | - |

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 分析成功 |
| 1 | 错误：视频/配置不存在、分析异常、API 全部不可用（`FatalAPIError`）等 |
| 2 | 视频被拒绝（prefilter 筛除或无可用帧），**不保存报告文件** |

### 5. Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)  # 如 "1_0_1_0_0_0_0_0_0_0"
print(report.event_results)
```

---

## 批量推理与评估（scripts/）

`scripts/analyze.sh` 与 `scripts/infer.sh` 是两条硬编码视频路径的单次分析示例，可复制后改路径使用。批量场景用以下两个脚本。

### 批量推理 `scripts/batch_infer.py`

```bash
python3 scripts/batch_infer.py \
  --video-dir ./test_videos \
  --output-dir ./output \
  --log-dir ./output/logs \
  --workers 4 \
  --format markdown
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--video-dir, -v` | 输入视频目录（必需） | - |
| `--output-dir, -o` | 报告输出目录（必需） | - |
| `--config-dir, -c` | 配置目录 | `./traffic_analyzer/config` |
| `--format, -f` | `markdown` / `json` | `markdown` |
| `--min-frames, -m` | VLM 最大输入帧数 | 10 |
| `--workers, -w` | 并行 worker 数 | 4（传 1 为串行） |
| `--log-dir, -l` | 逐视频日志目录 | - |
| `--cv-tracks-dir` | CV 轨迹 JSON 目录（`<视频名>.json`，可选） | - |
| `--force` | 已有报告也强制重跑（默认跳过） | - |
| `--api-key` | 临时覆盖 `.env` 中的 API Key | - |

### 批量评估 `scripts/batch_evaluate.py`

将推理报告与真实标签对比，输出 HTML / Markdown / JSON 评估报告：

```bash
# 真实标签来自视频文件名（默认 gt-mode=filename）
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--video-dir, -v` | 视频目录（可传多个，与 `--report-dir` 一一对应） | - |
| `--report-dir, -r` | 报告目录（`.md` / `.json`） | - |
| `--output` | 输出路径，按扩展名识别 `.html` / `.md` / `.json` | `evaluation_report.html` |
| `--gt-mode` | 真实标签来源 `filename` / `annotation_file` | `filename` |
| `--annotation-file` | 标注文件（JSON/CSV，`annotation_file` 模式必需） | - |
| `--single-class` | 只评估 `is_active=true` 的事件 | - |
| `--normal, -n` | 指标中额外输出"无事件 (normal)"类别 | - |

HTML 报告为单文件交互式页面：左侧统计与逐视频结果表（可筛选），右侧视频播放器 + Markdown 报告预览，数据全部内联，双击即可打开。

---

## 代码结构

```
traffic_analyzer/
├── cli.py                          # CLI 入口（analyze / validate-config 子命令）
├── __main__.py                     # 支持 python3 -m traffic_analyzer
├── __init__.py                     # 版本号 __version__
├── config/
│   ├── event_categories.yaml       # 事件定义（含 is_active）+ 裁决规则
│   ├── annotation_spec.yaml        # 标注规范，注入裁决 Prompt
│   ├── .env.example                # LLM/推理/缓存/prefilter 配置示例
│   └── prompts/                    # VLM Prompt 模板（Jinja2）
│       ├── common.yaml             # scene_understanding 先验 / expert_response_reflection / adjudication
│       └── event_0.yaml … event_9.yaml  # 每事件检测模板；1/3/4/6 另含 ROI 模板与 far_object_enhancement 配置
├── core/
│   ├── config_manager.py           # YAML/.env 加载、交叉校验、热重载（ConfigManager）
│   ├── video_preprocessor.py       # 视频元信息、prefilter、固定 FPS 抽帧（质量过滤+去重）
│   ├── pipeline_steps.py           # ExpertAgentLayer（并行专家层）+ AdjudicationStep（裁决，含 5 轮重试）
│   ├── expert_agent.py             # 单事件检测代理（兼容层，串联选图/模板/增强/反思）
│   ├── expert_agent_far_enhancement.py  # 远距离 ROI 证据增强流程（含应急车道专用流程、车辆语义否决）
│   ├── expert_agent_tools.py       # 工具调用执行辅助（遗留路径，当前无注册工具）
│   ├── vlm_engine.py               # VLM 统一调用：模板渲染、重试、故障转移、缓存、用量统计
│   ├── vlm_provider_clients.py     # 各提供者 payload 构造与 API 调用（anthropic/google/aliyun，aliyun 走 OpenAI 兼容协议）
│   ├── vlm_error_classifier.py     # 错误分类：可重试 / 触发故障转移 / 致命错误
│   ├── vlm_cache.py                # SQLite 磁盘缓存 + 缓存键计算（SHA-256(prompt+图像)）
│   ├── vlm_response_parser.py      # 响应 JSON 提取、修复（_repair_json）、基础 schema 校验
│   ├── vlm_exceptions.py           # VLM 异常体系（FatalAPIError、AllProvidersExhaustedError 等）
│   ├── report_generator.py         # 报告组装 + 二进制编码生成
│   ├── report_markdown_renderer.py # Markdown 报告渲染
│   ├── report_far_enhancement_renderer.py  # 增强流程证据（合成图、ROI 表格）渲染
│   └── report_text_utils.py        # 报告文本清洗工具
├── models/                         # Pydantic 数据模型
│   ├── schemas.py                  # 兼容层，统一再导出全部模型
│   ├── enums.py                    # DetectionMode、ConfidenceLevel 枚举
│   ├── video.py                    # VideoMetadata、Keyframe、KeyframeSequence
│   ├── scene.py                    # SceneInfo、RoadInfo、DirectionAnalysis 等
│   ├── event.py                    # EventCategory、EventCandidate、EventResult、AuditEntry 等
│   ├── llm.py                      # LLMResponse、LLMCallRecord、PromptTemplate 等
│   ├── report.py                   # Report、BinaryEncoding
│   ├── config.py                   # SystemConfig、LLMProviderConfig、SamplingConfig（含 env 默认值）
│   └── context.py                  # AnalysisContext（流水线共享上下文）
├── orchestrator/
│   ├── analysis_orchestrator.py    # 主编排器：元信息 → 预处理 → 专家层 → 裁决 → 报告
│   ├── video_meta_extractor.py     # 视频元信息提取
│   ├── reject_report_factory.py    # 拒绝报告（reject report）生成
│   ├── candidate_fallback.py       # 裁决失败时候选 → EventResult 回退
│   └── orchestrator_exceptions.py  # 编排器异常
├── tools/                          # 工具子系统（预留；注册表当前为空，不注册任何工具）
│   ├── tool_schema.py              # 工具定义层
│   ├── tool_router.py              # 工具路由层
│   └── tool_registry.py            # 默认 Router 工厂（TODO：零工具）
├── utils/
│   ├── event_detection.py          # 选图、专家响应解析、reflect_expert_candidate 反思检查
│   ├── emergency_lane_occupancy.py # 应急车道事件证据图生成（车道叠加、车辆红框、放大网格）
│   ├── far_non_motor_enhancer.py   # 远距离非机动车 ROI 放大合成（兼容封装层）
│   ├── roi_composite.py            # 单 ROI 增强合成图
│   ├── roi_motion.py               # ROI 运动打分与相邻帧对比图
│   ├── bbox_geometry.py            # 归一化 bbox 几何计算
│   ├── image_drawing.py            # 图像加载与底层绘制
│   ├── construction_evidence_gallery.py  # 施工事件多 ROI 证据画廊
│   ├── annotation_spec_loader.py   # annotation_spec.yaml 加载并转 Prompt 文本
│   └── tool_call_logger.py         # Tool-Call 风格日志（TRAFFIC_ANALYZER_TOOL_LOG_LEVEL 控制）
└── tests/                          # pytest 测试套件（含 tools/ 子目录）

scripts/
├── analyze.sh / infer.sh           # 单视频分析示例（硬编码路径，改后使用）
├── batch_infer.py                  # 批量推理（多进程）
└── batch_evaluate.py               # 批量评估（HTML/MD/JSON 报告）

# 仓库根目录关键文件
requirements.txt / requirements-dev.txt   # 运行 / 开发依赖
.pre-commit-config.yaml                   # pre-commit：常规检查 + validate-config 钩子
交通事件数据标注说明文档_v4.5.md            # 事件定义与标注的权威文档（annotation_spec.yaml 的源）
HTML-graph/                               # 架构图与知识图谱资源（PNG/HTML/Markdown）
Dockerfile / docker-compose.yml           # 可选 CPU 开发容器（另附 Dockerfile.cuda/gpu 与 gpu compose）
```

---

## 分析流水线

`AnalysisOrchestrator.analyze()` 的实际执行顺序：

1. **视频元信息提取**（`orchestrator/video_meta_extractor.py`）：时长、分辨率、码率等，供 prefilter 与报告使用。
2. **视频预处理**（`core/video_preprocessor.py`）：
   - **Prefilter（可选，`PREFILTER_ENABLE=true` 时启用）**：检查亮度、码率、时长（默认 5–15 秒窗口）。不合格则抛出 `VideoPrefilterError` → 生成 **reject report** → CLI 退出码 2，不保存报告。
   - **固定 FPS 抽帧**：按 `SAMPLING_FPS`（默认 1.0）均匀抽帧，再经质量阈值过滤与直方图去重；coarse 与 precision 帧集相同（运动片段检测代码保留但当前不在执行路径上）。
   - **零可用帧拒绝**：视频无法打开/解码导致一帧都取不到时，同样走 reject report（原因"视频无法打开/解码失败，无可用帧"），退出码 2。此行为保证坏视频不会产出误导性的全零报告。
3. **ExpertAgentLayer**（`core/pipeline_steps.py`）：对全部 `is_active=true` 事件用 `ThreadPoolExecutor` 并行运行 ExpertAgent。单个 ExpertAgent 的流程（`core/expert_agent.py`）：
   - 从粗采样帧中均匀抽取至多 `VLM_MAX_FRAMES` 帧；
   - 加载该事件的 Prompt 模板，并把 `scene_understanding` 模板中的场景先验规则（方向判定、应急车道识别等）注入 system prompt；
   - 若模板开启 `far_object_enhancement`（当前 event_id 1/3/4/6），走 ROI 增强流程（见下）；否则直接一次 VLM 调用并解析为 `EventCandidate`；
   - 若启用反思（默认开），用 `expert_response_reflection` 模板对候选做一次纯文本一致性核查，纠正自相矛盾的结果；核查本身失败则保留原候选（fail-open）；
   - 单个专家出错降级为 `detected=False` 候选；`FatalAPIError`（所有提供者耗尽/配额/鉴权）则中止整个分析。
4. **AdjudicationStep**（`core/pipeline_steps.py`）：一次 VLM 调用（`adjudication` 模板，带 JSON schema 约束），输入全部候选 + 关键帧 + 裁决规则 + 标注规范，输出最终 `event_results`、`reasoning_chain` 与 `audit_log`。若返回的事件集合不完整，最多重试 5 轮：候选异常的事件单独重跑对应专家，其余情况在提示中列出缺失事件后重新裁决；5 轮后仍缺失的事件直接从专家候选回填。裁决步骤整体失败时回退为"候选原样转 EventResult"。
5. **报告生成**（`core/report_generator.py`）：按 `event_results` 生成 **N 位二进制编码**（宽度 = 配置的事件总数，当前 10；未激活事件保留其 bit 但恒为 0）、最终分类文本与 Markdown/JSON 报告。

**远距离 ROI 增强流程**（`core/expert_agent_far_enhancement.py`，模板驱动）：

- **event_id=3（行人）/ 4（摩托车）**：逐帧 ROI 检测（归一化 bbox + 0–1 连续置信度 + 遮挡标志）→ 按置信度/面积/宽高比/遮挡/相邻帧运动分数排序取 top-K → 生成双图合成（单帧放大 + 相邻帧运动对比）→ 最终分类器。分类器为负但 ROI 证据充分时可安全回退为阳性。
- **event_id=1（应急车道占用）**：先用 `emergency_lane_calibration` 标定应急车道/导流区，再用 `emergency_lane_vehicle_roi` 检测车辆 ROI，生成车道叠加图、车辆红框图与放大网格等证据，最后由 `emergency_lane_occupancy_detection` 分类器判定。
- **event_id=6（道路施工）**：从中间帧提取多类施工证据区域（锥桶/人员/工程车/隔离栏/标志牌，含 `on_ground` 标志），拼成多 ROI 画廊后送最终分类器；ROI 证据满足施工作业区定义时可经施工专用回退提升为阳性。
- 增强流程生成的证据图保存在 `<报告输出目录>/tmp_img/`（未指定 `--output` 时为 `./output/tmp_img/`），Markdown 报告以相对路径引用。

---

## 配置体系

### 配置文件（`traffic_analyzer/config/`）

| 文件 | 内容 |
|---|---|
| `event_categories.yaml` | 10 个事件定义（`event_id` / `event_code` / `name_zh` / `definition` / `detection_mode` / `prompt_template_id` / `confidence_threshold` / `is_active`）+ `adjudication_rules`（当前仅 1 条：`emergency_parking_both`，应急车道静止车辆同时触发违停与占用双事件）。关闭事件请置 `is_active: false`（保留其编码位），不要整段注释。 |
| `prompts/common.yaml` | `scene_understanding`（场景先验规则，注入各专家 system prompt）、`expert_response_reflection`（反思模板）、`adjudication`（裁决模板）。 |
| `prompts/event_0.yaml` … `event_9.yaml` | 每事件检测模板；event 1/3/4/6 另含 ROI 检测模板与 `far_object_enhancement` 配置（`enabled`、`roi_template_id`、`top_k` 等）。同一 `template_id` 可有多版本，支持 A/B `traffic_percentage` 分流。 |
| `annotation_spec.yaml` | 标注规范（源自根目录《交通事件数据标注说明文档_v4.5》），注入裁决 Prompt；其 event_id 集合必须与 `event_categories.yaml` 完全一致。 |
| `.env`（由 `.env.example` 复制） | LLM 提供者、推理参数、缓存、prefilter 等。LLM 相关变量只从 `.env` 文件读取，忽略 shell 环境变量。 |

### 主要 .env 变量

单提供者（向后兼容）与多提供者（推荐，序号即优先级）两种方式；存在任一 `LLM_PROVIDER_<i>_PROVIDER` 时忽略单提供者变量。

| 变量 | 说明 | 默认值 |
|---|---|---|
| `VLM_PROVIDER` / `LLM_PROVIDER` | 提供者：`anthropic` / `google` / `aliyun` | `anthropic` |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | 通用 Key / 模型 / 自定义端点 | `claude-sonnet-4-6` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` / `ANTHROPIC_BASE_URL` | 提供者级覆盖（google/aliyun 同理：`GOOGLE_*`、`ALIYUN_*`），优先于通用变量 | - |
| `LLM_PROVIDER_<i>_PROVIDER` / `_API_KEY` / `_MODEL` / `_BASE_URL` | 第 i 个提供者（i=0 为主，依次回退） | - |
| `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` / `LLM_TIMEOUT` | 推理参数（超时单位秒） | 4096 / 0.2 / 300（示例文件为 120） |
| `LLM_MAX_RETRIES` | 每个提供者的最大重试次数 | 3 |
| `LLM_ENABLE_CACHE` / `LLM_CACHE_MAX_SIZE` | 内存缓存开关 / 容量 | true / 128 |
| `TRAFFIC_ANALYZER_DISK_CACHE` | SQLite 磁盘缓存路径（示例：`./output/.vlm_cache.db`）；不设置则关闭磁盘缓存 | - |
| `TRAFFIC_ANALYZER_DISK_CACHE_MAX_ENTRIES` | 磁盘缓存容量（超出按最久未访问淘汰） | 2000 |
| `VLM_MAX_FRAMES` | 每次 VLM 调用的最大输入帧数 | 10 |
| `EXPERT_ENABLE_REFLECTION` | 专家候选反思一致性检查 | true |
| `SAMPLING_FPS` | 抽帧帧率 | 1.0 |
| `PREFILTER_ENABLE` 及 `PREFILTER_*` | 预过滤器开关与阈值（亮度 50、码率 10000、时长 5–15 s） | 代码默认 false（示例文件为 true） |
| `PROMPT_VERSION_<TEMPLATE_ID>` | 强制指定某模板的版本 | - |
| `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL` | Tool-Call 风格日志粒度：`off` / `macro` / `mid` / `fine`（纯显示层，不影响结果） | `mid` |

### validate-config 的校验项

加载期 fail-fast：YAML 语法错误、缺失 `template_id`、重复 `event_id`、重复裁决 `rule_id`。交叉校验（`validate-config` 子命令汇总报告）：

1. `annotation_spec.yaml` 与 `event_categories.yaml` 的 event_id 集合完全一致；
2. `expert_agent` 事件的 `prompt_template_id` 存在且已定义；
3. 跨事件推断规则引用合法（当前未配置此类规则）；
4. 裁决规则 priority 在 [0, 1000]；
5. Prompt 模板 A/B 流量百分比合计有效；
6. 激活事件不得声明 tools（工具注册表为空，声明了也不会生效）；
7. event_id 必须从 0 连续（含未激活事件，保证编码位宽正确）；
8. 激活事件的 `detection_mode` 必须是 `expert_agent` —— 这是当前唯一有执行路径的模式。

---

## VLM 提供者与缓存

- **支持的提供者**：`anthropic`（Claude）、`google`（Gemini）、`aliyun`（通义千问，走 OpenAI 兼容协议）。引擎初始化时校验提供者名，不受支持直接报错。
- **重试**：每个提供者按 `LLM_MAX_RETRIES` 独立重试，指数退避 `min(2^attempt, 30)` 秒；仅对可重试错误（限流、超时、连接错误、5xx）重试。
- **故障转移**：限流/鉴权/配额/欠费/402/5xx 类错误触发切换到下一个提供者；切换是"粘性"的（后续调用从成功的提供者继续），由锁保护以支撑专家层多线程共享引擎。
- **致命错误**：所有提供者耗尽时抛 `AllProvidersExhaustedError` → `FatalAPIError`，分析立即中止（CLI 退出码 1），避免为整批视频输出全零报告。
- **两层缓存**：
  - 内存 LRU（容量 `LLM_CACHE_MAX_SIZE`）+ SQLite 磁盘缓存（`TRAFFIC_ANALYZER_DISK_CACHE`，跨进程共享，供批量推理使用）；
  - 缓存键 = SHA-256(system prompt + user prompt + 图像字节)；命中时还要求 provider 与 model 与当前配置一致，防止故障转移后读到旧提供者的结果；
  - 只缓存成功响应；磁盘行损坏或格式过旧时按未命中处理并删除该行（自愈）；
  - 可用 `LLM_ENABLE_CACHE=false` 整体关闭。

---

## 输出格式

### 二进制事件编码

编码宽度 = 配置的事件总数（当前 10），格式 `{bit_0_bit_1_..._bit_9}`，**bit i ↔ event_id i**。未激活事件保留其 bit 但恒为 0。示例：`1_0_1_0_0_0_0_0_0_0` 表示检出事件 0 与 2。

| bit | 编码 | 事件 | is_active | 远距离 ROI 增强 |
|---|---|---|---|---|
| 0 | A | 违法停车 | ✓ | – |
| 1 | B | 应急车道占用 | ✓ | ✓（车道标定 + 车辆 ROI） |
| 2 | C | 交通事故 | ✓ | – |
| 3 | D | 高速公路行人出现 | ✓ | ✓（逐帧 ROI + 双图合成） |
| 4 | E | 摩托车出现 | ✓ | ✓（逐帧 ROI + 双图合成） |
| 5 | F | 拥堵 | ✓ | – |
| 6 | G | 道路施工 | ✓ | ✓（多 ROI 证据画廊） |
| 7 | H | 车辆逆行/倒车 | ✓ | – |
| 8 | J | 抛洒物 | ✗ | – |
| 9 | K | 实线变道 | ✗ | – |

### 报告结构

Markdown 报告主要章节：视频信息 → 事件类别分析（检测总览 + 逐事件详情，含专家原始输出、检测实例、增强证据图）→ 最终分类（含编码解读）→ 裁决详情（总体推理 / 逐事件推理链 / 审计日志）→ 处置建议 → 分析统计（token 用量、耗时等）。JSON 报告为同一 `Report` 模型的完整序列化。

### 拒绝报告（reject report）

视频被 prefilter 筛除或无可用帧时，编排器返回 `rejected=true`、`reject_reason` 填明的报告对象，事件编码为全零宽度占位，且**未执行任何检测**。CLI 对该情况不写出报告文件，直接以退出码 2 结束——下游批量流程应据退出码区分"未检出事件"与"视频不可分析"。

---

## 测试

```bash
python3 -m pytest traffic_analyzer/tests -q
```

套件覆盖：配置加载与校验（`test_config_manager.py`）、CLI（`test_cli.py`）、编排器与拒绝路径（`test_orchestrator.py`）、抽帧与 prefilter（`test_video_preprocessor.py`）、专家增强流程（`test_expert_agent_far_enhancement.py`、`test_far_non_motor_enhancer.py`、`test_roi_motion.py`、`test_emergency_lane_occupancy.py`）、反思机制（`test_expert_reflection.py`）、VLM 引擎/缓存/故障转移/解析/提供者客户端（`test_vlm_*.py`）、报告生成（`test_report_generator.py`）与工具路由（`tools/test_tool_router.py`）。当前全部通过（个别用例在本机 anthropic SDK 缺少 `OverloadedError` 时按预期跳过）。

---

## 已知限制

- **仅 `expert_agent` 一种检测模式有执行路径**。`DetectionMode` 枚举中保留的 `direct_vlm` / `logic_chain` / `scene_tag` 没有任何代码执行它们；`validate-config` 会拒绝使用这些模式的激活事件。
- **工具子系统是空壳**。`tools/` 下的 schema/router/registry 为预留基础设施，注册表不注册任何工具；激活事件声明 tools 会被 `validate-config` 拒绝。
- **反思检查是 fail-open 的启发式机制**：反思调用失败或解析失败时保留原候选，可用 `EXPERT_ENABLE_REFLECTION=false` 整体关闭。
- **裁决由 VLM 执行而非硬规则**：`adjudication_rules` 只是嵌入 Prompt 的业务指导，不保证逐字执行；候选回填、实例数对齐等是启发式兜底。
- **prefilter 阈值是启发式的**（默认时长窗口 5–15 秒等），超长/超短视频需调整 `PREFILTER_*` 或关闭 prefilter。
- **LLM 配置只认 `.env` 文件**：shell 里 `export` 的同名变量不会生效（`VLM_MAX_FRAMES`、`PREFILTER_*` 等非 LLM 变量仍走进程环境）。
