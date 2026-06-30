[English](README.md) | [简体中文](README.zh-CN.md)

# 交通事件分析系统

基于多模态大视觉模型（VLM）的高速公路监控视频交通事件检测框架，支持 **10 类事件识别**（当前 **8 类激活：0-7**），输出 10 位二进制编码 + 详细 Markdown 分析报告。所有事件定义、Prompt 模板、裁决规则均通过 YAML 配置驱动，新增事件无需修改代码。

> **当前版本：v4.0.0** —— VLM 多智能体专家 + 裁决层架构，支持行人、非机动车、道路施工的远距离 ROI 证据增强。工具层框架保留，但当前无内置工具。

---

## 架构概览 (v4.0.0)

```
视频输入
    |
    v
1. 视频预处理
   - 粗采样 + 精确关键帧提取
   - 两段式采样（前段密集 + 后段均匀）
    |
    v
2. ExpertAgentLayer（针对激活事件并行运行 ExpertAgent）
   每个 ExpertAgent：单事件 VLM 调用 -> EventCandidate
   - 仅做事实识别（看到就报）
   - event_id=3（高速公路行人出现）、event_id=4（摩托车出现）、
     event_id=6（道路施工）在 Prompt 模板启用时启用远距离 ROI 证据增强：
       * event_id=3/4：逐帧 ROI 检测 -> 双图合成（单帧放大 + 运动对比）-> 最终分类器
       * event_id=6：中间帧多 ROI 画廊 -> 最终分类器
    |
    v
3. AdjudicationStep（单次 VLM 调用，带重试循环）
   输入：所有 EventCandidate + 关键帧 + 业务规则 + 标注规范
   输出：最终 EventResults + AuditLog
   - 解决冲突（如事故抑制违停）
   - 应用 YAML 中定义的业务规则
   - event_results 不完整时最多重试 5 次
    |
    v
4. 报告生成
   - Markdown 报告（人工可读，含每步耗时）
   - JSON 报告
   - 二进制编码 {bit_0_bit_1_..._bit_9}
   - 每条包含/排除决策的审计日志
```

默认推理流程由 **VLM 驱动**。工具层框架（工具定义层 + 工具路由层）保留以供后续扩展，但当前无内置工具。

---

## 支持的事件

当前以下事件 `is_active=true`。事件位 8、9 保留在二进制编码中，但推理时跳过。

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

事件 8（抛洒物）、9（实线变道）当前未激活。

---

## 核心特性

### 1. 专家代理层 (Expert Agent Layer)

每个激活的事件拥有独立的 **ExpertAgent** —— 一次专用的 VLM 调用，携带针对该事件的专用 Prompt。所有 Agent 通过 `ThreadPoolExecutor` 并行执行。每个 Agent 只负责 **事实识别**（看到什么报什么），不做任何过滤。这种关注点分离使系统模块化且易于调试。

### 2. 远距离 ROI 证据增强

在 Prompt 模板中设置了 `far_object_enhancement.enabled: true` 的事件会启用 ROI 驱动的远距离证据增强流程。当前启用的事件：

- **event_id=3（高速公路行人出现）** — 逐帧 ROI 检测返回归一化 bbox、遮挡标志和 `[0.0, 1.0]` 连续置信度。综合置信度、面积、宽高比、遮挡程度和相邻帧运动分数对 top-K 候选排序，生成双图合成（单帧放大 + 相邻帧运动对比）后送入最终分类器，输出完整专家响应格式。
- **event_id=4（摩托车出现）** — 与 event_id=3 使用相同的逐帧 ROI + 双图合成流程，但针对摩托车/电动车/自行车/三轮车进行专门优化。最终分类器使用最小 `{detected, reason}` 格式，并增加“无可辨识车辆结构”否决规则，避免将暗斑、反光点等误报为非机动车。
- **event_id=6（道路施工）** — 使用**多 ROI 画廊**：从中间帧提取多个施工证据区域（`cone` 锥桶、`worker` 施工人员、`vehicle` 施工车辆、`barrier` 隔离栏/围挡、`sign` 施工标志牌），附带置信度和 `on_ground` 落地标志；取最多 4 个区域拼成标注画廊，再送入分类器。即使分类器为负，只要 ROI 证据满足施工作业区定义，就会通过施工专用回退逻辑提升为阳性结果。

event_id=3 与 event_id=4 在分类器为负但 ROI 证据充分（高置信度、未遮挡）时，可将最优候选安全回退为阳性。二阶段运动对比图规则已优化：相邻帧看不到目标反而支持其为运动中的非机动车；静止暗斑、反光点等应排除。

### 3. 裁决步骤 (Adjudication Step)

**单次 VLM 调用**接收所有专家候选结果、关键帧和业务规则，输出：
- 每个事件的最终 `EventResult`（检出 / 未检出）
- `AuditLog` 记录每条包含/排除决策及其理由
- `adjudication_reasoning` 解释整体决策过程

业务规则在 `event_categories.yaml` 的 `adjudication_rules:` 下定义，并嵌入裁决 Prompt 中。示例规则：
- **事故优先于违停** —— 事故场景中的静止车辆属于事故的一部分，不应再单独标记为违停
- **施工区域排除应急车道占用** —— 明确位于施工区域内的车辆不判定为应急车道占用
- **摩托车排除应急车道占用** —— 应急车道上的摩托车优先判定为"摩托车出现"，避免重复标记

### 4. 审计日志 (Audit Log)

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

### 5. 配置驱动设计

以下内容全部在 YAML 中定义 —— 无需修改代码：
- 事件定义（`event_categories.yaml`）
- Prompt 模板（`prompts/*.yaml`）
- 裁决规则（`event_categories.yaml`）
- 标注规范（`annotation_spec.yaml`）

### 6. 裁决重试循环

裁决步骤最多运行 **5 次**尝试。若 `event_results` 不完整：
- 检查对应专家输出是否异常，异常则仅重跑这些专家；
- 否则在提示中列出上次遗漏的事件并重新裁决；
- 5 次尝试后仍缺失的事件将从原始专家候选回填。

这使流水线对偶发的 VLM 遗漏具有鲁棒性，同时不丢失有效的专家信号。

### 7. JSON 修复与候选清洗

VLM 输出在解析前会自动加固：
- `vlm_response_parser.py` 中的 `_repair_json` 修复常见语法错误（缺逗号、尾随逗号等）。
- `event_detection.py` 中的 `_sanitize_candidate` 协调不一致的专家输出（例如 `detected=true` 但内容否认事件）。

---

## 项目结构

```
traffic_analyzer/
├── config/
│   ├── annotation_spec.yaml       # 注入裁决 Prompt 的标注规范
│   ├── event_categories.yaml      # 事件定义 + 裁决规则
│   ├── prompts/                   # VLM Prompt 模板（event_*.yaml + common.yaml）
│   └── .env.example               # LLM 提供商配置示例
├── core/
│   ├── config_manager.py          # 配置加载、校验
│   ├── expert_agent.py            # 单事件检测代理兼容层
│   ├── expert_agent_far_enhancement.py  # 远距离 ROI 证据增强
│   ├── expert_agent_tools.py      # 专家代理工具辅助函数
│   ├── pipeline_steps.py          # ExpertAgentLayer + AdjudicationStep（含重试）
│   ├── report_generator.py        # 报告生成兼容层
│   ├── report_markdown_renderer.py      # Markdown 报告渲染
│   ├── report_far_enhancement_renderer.py  # 增强流程报告渲染
│   ├── report_text_utils.py       # 报告文本格式化工具
│   ├── video_preprocessor.py      # 视频帧提取
│   ├── vlm_engine.py              # VLM 封装兼容层
│   ├── vlm_cache.py               # 内存 + 磁盘 VLM 结果缓存
│   ├── vlm_response_parser.py     # VLM 响应解析 + JSON 修复
│   ├── vlm_provider_clients.py    # 各提供商 API 客户端
│   ├── vlm_error_classifier.py    # API 错误分类，用于故障转移决策
│   └── vlm_exceptions.py          # VLM 相关异常
├── models/
│   ├── schemas.py                 # 兼容层，重新导出所有 Pydantic 模型
│   ├── enums.py                   # DetectionMode, ConfidenceLevel
│   ├── video.py                   # VideoMetadata, Keyframe, KeyframeSequence
│   ├── scene.py                   # SceneInfo, RoadInfo, DirectionAnalysis 等
│   ├── event.py                   # EventCategory, EventCandidate, EventResult, AuditEntry 等
│   ├── llm.py                     # LLMResponse, LLMCallRecord, PromptTemplate 等
│   ├── report.py                  # Report, BinaryEncoding
│   ├── config.py                  # SystemConfig, LLMProviderConfig, SamplingConfig
│   └── context.py                 # AnalysisContext
├── orchestrator/
│   ├── analysis_orchestrator.py   # 4 步流水线主编排器
│   ├── orchestrator_exceptions.py # 编排器专用异常
│   ├── video_meta_extractor.py    # 视频元信息提取
│   ├── reject_report_factory.py   # 拒绝报告生成
│   └── candidate_fallback.py      # 候选结果回退辅助
├── tools/
│   ├── tool_schema.py             # 工具定义层
│   ├── tool_router.py             # 工具路由层
│   └── tool_registry.py           # 默认 Router 注册（当前无内置工具）
├── utils/
│   ├── event_detection.py         # 图像选择 + 响应解析 + 候选清洗
│   ├── far_non_motor_enhancer.py  # 远距离非机动车增强工具函数
│   ├── roi_composite.py           # ROI 合成图生成
│   ├── roi_motion.py              # ROI 运动分析
│   ├── bbox_geometry.py           # 边界框几何辅助
│   ├── image_drawing.py           # 图像标注辅助
│   ├── annotation_spec_loader.py  # 标注规范加载
│   ├── construction_evidence_gallery.py  # 施工事件证据画廊
│   └── tool_call_logger.py        # Tool-Call 风格日志
├── cli.py                         # CLI 入口
└── __main__.py                    # `python -m traffic_analyzer`
```

---

## 快速开始

### 1. 配置 LLM 提供商

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# 编辑 .env，设置 API Key 和模型
```

LLM 配置**严格只从配置目录下的 `.env` 文件读取**，不读取系统环境变量。支持两种配置方式：

**单提供商（向后兼容）：**

| 变量 | 说明 | 默认值 |
|---|---|---|
| `VLM_PROVIDER` / `LLM_PROVIDER` | VLM 提供商 (`anthropic` / `google` / `aliyun`) | `anthropic` |
| `LLM_API_KEY` | API Key | - |
| `LLM_MODEL` | 模型名称 | `claude-sonnet-4-6` |

**多提供商故障转移（推荐）：**

| 变量 | 说明 | 示例 |
|---|---|---|
| `LLM_PROVIDER_0_PROVIDER` | 主提供商 | `anthropic` |
| `LLM_PROVIDER_0_API_KEY` | 主提供商 API Key | - |
| `LLM_PROVIDER_0_MODEL` | 主提供商模型 | `claude-sonnet-4-6` |
| `LLM_PROVIDER_1_PROVIDER` | 备用提供商 | `aliyun` |
| `LLM_PROVIDER_1_API_KEY` | 备用提供商 API Key | - |
| `LLM_PROVIDER_1_MODEL` | 备用提供商模型 | `qwen-vl-max` |

当存在带序号的 `LLM_PROVIDER_N_*` 变量时，优先于单提供商变量。编排器默认使用 provider 0；遇到配额耗尽、鉴权失败、限流或 5xx 错误时，自动切换到 provider 1（以及更多序号提供商）。

共享推理参数：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_MAX_TOKENS` | 最大输出 token | `4096` |
| `LLM_TEMPERATURE` | 采样温度 | `0.2` |
| `LLM_TIMEOUT` | API 超时（秒） | `120` |
| `LLM_MAX_RETRIES` | 每个提供商最大重试次数 | `3` |
| `LLM_ENABLE_CACHE` | 启用 VLM 结果缓存 | `true` |
| `LLM_CACHE_MAX_SIZE` | 缓存最大条目数 | `128` |
| `TRAFFIC_ANALYZER_DISK_CACHE` | SQLite 磁盘缓存路径（跨进程） | - |
| `TRAFFIC_ANALYZER_DISK_CACHE_MAX_ENTRIES` | 磁盘缓存最大条目数 | `2000` |
| `VLM_MAX_FRAMES` | VLM 调用最大帧数 | `10` |
| `PROMPT_VERSION_{TEMPLATE_ID}` | 强制使用指定 Prompt 版本 | - |

### 2. 安装 pre-commit hook（推荐）

```bash
pip install pre-commit
pre-commit install
```

配置变更时自动校验，防止提交无效配置。

### 3. 验证配置

```bash
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config
```

### 4. 运行分析

```bash
# 基本用法（默认 10 帧）
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md

# 更多帧（精度更高、速度更慢）
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md \
  --min-frames 30
```

### 5. Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)
print(report.event_results)
```

---

## 批量推理与评估

### 批量推理 (`scripts/batch_infer.py`)

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
| `--workers` / `-w` | 并行 worker 数（ProcessPoolExecutor） | CPU 核心数 |
| `--log-dir` / `-l` | 逐视频日志文件存放目录 | - |
| `--skip-existing` | 跳过已有报告的视频（默认启用） | `true` |
| `--no-skip-existing` | 强制重新处理所有视频 | - |

### 批量评估 (`scripts/batch_evaluate.py`)

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
  --video-dir ./test_videos \
  --output-dir ./output \
  --log-dir ./log \
  --workers 4 \
  --format markdown

# 2. 生成 HTML 交互式评估报告
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class

# 3. （可选）生成 Markdown 表格报告
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.md \
  --single-class
```

---

## 支持的 VLM 提供商

- **Anthropic** (Claude) — 默认推荐
- **Google** (Gemini)
- **Aliyun** (通义千问)

在 `.env` 中配置提供商和 API Key。可配置多个提供商实现自动故障转移。

---

## Tool-Call 风格日志输出

运行时输出类似现代 AI Agent 的工具调用轨迹日志：

```
[INFO] 14:30:00 🔧 tool_call: video_preprocessor.process(video='clip.mp4')
[INFO] 14:30:03   ↳ result: coarse=20, precision=41 | elapsed=3.0s
[INFO] 14:30:03 🔧 tool_call: expert_agent.detect(event='高速公路行人出现')
[INFO] 14:30:15   ↳ result: detected=true | elapsed=12.0s
[INFO] 14:30:15 🔧 tool_call: adjudication.resolve(candidates=4)
[INFO] 14:30:28   ↳ result: events=2, audit_entries=1 | elapsed=13.0s
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

## 版本标签说明

| 标签 | 分支 | 说明 |
|---|---|---|
| `v4.0.0-far-enhancement` | `main` | **当前版本**。VLM 多智能体专家 + 裁决层架构。事件 0-7 激活。新增 event_id=3（行人）、event_id=4（非机动车）、event_id=6（道路施工）远距离 ROI 证据增强，ROI 置信度使用 0-1 连续量化。工具层框架保留，但当前无内置工具。 |
| `v2.0.0-multi-agent` | `legacy/v2.0` | 上一版稳定的多智能体架构，10 个事件中 8 个激活，纯 VLM 流水线。 |
| `v1.5.0-legacy` | `legacy/v1.5` | 单体架构。SceneUnderstandingStep（约 30 秒瓶颈）+ 混合检测模式（direct_vlm 并行、logic_chain 串行、scene_tag 零 VLM）+ PostProcessStep 跨事件推断。 |

所有新开发在 `main`（v4.0.0）上进行。
