# Traffic Analyzer / 交通事件分析系统

[English](#english-version) | [中文](#中文版)

---

## English Version

[Back to top](#traffic-analyzer--交通事件分析系统) | [中文](#中文版)

A multi-modal large vision model (VLM) based traffic event detection framework for highway surveillance video. Supports **10 event categories**, outputs a 10-bit binary encoding plus a detailed Markdown analysis report. All event definitions, prompt templates, and adjudication rules are driven by YAML configuration — adding a new event requires zero code changes.

> **Current version: v2.0.0** — Multi-agent expert + adjudication architecture (see [Version Tags](#version-tags)).

---

### Architecture Overview (v2.0.0)

The v2.0.0 architecture replaces the monolithic v1.5 pipeline with a **multi-agent expert layer + single VLM adjudication** design:

```
Video Input
    |
    v
1. Video Preprocessing
   - Coarse sampling + precision keyframe extraction
   - Two-stage sampling (dense early + uniform late)
    |
    v
2. ExpertAgentLayer (10 parallel ExpertAgents)
   Each ExpertAgent: single-event VLM call -> EventCandidate
   - Only fact identification (see it, report it)
   - No filtering or exclusion logic
    |
    v
3. AdjudicationStep (single VLM call)
   Input: all EventCandidates + keyframes + business rules
   Output: final EventResults + AuditLog
   - Resolves conflicts (e.g. accident suppresses parking)
   - Applies business rules from YAML
    |
    v
4. Report Generation
   - Markdown report (human-readable, per-step timing)
   - Binary encoding {bit_0_bit_1_..._bit_9}
   - Audit log of every inclusion / exclusion decision
```

**Key improvement over v1.5**: Instead of a single ~30s scene-understanding bottleneck followed by mixed detection modes, all 10 events are detected in parallel by dedicated expert agents, then a single adjudication call resolves conflicts using explicit business rules. This is more accurate, more auditable, and easier to tune.

---

### Supported Events

| ID | Code | Name | is_active |
|---|---|---|---|
| 0 | A | Illegal Parking | true |
| 1 | B | Emergency Lane Occupancy | true |
| 2 | C | Traffic Accident | true |
| 3 | D | Person Presence in Highway | true |
| 4 | E | Motorcycle Presence | true |
| 5 | F | Heavy Congestion | true |
| 6 | G | Road Construction | true |
| 7 | H | Vehicle Reversing | true |
| 8 | J | Thrown Objects | true |
| 9 | K | Lane Change over Solid Line | false |

All events use `detection_mode: "expert_agent"` in v2.0.0.

---

### Key Features

#### 1. Expert Agent Layer

Each active event gets its own **ExpertAgent** — a dedicated VLM call with a specialized prompt. Agents run in parallel via `ThreadPoolExecutor`. Each agent only performs **fact identification** (what it sees) without any filtering. This separation of concerns makes the system modular and debuggable.

#### 2. Adjudication Step

A **single VLM call** receives all expert candidates, keyframes, and business rules, then outputs:
- Final `EventResult` for each event (detected / not detected)
- `AuditLog` recording every inclusion / exclusion decision with reasoning
- `adjudication_reasoning` explaining the overall decision process

Business rules are defined in `event_categories.yaml` under `adjudication_rules:` and embedded into the adjudication prompt. Example rules:
- **Accident suppresses parking** — stationary vehicles in an accident scene are part of the accident, not illegal parking
- **Construction excludes emergency lane** — vehicles inside a construction zone are not emergency lane violations
- **Motorcycle excludes emergency lane** — a motorcycle on the shoulder is tagged as "motorcycle presence", not "emergency lane occupancy"

#### 3. Audit Log

Every event that is excluded during adjudication is recorded with a reason and the triggering rule ID. This makes the system transparent and helps debug false negatives.

```json
{
  "event_id": 0,
  "event_name": "Illegal Parking",
  "action": "excluded",
  "reason": "Vehicle is part of an accident scene",
  "rule_id": "accident_suppresses_parking"
}
```

#### 4. Config-Driven Design

All of the following are defined in YAML — no code changes needed:
- Event definitions (`event_categories.yaml`)
- Prompt templates (`prompt_templates.yaml`)
- Adjudication rules (`event_categories.yaml`)
- Legacy logic chains (`logic_chains.yaml`, kept for reference)

---

### Project Structure

```
traffic_analyzer/
├── config/
│   ├── event_categories.yaml      # Event definitions + adjudication_rules
│   ├── logic_chains.yaml          # Legacy logic chains (kept for reference)
│   └── prompt_templates.yaml      # VLM Prompt templates + adjudication template
├── core/
│   ├── config_manager.py          # Config loading, validation
│   ├── expert_agent.py            # Single-event detection agent
│   ├── logic_engine.py            # Logic chain execution engine
│   ├── pipeline_steps.py          # ExpertAgentLayer + AdjudicationStep
│   ├── report_generator.py        # Report generation
│   ├── video_preprocessor.py      # Video frame extraction
│   └── vlm_engine.py              # VLM wrapper (multi-provider + cache)
├── models/
│   └── schemas.py                 # Pydantic models (EventCandidate, AdjudicationResult, AuditEntry)
├── orchestrator/
│   └── analysis_orchestrator.py   # 4-step pipeline orchestrator
├── utils/
│   └── event_detection.py         # Image selection + response parsing helpers
└── config/
    └── .env                       # LLM provider config (API Key, etc.)
```

---

### Quick Start

#### 1. Configure LLM Provider

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# Edit .env, set API Key and model
```

Supported environment variables:

| Variable | Description | Default |
|---|---|---|
| `LLM_PROVIDER` | VLM provider (`anthropic` / `google` / `aliyun`) | `anthropic` |
| `LLM_API_KEY` | API Key | - |
| `LLM_MODEL` | Model name | `claude-sonnet-4-6` |
| `LLM_MAX_TOKENS` | Max output tokens | `4096` |
| `LLM_TEMPERATURE` | Sampling temperature | `0.2` |
| `LLM_TIMEOUT` | API timeout (seconds) | `120` |
| `LLM_MAX_RETRIES` | Max retry count | `3` |
| `LLM_ENABLE_CACHE` | Enable VLM result cache | `true` |
| `LLM_CACHE_MAX_SIZE` | Max cache entries | `128` |
| `VLM_MAX_FRAMES` | Max frames per VLM call | `10` |
| `PROMPT_VERSION_{TEMPLATE_ID}` | Force a specific prompt version | - |

#### 2. Install pre-commit hook (recommended)

```bash
pip install pre-commit
pre-commit install
```

Automatically validates config changes on commit to prevent invalid YAML from being committed.

#### 3. Validate Configuration

```bash
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config
```

Validates:
- YAML syntax and structure
- Event category -> prompt template cross-references
- Adjudication rule integrity
- Logic chain step references (for legacy chains)

#### 4. Run Analysis

```bash
# Basic usage (default 30 frames)
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md

# Fast mode (10 frames, for short videos / testing)
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md \
  --min-frames 10

# With CV track cross-validation
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --cv-tracks ./tracks.json \
  --format markdown \
  --output ./report.md
```

#### 5. Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)
print(report.event_results)
```

---

### Batch Inference & Evaluation

Two batch processing scripts are provided in the `scripts/` directory.

#### Batch Inference (`batch_infer.py`)

```bash
python3 scripts/batch_infer.py \
  --video-dir ./videos \
  --output-dir ./reports \
  --log-dir ./logs \
  --workers 4 \
  --format markdown \
  --min-frames 30
```

| Parameter | Description | Default |
|---|---|---|
| `--video-dir` / `-v` | Input video directory (required) | - |
| `--output-dir` / `-o` | Output report directory (required) | - |
| `--config-dir` / `-c` | Config directory | `./traffic_analyzer/config` |
| `--format` / `-f` | Output format (`markdown` / `json`) | `markdown` |
| `--min-frames` / `-m` | VLM max input frames | `30` |
| `--cv-tracks-dir` | CV track JSON directory (optional) | - |
| `--workers` / `-w` | Parallel workers (ProcessPoolExecutor) | CPU cores |
| `--log-dir` / `-l` | Per-video log directory | - |
| `--skip-existing` | Skip videos with existing reports (default) | `true` |
| `--no-skip-existing` | Force reprocess all videos | - |

#### Batch Evaluation (`batch_evaluate.py`)

```bash
# Default: interactive HTML report
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.html

# With standalone annotation file
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --gt-mode annotation_file \
  --annotation-file ./annotations.json \
  --output evaluation_report.html

# Markdown table report
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.md

# Single-class mode (only evaluate is_active=true events)
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --single-class \
  --config-dir ./traffic_analyzer/config \
  --output evaluation_report.html
```

| Parameter | Description | Default |
|---|---|---|
| `--video-dir` / `-v` | Video directory (for ground-truth extraction) | - |
| `--report-dir` / `-r` | Report directory (`.md` or `.json`) | - |
| `--output` | Output path (`.html` / `.md` / `.json`, auto-detected by extension) | `evaluation_report.html` |
| `--gt-mode` | Ground-truth source (`filename` / `annotation_file`) | `filename` |
| `--annotation-file` | Annotation file path (JSON or CSV) | - |
| `--single-class` | Only evaluate `is_active=true` events | - |
| `--config-dir` / `-c` | Config directory (for `--single-class`) | `./traffic_analyzer/config` |

**HTML Interactive Report Features:**
- Left panel: event statistics table + per-video results table (filterable by pass/fail)
- Right panel: video player + Markdown report preview
- Click a table row to play the video, click a report link to preview Markdown
- All data is inline-embedded using `file://` absolute paths — open directly in a browser, no HTTP server needed

**Full Batch Workflow:**

```bash
# 1. Batch inference (4 parallel workers, save logs)
python3 scripts/batch_infer.py \
  --video-dir ./test_videos \
  --output-dir ./output \
  --log-dir ./log \
  --workers 4 \
  --format markdown

# 2. Generate HTML evaluation report
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class

# 3. (Optional) Generate Markdown table report
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.md \
  --single-class
```

---

### Supported VLM Providers

- **Anthropic** (Claude) — default recommended
- **Google** (Gemini)
- **Aliyun** (Tongyi Qianwen)

Configure provider and API Key in `.env`.

---

### Tool-Call Style Logging

Runtime output follows modern AI Agent (Cursor / Cline / Claude Code) tool-call trace style:

```
[INFO] 14:30:00 🔧 tool_call: video_preprocessor.process(video='clip.mp4')
[INFO] 14:30:03   ↳ result: coarse=20, precision=41 | elapsed=3.0s
[INFO] 14:30:03 🔧 tool_call: expert_agent.detect(event='Illegal Parking')
[INFO] 14:30:15   ↳ result: detected=true, confidence=0.92 | elapsed=12.0s
[INFO] 14:30:15 🔧 tool_call: adjudication.resolve(candidates=10)
[INFO] 14:30:28   ↳ result: events=3, audit_entries=2 | elapsed=13.0s
```

Control granularity via `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL`:

| Value | Behavior |
|---|---|
| `off` | No tool_call logs |
| `macro` | Top-level calls only |
| `mid` | Top-level + nested (default) |
| `fine` | Reserved for future expansion |

```bash
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=off python -m traffic_analyzer ...    # silent
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=macro python -m traffic_analyzer ...  # top only
```

This logging is a **pure display layer** — it does not affect parallelism, performance, or results. The binary encoding output is identical regardless of log level.

---

### Version Tags

| Tag | Branch | Description |
|---|---|---|
| `v2.0.0-multi-agent` | `main` | **Current**. Multi-agent expert + adjudication architecture. All 10 events use `expert_agent` mode. Parallel detection + single VLM adjudication with business rules. |
| `v1.5.0-legacy` | `legacy/v1.5` | Monolithic architecture. SceneUnderstandingStep (~30s bottleneck) + mixed detection modes (direct_vlm parallel, logic_chain sequential, scene_tag zero-VLM) + PostProcessStep with cross-event inference. |

The `legacy/v1.5` branch preserves the old architecture for reference and comparison. All new development happens on `main` (v2.0.0).

---

## 中文版

[回到顶部](#traffic-analyzer--交通事件分析系统) | [English](#english-version)

基于多模态大视觉模型（VLM）的高速公路监控视频交通事件检测框架，支持 **10 类事件识别**，输出 10 位二进制编码 + 详细 Markdown 分析报告。所有事件定义、Prompt 模板、裁决规则均通过 YAML 配置驱动，新增事件无需修改代码。

> **当前版本：v2.0.0** — 多智能体专家 + 裁决层架构（详见 [版本标签说明](#版本标签说明)）。

---

### 架构概览 (v2.0.0)

v2.0.0 架构将 v1.5 的单体流水线替换为 **多智能体专家层 + 单 VLM 裁决** 设计：

```
视频输入
    |
    v
1. 视频预处理
   - 粗采样 + 精确关键帧提取
   - 两段式采样（前段密集 + 后段均匀）
    |
    v
2. ExpertAgentLayer（10 个并行 ExpertAgent）
   每个 ExpertAgent：单事件 VLM 调用 -> EventCandidate
   - 仅做事实识别（看到就报）
   - 不做过滤或排除判断
    |
    v
3. AdjudicationStep（单次 VLM 调用）
   输入：所有 EventCandidate + 关键帧 + 业务规则
   输出：最终 EventResults + AuditLog
   - 解决冲突（如事故抑制违停）
   - 应用 YAML 中定义的业务规则
    |
    v
4. 报告生成
   - Markdown 报告（人工可读，含每步耗时）
   - 二进制编码 {bit_0_bit_1_..._bit_9}
   - 每条包含/排除决策的审计日志
```

**相比 v1.5 的核心改进**：不再依赖单次约 30 秒的场景理解瓶颈 + 混合检测模式，而是让 10 个事件由专用专家代理并行检测，再由单次裁决调用根据显式业务规则解决冲突。准确率更高、可审计性更强、调试更方便。

---

### 支持的事件

| ID | 编码 | 事件名称 | is_active |
|---|---|---|---|
| 0 | A | 违法停车 | true |
| 1 | B | 应急车道占用 | true |
| 2 | C | 交通事故 | true |
| 3 | D | 高速公路行人出现 | true |
| 4 | E | 摩托车出现 | true |
| 5 | F | 严重拥堵 | true |
| 6 | G | 道路施工 | true |
| 7 | H | 车辆逆行/倒车 | true |
| 8 | J | 抛洒物 | true |
| 9 | K | 实线变道 | false |

v2.0.0 中所有事件均使用 `detection_mode: "expert_agent"`。

---

### 核心特性

#### 1. 专家代理层 (Expert Agent Layer)

每个激活的事件拥有独立的 **ExpertAgent** —— 一次专用的 VLM 调用，携带针对该事件的专用 Prompt。所有 Agent 通过 `ThreadPoolExecutor` 并行执行。每个 Agent 只负责 **事实识别**（看到什么报什么），不做任何过滤。这种关注点分离使系统模块化且易于调试。

#### 2. 裁决步骤 (Adjudication Step)

**单次 VLM 调用**接收所有专家候选结果、关键帧和业务规则，输出：
- 每个事件的最终 `EventResult`（检出 / 未检出）
- `AuditLog` 记录每条包含/排除决策及其理由
- `adjudication_reasoning` 解释整体决策过程

业务规则在 `event_categories.yaml` 的 `adjudication_rules:` 下定义，并嵌入裁决 Prompt 中。示例规则：
- **事故优先于违停** —— 事故场景中的静止车辆属于事故的一部分，不应再单独标记为违停
- **施工区域排除应急车道占用** —— 明确位于施工区域内的车辆不判定为应急车道占用
- **摩托车排除应急车道占用** —— 应急车道上的摩托车优先判定为"摩托车出现"，避免重复标记

#### 3. 审计日志 (Audit Log)

裁决过程中被排除的每个事件都会记录原因和触发规则的 ID。这使系统透明化，有助于调试漏报。

```json
{
  "event_id": 0,
  "event_name": "违法停车",
  "action": "excluded",
  "reason": "车辆属于事故场景的一部分",
  "rule_id": "accident_suppresses_parking"
}
```

#### 4. 配置驱动设计

以下内容全部在 YAML 中定义 —— 无需修改代码：
- 事件定义（`event_categories.yaml`）
- Prompt 模板（`prompt_templates.yaml`）
- 裁决规则（`event_categories.yaml`）
- 遗留逻辑链（`logic_chains.yaml`，保留作参考）

---

### 项目结构

```
traffic_analyzer/
├── config/
│   ├── event_categories.yaml      # 事件定义 + 裁决规则
│   ├── logic_chains.yaml          # 遗留逻辑链（保留作参考）
│   └── prompt_templates.yaml      # VLM Prompt 模板 + 裁决模板
├── core/
│   ├── config_manager.py          # 配置加载、校验
│   ├── expert_agent.py            # 单事件检测代理
│   ├── logic_engine.py            # 逻辑链执行引擎
│   ├── pipeline_steps.py          # ExpertAgentLayer + AdjudicationStep
│   ├── report_generator.py        # 报告生成
│   ├── video_preprocessor.py      # 视频帧提取
│   └── vlm_engine.py              # VLM 封装（多提供商 + 缓存）
├── models/
│   └── schemas.py                 # Pydantic 模型（EventCandidate, AdjudicationResult, AuditEntry）
├── orchestrator/
│   └── analysis_orchestrator.py   # 4 步流水线编排器
├── utils/
│   └── event_detection.py         # 图像选择 + 响应解析辅助函数
└── config/
    └── .env                       # LLM 提供商配置（API Key 等）
```

---

### 快速开始

#### 1. 配置 LLM 提供商

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# 编辑 .env，设置 API Key 和模型
```

支持的环境变量：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_PROVIDER` | VLM 提供商 (`anthropic` / `google` / `aliyun`) | `anthropic` |
| `LLM_API_KEY` | API Key | - |
| `LLM_MODEL` | 模型名称 | `claude-sonnet-4-6` |
| `LLM_MAX_TOKENS` | 最大输出 token | `4096` |
| `LLM_TEMPERATURE` | 采样温度 | `0.2` |
| `LLM_TIMEOUT` | API 超时（秒） | `120` |
| `LLM_MAX_RETRIES` | 最大重试次数 | `3` |
| `LLM_ENABLE_CACHE` | 启用 VLM 结果缓存 | `true` |
| `LLM_CACHE_MAX_SIZE` | 缓存最大条目数 | `128` |
| `VLM_MAX_FRAMES` | VLM 调用最大帧数 | `10` |
| `PROMPT_VERSION_{TEMPLATE_ID}` | 强制使用指定 Prompt 版本 | - |

#### 2. 安装 pre-commit hook（推荐）

```bash
pip install pre-commit
pre-commit install
```

配置变更时自动校验，防止提交无效配置。

#### 3. 验证配置

```bash
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config
```

校验内容包括：
- YAML 语法和结构
- 事件类别与 Prompt 模板的交叉引用
- 裁决规则完整性
- 逻辑链步骤引用（遗留链）

#### 4. 运行分析

```bash
# 基本用法（默认 30 帧）
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md

# 快速模式（10 帧，适合短视频/测试）
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md \
  --min-frames 10

# 带 CV 轨迹验证
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --cv-tracks ./tracks.json \
  --format markdown \
  --output ./report.md
```

#### 5. Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)
print(report.event_results)
```

---

### 批量推理与评估

项目提供两个批量处理脚本，位于 `scripts/` 目录。

#### 批量推理 (`batch_infer.py`)

```bash
python3 scripts/batch_infer.py \
  --video-dir ./videos \
  --output-dir ./reports \
  --log-dir ./logs \
  --workers 4 \
  --format markdown \
  --min-frames 30
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--video-dir` / `-v` | 输入视频目录（必需） | - |
| `--output-dir` / `-o` | 输出报告目录（必需） | - |
| `--config-dir` / `-c` | 配置目录 | `./traffic_analyzer/config` |
| `--format` / `-f` | 输出格式 (`markdown` / `json`) | `markdown` |
| `--min-frames` / `-m` | VLM 最大输入帧数 | `30` |
| `--cv-tracks-dir` | CV 轨迹 JSON 目录（可选） | - |
| `--workers` / `-w` | 并行 worker 数（ProcessPoolExecutor） | CPU 核心数 |
| `--log-dir` / `-l` | 逐视频日志文件存放目录 | - |
| `--skip-existing` | 跳过已有报告的视频（默认启用） | `true` |
| `--no-skip-existing` | 强制重新处理所有视频 | - |

#### 批量评估 (`batch_evaluate.py`)

```bash
# 默认：生成 HTML 交互式报告
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.html

# 使用独立标注文件
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --gt-mode annotation_file \
  --annotation-file ./annotations.json \
  --output evaluation_report.html

# Markdown 表格报告
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.md

# 单类别模式（只评估 is_active=true 的事件）
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --single-class \
  --config-dir ./traffic_analyzer/config \
  --output evaluation_report.html
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--video-dir` / `-v` | 视频目录（用于提取真实标签） | - |
| `--report-dir` / `-r` | 报告目录（`.md` 或 `.json`） | - |
| `--output` | 评估结果输出路径（支持 `.html` / `.md` / `.json`，按扩展名自动识别格式） | `evaluation_report.html` |
| `--gt-mode` | 真实标签来源 (`filename` / `annotation_file`) | `filename` |
| `--annotation-file` | 标注文件路径（JSON 或 CSV） | - |
| `--single-class` | 只评估 `is_active=true` 的事件 | - |
| `--config-dir` / `-c` | 配置目录（配合 `--single-class`） | `./traffic_analyzer/config` |

**HTML 交互式报告特性：**
- 左侧：事件统计表 + 逐视频结果表（支持筛选 通过/不通过）
- 右侧：视频播放器 + Markdown 报告预览面板
- 点击表格行播放视频，点击报告链接预览 Markdown
- 所有数据内联嵌入，使用 `file://` 绝对路径，可直接双击打开，无需 HTTP 服务器

**完整批量工作流示例：**

```bash
# 1. 批量推理（4 并行 worker，保存日志）
python3 scripts/batch_infer.py \
  --video-dir ./测试视频 \
  --output-dir ./output \
  --log-dir ./log \
  --workers 4 \
  --format markdown

# 2. 生成 HTML 交互式评估报告
python3 scripts/batch_evaluate.py \
  --video-dir ./测试视频 \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class

# 3. （可选）生成 Markdown 表格报告
python3 scripts/batch_evaluate.py \
  --video-dir ./测试视频 \
  --report-dir ./output \
  --output ./evaluation_report.md \
  --single-class
```

---

### 支持的 VLM 提供商

- **Anthropic** (Claude) — 默认推荐
- **Google** (Gemini)
- **Aliyun** (通义千问)

在 `.env` 中配置提供商和 API Key。

---

### Tool-Call 风格日志输出

运行时输出类似现代 AI Agent (Cursor / Cline / Claude Code) 的工具调用轨迹日志：

```
[INFO] 14:30:00 🔧 tool_call: video_preprocessor.process(video='clip.mp4')
[INFO] 14:30:03   ↳ result: coarse=20, precision=41 | elapsed=3.0s
[INFO] 14:30:03 🔧 tool_call: expert_agent.detect(event='违法停车')
[INFO] 14:30:15   ↳ result: detected=true, confidence=0.92 | elapsed=12.0s
[INFO] 14:30:15 🔧 tool_call: adjudication.resolve(candidates=10)
[INFO] 14:30:28   ↳ result: events=3, audit_entries=2 | elapsed=13.0s
```

通过环境变量 `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL` 切换粒度：

| 值 | 行为 |
|---|---|
| `off` | 不输出任何 tool_call 日志 |
| `macro` | 仅顶层调用 |
| `mid` | 顶层 + 嵌套（默认） |
| `fine` | 预留，未来扩展 |

```bash
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=off python -m traffic_analyzer ...    # 关闭
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=macro python -m traffic_analyzer ...  # 仅顶层
```

此日志是**纯显示层**，不影响并行/性能/结果。关闭后输出的二进制编码与开启时完全一致。

---

### 版本标签说明

| 标签 | 分支 | 说明 |
|---|---|---|
| `v2.0.0-multi-agent` | `main` | **当前版本**。多智能体专家 + 裁决架构。全部 10 个事件使用 `expert_agent` 模式。并行检测 + 单次 VLM 裁决，带业务规则。 |
| `v1.5.0-legacy` | `legacy/v1.5` | 单体架构。SceneUnderstandingStep（约 30 秒瓶颈）+ 混合检测模式（direct_vlm 并行、logic_chain 串行、scene_tag 零 VLM）+ PostProcessStep 跨事件推断。 |

`legacy/v1.5` 分支保留旧架构供参考和对比。所有新开发在 `main`（v2.0.0）上进行。
