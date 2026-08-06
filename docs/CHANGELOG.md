# 更新日志

## [v6.0.0] 2026-08-05 — Web 前端全面重构(Vue 3 SPA 替换原生 JS)

### 核心变更

#### 1. 前端整体重写(`frontend/`)
- **技术栈**:Vite + Vue 3 + TypeScript + Pinia + Vue Router + Naive UI,构建产物由 FastAPI 挂载于 `/`(SPA 回退,`/v2/*` 301 兼容旧书签)
- **设计系统锁定**:根目录 `design.md` + OKLCH 命名 token(生成器 `frontend/scripts/hex_to_oklch.py`),保留像素字体与 Claude 浅色系视觉 DNA;Hallmark 纪律(token 引用、8 态组件、honest copy、reduced-motion)
- **视图迁移**:文件树(懒加载/勾选/像素进度条/过滤排序)、数据看板、分析详情(视频流/逐帧降级、报告、证据编辑)、SFT 编辑器(contenteditable + chips 三层联动,与 legacy 保存字节级一致)、专家泳道面板、登录页
- **工作区弹窗双模式**:快捷选择(白名单根 + 一层子目录平铺 + 过滤 + 键盘)+ 手动浏览(面包屑 + 白名单树下拉懒加载下钻)

#### 2. 后端实时化与结构拆分
- **结构化进度契约**:推理子进程经 `TRAFFIC_ANALYZER_PROGRESS_FILE` 写 JSONL 事件,废除 stdout `[x/4]`/`EXPERT_PROGRESS` 文本解析(stdout 标记保留供 CLI)
- **SSE 总线**:`web/realtime.py`,`GET /api/events` 单连接推送 `job.progress`/`job.done`/`dashboard.changed`/`presence`,全面取代前端轮询
- **长文件拆包**:`jobs.py`→`web/jobs/`、`evidence_api.py`→`web/evidence/`、`workspace.py`→`web/workspace/`、`dashboard.py`→`web/dashboard/`(老 import/monkeypatch 路径兼容)
- **性能**:看板缓存 15s TTL→600s+主动失效、`_build_dashboard` 并发读 JSON、videos 冷路径 stat 合并;前端 rel/stem 预建 Map 消 O(N²)、浅响应式、SSE 节流、大目录分片渲染
- **legacy 删除**:`traffic_analyzer/web/static/` 整体移除(原生 JS SPA、mock 体系);`scripts/e2e_mock_test.py`/`build_mock_data.py` 废弃存档,新增 `scripts/e2e_v2_smoke.py` 冒烟

#### 3. 事件配置
- 抛洒物(10)/ 实线变道(11)激活(`is_active: true`),`event_options.yaml` 补齐实线变道属性组(方向/车辆类型)

## [v5.0.0] 2026-07-22 — Web UI 可视化工作台、证据导出与 tmp_img 目录调整

### 核心变更

#### 1. 新增 Web UI 可视化工作台
- **FastAPI 后端 + SPA 前端**：`traffic_analyzer/web/`（REST API、串行任务队列、工作区管理）+ `traffic_analyzer/web/static/`（单页应用）
- **工作区选择**：视频与分析结果统一存放在一个工作目录下，可用 `--workspace` 预选或在界面中选择
- **单视频 / 批量推理**：后台任务队列执行（子进程串行），前端实时展示任务进度
- **逐视频结果卡片**：SFT 样本详情、Markdown 报告、可视化证据编辑器
- **可视化证据编辑器**：多边形与矩形标注的顶点级编辑，保存回 `<stem>_evidence.json`
- **数据看板并入 UI**：`traffic_analyzer/web/dashboard.py`，逐行合并文件名 GT 与预测结果，展示 GT 一致性（consistent / diff / no_gt / no_results）、三态审核状态（unconfirmed / confirmed / needs_review，持久化至 `review_states.json`）与实时指标

#### 2. 新增 `web` CLI 子命令
- `python3 -m traffic_analyzer web [--host 127.0.0.1] [--port 8600] [--workspace DIR]` 启动 Web 服务（uvicorn，自动打开浏览器）
- Web 推理任务以子进程运行 `analyze --sft-label`，与 CLI 走同一分析流水线

#### 3. 可视化证据导出（evidence.json）
- 新增 `traffic_analyzer/core/evidence_exporter.py`
- `--sft-label` 开启时为每个视频导出 `<stem>_evidence.json`（schema_version 1）
- 内容：标定多边形（calibration polygons）、证据区域（evidence_regions）、证据画廊图像（gallery images），坐标归一化到 [0,1]；引用图像复制到同级 `images/` 目录，导出结果自包含
- fail-open：数据缺失时记录 WARNING 并写出可用部分，不阻断分析

#### 4. tmp_img 产物按视频分目录
- 增强流程证据图等中间产物由扁平的 `output/tmp_img/` 改为按视频组织：`output/tmp_img/<video_stem>/`

#### 5. Web 工作区结果目录约定
- 每个视频结果：`<workspace>/analysis/<video_stem>/{report.md, <video_stem>.json, <video_stem>_evidence.json, images/}`
- 审核状态：`<workspace>/analysis/review_states.json`

#### 6. batch_evaluate 报告发现修复
- `scripts/batch_evaluate.py` 报告发现支持 Web 工作区布局（`<report_dir>/<stem>/report.md` 子目录），并按视频 stem 去重，避免顶层与子目录报告重复计数

#### 7. Grounding verification（裁决后锚定核验）
- 新增 `traffic_analyzer/core/grounding_verification.py`（`grounding_check_enable` 开启）：裁决后对每个阳性事件再发一次 VLM 调用，仅用原始粗关键帧核验关键视觉元素是否可锚定；无法锚定的阳性判为幻觉并就地推翻（`grounding_overturned=True`），Prompt 见 `traffic_analyzer/config/prompts/grounding_verification.yaml`

#### 8. SFT label 模式
- 新增 `traffic_analyzer/core/sft_label_rewrite.py`（`--sft-label` 开启）：裁决后由一次额外 VLM 调用将裁决结果改写为每视频一条 SFT 训练样本（JSON），仅依据原始粗关键帧 grounding；无法在原始帧上锚定阳性事件的样本写入 `quarantine/` 子目录

#### 9. Mock 演示模式
- 前端追加 `?mock=1` 即可使用内置模拟数据进行演示（`traffic_analyzer/web/static/js/mock*.js`），无需后端与真实视频；配套 `scripts/build_mock_data.py` 与 `scripts/e2e_mock_test.py`

#### 10. 新增依赖
- `requirements.txt` 新增 `fastapi>=0.110.0,<1.0.0`、`uvicorn>=0.29.0,<1.0.0`（Web 服务）

### 关键文件变更

| 文件 | 变更 |
|---|---|
| `traffic_analyzer/web/app.py` | 新增：FastAPI 应用工厂（`create_app()`，uvicorn factory 模式） |
| `traffic_analyzer/web/jobs.py` | 新增：串行子进程任务队列（推理），逐行解析进度 |
| `traffic_analyzer/web/workspace.py` | 新增：工作区状态与视频发现 |
| `traffic_analyzer/web/evidence_api.py` | 新增：结果读取与证据编辑端点 |
| `traffic_analyzer/web/dashboard.py` | 新增：数据看板端点（GT 一致性、审核状态、实时指标） |
| `traffic_analyzer/web/frames.py` | 新增：按需视频帧提取（LRU 缓存） |
| `traffic_analyzer/web/static/` | 新增：SPA 前端（`index.html` / `js/` / `style.css`），含 `?mock=1` mock 演示模式 |
| `traffic_analyzer/cli.py` | 新增 `web` 子命令（`--host` / `--port` / `--workspace`） |
| `traffic_analyzer/core/evidence_exporter.py` | 新增：`<stem>_evidence.json` 导出（schema_version 1，归一化坐标） |
| `traffic_analyzer/core/grounding_verification.py`、`traffic_analyzer/config/prompts/grounding_verification.yaml` | 新增：裁决后锚定核验步骤（`grounding_check_enable`） |
| `traffic_analyzer/core/sft_label_rewrite.py` | 新增：SFT label 改写步骤（`--sft-label`），不可锚定样本隔离至 `quarantine/` |
| `traffic_analyzer/orchestrator/analysis_orchestrator.py`、`traffic_analyzer/models/context.py` | tmp_img 产物路径改为 `<output_dir>/tmp_img/<video_stem>/` |
| `scripts/batch_evaluate.py` | 报告发现支持工作区子目录布局（`*/report.md`）并按 stem 去重 |
| `requirements.txt` | 新增 fastapi / uvicorn |

---

## [v4.0.0] 2026-06-16 — 专家 Agent 重构、多 Provider 故障转移与事件激活扩展

### 核心变更

#### 1. event_id=4（非机动车/摩托车出现）远距离 ROI 增强量化
- **ROI confidence 从离散等级改为连续数值**: `high` / `medium` / `low` 三档改为 `0.0-1.0` 连续置信度
- **增强逻辑重构**: 专家 Agent 根据连续 confidence 阈值决定是否进行 ROI 裁剪放大与二次 VLM 判别
- **Prompt 与配置同步更新**: `traffic_analyzer/config/prompts/*.yaml` 和报告生成相关模块中相关描述统一改为 0-1 量化指标
- **二阶段运动对比图规则优化**: 相邻帧看不到目标反而支持非机动车；静止暗斑/反光点应排除

#### 2. 事件激活状态扩展
- 支持事件激活状态（`is_active`）动态控制
- 当前 **event_id 0/1/2/3/4/5/6/7** 处于激活状态，**8/9** 仍不活跃

#### 3. 专家 Agent 重构与代码拆分
- 远距离非机动车增强流程与主裁决流程解耦，逻辑更清晰
- ROI 增强结果与 adjudication 层数据对齐方式优化
- `expert_agent.py` 保留为兼容层，实际逻辑拆入 `expert_agent_far_enhancement.py`、`expert_agent_tools.py`

#### 4. VLM Engine 拆分
- `vlm_engine.py` 保留为兼容层
- 辅助模块拆入 `vlm_cache.py`、`vlm_response_parser.py`、`vlm_provider_clients.py`、`vlm_error_classifier.py`、`vlm_exceptions.py`

#### 5. Report Generator 拆分
- `report_generator.py` 保留为兼容层
- 实现拆入 `report_markdown_renderer.py`、`report_far_enhancement_renderer.py`、`report_text_utils.py`

#### 6. Models 拆分
- `models/schemas.py` 保留为兼容层
- 模型拆入 `enums.py`、`video.py`、`scene.py`、`event.py`、`llm.py`、`report.py`、`config.py`、`context.py`

#### 7. Orchestrator 辅助逻辑拆分
- `orchestrator/analysis_orchestrator.py` 保留主编排
- 辅助逻辑拆入 `orchestrator_exceptions.py`、`video_meta_extractor.py`、`reject_report_factory.py`、`candidate_fallback.py`

#### 8. 新增多 Provider 故障转移
- 支持 `LLM_PROVIDER_0_*`、`LLM_PROVIDER_1_*` 环境变量配置多个 provider
- 主 provider quota/鉴权/限流/5xx 耗尽时自动切换到备用 provider
- LLM API 配置现在严格只从 `.env` 文件读取，不读取系统环境变量

#### 9. 依赖与部署精简
- `requirements.txt` 已精简，移除 aiohttp/rich/tenacity，为关键包加 upper bound
- 新增 `requirements-dev.txt`（pytest 相关）
- Dockerfile / Dockerfile.gpu 已移除 YOLO/torch 补丁安装

#### 10. 工具层调整
- 工具层框架（Tool Schema + Tool Router）保留，但当前无内置工具
- 移除 `traffic_analyzer/models/yolo/`、`traffic_analyzer/tools/yolo_track_tool.py`
- 移除相关测试 `tests/tools/test_yolo_track_tool.py`、`tests/tools/test_expert_agent_tools.py`

#### 11. Prompt 配置迁移
- `prompt_templates.yaml` 已删除
- 所有 YAML prompt 迁移至 `traffic_analyzer/config/prompts/*.yaml`

#### 12. 测试覆盖
- `traffic_analyzer/tests/test_expert_agent_far_enhancement.py`: event_id=4 远距离增强量化指标测试
- `traffic_analyzer/tests/test_report_generator.py`: 报告生成与量化 confidence 一致性测试

### 关键文件变更

| 文件 | 变更 |
|---|---|
| `traffic_analyzer/core/expert_agent.py` | 兼容层，event_id=4 远距离 ROI 增强逻辑拆出 |
| `traffic_analyzer/core/expert_agent_far_enhancement.py` | 新增：远距离 ROI 增强实现 |
| `traffic_analyzer/core/expert_agent_tools.py` | 新增：专家 Agent 工具相关逻辑 |
| `traffic_analyzer/core/vlm_engine.py` | 兼容层，VLM 调用逻辑拆出 |
| `traffic_analyzer/core/vlm_cache.py` | 新增：VLM 缓存 |
| `traffic_analyzer/core/vlm_response_parser.py` | 新增：VLM 响应解析 |
| `traffic_analyzer/core/vlm_provider_clients.py` | 新增：多 provider 客户端 |
| `traffic_analyzer/core/vlm_error_classifier.py` | 新增：VLM 错误分类 |
| `traffic_analyzer/core/vlm_exceptions.py` | 新增：VLM 异常定义 |
| `traffic_analyzer/core/report_generator.py` | 兼容层，报告渲染拆出 |
| `traffic_analyzer/core/report_markdown_renderer.py` | 新增：Markdown 报告渲染 |
| `traffic_analyzer/core/report_far_enhancement_renderer.py` | 新增：远距离增强报告渲染 |
| `traffic_analyzer/core/report_text_utils.py` | 新增：报告文本工具 |
| `traffic_analyzer/models/schemas.py` | 兼容层，模型拆出 |
| `traffic_analyzer/models/enums.py` | 新增：枚举定义 |
| `traffic_analyzer/models/video.py` | 新增：视频相关模型 |
| `traffic_analyzer/models/scene.py` | 新增：场景相关模型 |
| `traffic_analyzer/models/event.py` | 新增：事件相关模型 |
| `traffic_analyzer/models/llm.py` | 新增：LLM 相关模型 |
| `traffic_analyzer/models/report.py` | 新增：报告相关模型 |
| `traffic_analyzer/models/config.py` | 新增：配置相关模型 |
| `traffic_analyzer/models/context.py` | 新增：上下文相关模型 |
| `traffic_analyzer/orchestrator/analysis_orchestrator.py` | 主编排保留，辅助逻辑拆出 |
| `traffic_analyzer/orchestrator/orchestrator_exceptions.py` | 新增：编排异常 |
| `traffic_analyzer/orchestrator/video_meta_extractor.py` | 新增：视频元信息提取 |
| `traffic_analyzer/orchestrator/reject_report_factory.py` | 新增：拒绝报告工厂 |
| `traffic_analyzer/orchestrator/candidate_fallback.py` | 新增：候选回退逻辑 |
| `traffic_analyzer/config/prompts/*.yaml` | 非机动车/摩托车 ROI 增强 prompt 改为 0-1 量化描述 |
| `traffic_analyzer/config/.env.example` | 更新为多 provider 配置示例 |
| `requirements.txt` | 精简依赖，加 upper bound |
| `requirements-dev.txt` | 新增：开发依赖 |
| `Dockerfile` / `Dockerfile.gpu` | 移除 YOLO/torch 补丁安装 |

### 版本说明
- 版本号跳跃原因：内部已沉淀 v3.x 系列迭代（专家 Agent 重构、工具系统集成），本次对外发布统一为 **v4.0.0**，与 `README` 标注版本对齐。

---

## [v2.1.0] 2026-05-29 — Anthropic Native API 工具调用 + Docker 容器化

### 核心功能

#### 1. Anthropic Native API 工具调用
- **标准流程**: 使用 `anthropic` 库直接调用 `client.messages.create(tools=[...])`
- **自动检测**: 检查 `response.stop_reason == "tool_use"` 识别模型是否返回工具调用
- **文本 Fallback**: 当模型（如 Kimi）不返回原生 `tool_use` block 时，自动解析 `<tool_call>` 标签中的 JSON
- **完整链路**: 第一次调用（传 tools）→ 执行工具 → 第二次调用（传 tool_result）→ 返回最终判断

#### 2. 工具系统架构
- **ToolSchema**: 统一的工具定义层，支持 `to_anthropic()` / `to_openai()` 格式转换
- **ToolRouter**: 工具路由层，负责请求解析、权限校验、执行分发
- 工具层框架保留，但当时内置的 YOLO Track Tool 已在后续版本中移除

#### 3. 配置驱动集成
- `event_categories.yaml`: 事件加 `tools` 字段启用工具（当前工具层无内置工具）
- `traffic_analyzer/config/prompts/*.yaml`: 模板加 `available_tools` 字段声明可用工具
- 专家 Agent 自动检测工具配置，优先走 Native API，失败 fallback 到常规调用

### Docker 容器化

#### 基础环境
- **CPU 版**: `python:3.11-slim-bookworm` + OpenCV
- **GPU 版**: CUDA 11.4 + torch cu118（`Dockerfile.gpu`）
- **工作目录**: `/data`（与宿主机项目目录挂载同步）

#### 网络配置
- **Host 网络模式**: 与宿主机共享网络栈，解决容器内代理访问问题
- **代理支持**: 通过 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量配置 clash 代理

### Prompt 优化

#### 违停检测 (`illegal_parking_detection`)
- 工具说明移到 prompt 最前面
- 删除手动像素位移估计（改为必须调用工具）
- 明确静止判定依赖工具返回的 `is_stationary`

#### 逆行检测 (`direct_reversing_detection`)
- 删除车头朝向判断（VLM 识别不可靠）
- 改为单一流程: 扫描 → 调用工具 → `direction_text` 对比正常流向 → 判定
- 明确提示"不要通过车头朝向判断逆行"

### 测试覆盖
- `tests/tools/test_tool_router.py`: 工具路由层单元测试

### 关键文件变更

| 文件 | 变更 |
|---|---|
| `traffic_analyzer/core/expert_agent.py` | +806 行: Native API 工具调用、fallback 逻辑、二次 VLM 调用 |
| `traffic_analyzer/core/vlm_engine.py` | +275 行: `call_with_tools()`、`call_with_tool_results()` |
| `traffic_analyzer/tools/tool_schema.py` | +448 行: 工具定义、Anthropic/OpenAI 格式转换 |
| `traffic_analyzer/tools/tool_router.py` | +533 行: 工具路由、请求解析、执行分发 |
| `traffic_analyzer/config/prompts/*.yaml` | 违停/逆行 prompt 重构，加工具调用说明 |
| `Dockerfile` / `docker-compose.yml` | 容器化配置，host 网络模式 |

### 已知限制

1. **Kimi 模型不支持原生 tool_use**: 当前通过文本解析 `<tool_call>` fallback 解决
2. **逆行检测 adjudication 问题**: 第二次 VLM 返回 `detected=True`，但 adjudication 阶段可能改为 `False`（待排查）

### 使用方法

```bash
# 构建并启动容器
docker-compose up -d

# 验证配置
docker-compose exec -T traffic-agent python3 -m traffic_analyzer validate-config

# 分析视频（工具自动触发）
docker-compose exec -T traffic-agent python3 -m traffic_analyzer analyze \
  --video /data/test_videos/test.mp4 --format markdown --output report.md
```

---

## [v2.0.0] 2026-05-11 — 工具调用基础架构

- 工具注册表 (`tool_registry.py`)
- 工具路由层 (`tool_router.py`)
- 专家 Agent 字符串解析 `<tool_call>` 方式
- 工具层框架初版（YOLO 跟踪工具初版已在后续版本中移除）
