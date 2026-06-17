[English](README.md) | [简体中文](README.zh-CN.md)

# 交通事件分析系统

基于多模态大视觉模型（VLM）的高速公路监控视频交通事件检测框架，支持 **10 类事件识别**（当前 **4 类激活**），输出 10 位二进制编码 + 详细 Markdown 分析报告。所有事件定义、Prompt 模板、裁决规则均通过 YAML 配置驱动，新增事件无需修改代码。

> **当前版本：v4.0.0** —— VLM 多智能体专家 + 裁决层架构，支持远距离非机动车 ROI 增强，并保留可选的工具层扩展（详见 [版本标签说明](#版本标签说明)）。

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
   - event_id=4（摩托车出现）可选启用远距离 ROI 增强流程：
     ROI 检测 -> 合成图生成 -> 最终分类器，ROI 置信度使用 0-1 连续量化
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

默认推理流程由 **VLM 驱动**。系统同时提供可选的 **工具层**（工具定义层 + 工具路由层），可用于接入 YOLO 跟踪等工具；默认流水线不会自动调用工具，但工具层可供后续扩展或独立脚本使用。

---

## 支持的事件

当前仅以下事件 `is_active=true`。其余事件位（0-2、7-9）保留在二进制编码中，但推理时跳过。

| ID | 编码 | 事件名称 | is_active |
|---|---|---|---|
| 3 | D | 高速公路行人出现 | true |
| 4 | E | 摩托车出现 | true |
| 5 | F | 严重拥堵 | true |
| 6 | G | 道路施工 | true |

事件 0（违法停车）、1（应急车道占用）、2（交通事故）、7（车辆逆行/倒车）、8（抛洒物）、9（实线变道）当前未激活。

---

## 核心特性

### 1. 专家代理层 (Expert Agent Layer)

每个激活的事件拥有独立的 **ExpertAgent** —— 一次专用的 VLM 调用，携带针对该事件的专用 Prompt。所有 Agent 通过 `ThreadPoolExecutor` 并行执行。每个 Agent 只负责 **事实识别**（看到什么报什么），不做任何过滤。这种关注点分离使系统模块化且易于调试。

### 2. 远距离非机动车增强（event_id=4）

针对摩托车/非机动车检测，当事件 Prompt 模板设置 `enable_far_object_enhancement: true` 时，会启用专用增强流程：

1. **ROI 检测**：逐帧 VLM 调用返回归一化 bbox、遮挡标志和 `[0.0, 1.0]` 连续置信度。
2. **候选评分**：综合置信度、面积、宽高比、遮挡程度和相邻帧运动分数对 ROI 排序。
3. **合成图生成**：对 top-K 候选生成双图合成（单帧放大 + 相邻帧运动对比）。
4. **最终分类**：第二次 VLM 调用判断裁剪区域内是否为非机动车。
5. **安全回退**：当分类器为负但 ROI 证据充分（高置信度、未遮挡）时，可将最优候选提升为阳性结果。

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
- Prompt 模板（`prompt_templates.yaml`）
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
- `vlm_engine.py` 中的 `_repair_json` 修复常见语法错误（缺逗号、尾随逗号等）。
- `event_detection.py` 中的 `_sanitize_candidate` 协调不一致的专家输出（例如 `detected=true` 但内容否认事件）。

---

## 项目结构

```
traffic_analyzer/
├── config/
│   ├── annotation_spec.yaml       # 注入裁决 Prompt 的标注规范
│   ├── event_categories.yaml      # 事件定义 + 裁决规则
│   ├── prompt_templates.yaml      # VLM Prompt 模板 + 裁决模板
│   └── .env.example               # LLM 提供商配置示例
├── core/
│   ├── config_manager.py          # 配置加载、校验
│   ├── expert_agent.py            # 单事件检测代理 + 远距离增强
│   ├── pipeline_steps.py          # ExpertAgentLayer + AdjudicationStep（含重试）
│   ├── report_generator.py        # 报告生成（Markdown / JSON / 二进制）
│   ├── video_preprocessor.py      # 视频帧提取
│   └── vlm_engine.py              # VLM 封装（多提供商 + 缓存 + JSON 修复）
├── models/
│   └── schemas.py                 # Pydantic 模型（EventCandidate, AdjudicationResult, AuditEntry）
├── orchestrator/
│   └── analysis_orchestrator.py   # 4 步流水线编排器
├── tools/
│   ├── tool_schema.py             # 工具定义层
│   ├── tool_router.py             # 工具路由层
│   └── tool_registry.py           # 默认 Router 注册
├── utils/
│   └── event_detection.py         # 图像选择 + 响应解析 + 候选清洗
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

在 `.env` 中配置提供商和 API Key。

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
| `v4.0.0-far-enhancement` | `main` | **当前版本**。VLM 多智能体专家 + 裁决层架构。当前仅事件 3、4、5、6 激活。新增 event_id=4 远距离非机动车 ROI 增强，ROI 置信度使用 0-1 连续量化。保留可选的工具层（工具定义层 + 工具路由层），可扩展 YOLO 跟踪等能力。 |
| `v2.0.0-multi-agent` | `legacy/v2.0` | 上一版稳定的多智能体架构，10 个事件中 8 个激活，纯 VLM 流水线。 |
| `v1.5.0-legacy` | `legacy/v1.5` | 单体架构。SceneUnderstandingStep（约 30 秒瓶颈）+ 混合检测模式（direct_vlm 并行、logic_chain 串行、scene_tag 零 VLM）+ PostProcessStep 跨事件推断。 |

所有新开发在 `main`（v4.0.0）上进行。

---

## 可选工具系统

框架提供**工具定义层（Tool Schema）** + **工具路由层（Tool Router）**用于可选扩展。默认推理流水线不会自动调用工具，但工具层可用于独立脚本和后续能力扩展，例如基于 YOLO 的目标跟踪。

### 架构概述

```
模型输出 ToolRequest (JSON/Markdown/XML)
        ↓
ToolRouter.route() → 解析并校验参数
        ↓
匹配 ToolDefinition → 执行 handler
        ↓
返回 ToolResponse (success/data/error + 耗时)
```

### 添加新工具（3 步）

#### 第 1 步：实现工具函数

在 `traffic_analyzer/tools/` 下新建文件：

```python
# traffic_analyzer/tools/my_new_tool.py

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def my_new_tool(
    video_path: str,
    param1: float = 0.5,
    param2: Optional[str] = None,
) -> dict:
    """工具实现。返回 dict，会被 ToolResponse 包装。"""
    logger.info(f"my_new_tool: video={video_path}, param1={param1}")
    
    # ... 你的逻辑 ...
    
    return {
        "success": True,
        "result": "something",
        "detail": {"param1": param1, "param2": param2},
    }
```

**关键约束**：
- 参数名使用 snake_case，与注册时的 `ToolParameter.name` 严格一致
- 返回值必须是 JSON 可序列化的类型（dict/list/str/int/float/bool/None）
- 函数可以是同步或异步（async def），Router 会自动识别

#### 第 2 步：注册到工具路由层

编辑 `traffic_analyzer/tools/tool_registry.py`，在 `create_router()` 中添加注册：

```python
def create_router() -> ToolRouter:
    router = ToolRouter()
    
    # 新工具
    _register_my_new_tool(router)
    
    return router
```

然后实现注册函数：

```python
from .my_new_tool import my_new_tool
from .tool_schema import ParameterType, ToolConstraint, ToolDefinition, ToolParameter, ToolReturn

def _register_my_new_tool(router: ToolRouter) -> None:
    definition = ToolDefinition(
        name="my_new_tool",  # 模型用此名称调用
        description="工具功能的详细说明，给模型看的，至少10个字。说明工具做什么、适用场景、输入输出。",
        parameters=[
            ToolParameter(
                name="video_path",
                type=ParameterType.STRING,
                description="输入视频文件的绝对路径（容器内路径）",
                constraints=ToolConstraint(
                    required=True,
                    pattern=r"^/.*\.(mp4|avi|mov|mkv)$",
                ),
            ),
            ToolParameter(
                name="param1",
                type=ParameterType.FLOAT,
                description="参数1的详细说明",
                constraints=ToolConstraint(
                    required=False,
                    min_value=0.0,
                    max_value=1.0,
                ),
                default=0.5,
            ),
            ToolParameter(
                name="param2",
                type=ParameterType.STRING,
                description="参数2的详细说明",
                constraints=ToolConstraint(required=False),
                default=None,
            ),
        ],
        returns=ToolReturn(
            type=ParameterType.OBJECT,
            description="返回结果的详细说明，帮助模型理解如何使用返回数据",
        ),
    )
    
    router.register(definition, my_new_tool)
    logger.info("my_new_tool 注册完成")
```

**支持的参数约束**：

| 约束 | 说明 | 适用类型 |
|---|---|---|
| `required` | 是否必填 | 全部 |
| `min_value` / `max_value` | 数值范围 | integer, float |
| `min_length` / `max_length` | 长度限制 | string, array |
| `pattern` | 正则匹配 | string |
| `enum_values` | 枚举值列表 | 全部 |
| `items_type` | 数组元素类型 | array |

#### 第 3 步：编写测试

在 `tests/tools/` 下添加测试：

```python
# tests/tools/test_my_new_tool.py

import pytest
from traffic_analyzer.tools.tool_registry import create_router


def test_my_new_tool_registration():
    """验证工具已注册"""
    router = create_router()
    assert "my_new_tool" in router.list_tools()
    
    # 验证参数定义
    definition = router.get_tool("my_new_tool")
    param_names = [p.name for p in definition.parameters]
    assert "video_path" in param_names


def test_my_new_tool_execution():
    """验证工具可正常执行"""
    router = create_router()
    resp = router.route(
        '{"tool_name": "my_new_tool", "arguments": {"video_path": "/data/test.mp4", "param1": 0.8}}'
    )
    assert resp.success is True
    assert resp.data["success"] is True


def test_my_new_tool_validation_error():
    """验证参数校验生效"""
    router = create_router()
    # 缺少必填参数
    resp = router.route('{"tool_name": "my_new_tool", "arguments": {}}')
    assert resp.success is False
    assert "Missing required parameter" in resp.error
```

运行测试：

```bash
docker-compose exec traffic-agent python3 -m pytest tests/tools/test_my_new_tool.py -v
```

### 模型如何调用工具

专家 Agent 的 Prompt 中会注入可用工具的 JSON Schema（通过 `ToolRouter.get_tool_descriptions(format="json")`）。模型输出如下格式的调用请求：

```json
{
    "tool_name": "my_new_tool",
    "arguments": {
        "video_path": "/data/test_videos/clip.mp4",
        "param1": 0.8
    }
}
```

也支持 Markdown 代码块和 XML 标签格式，Router 会自动解析。

### 工具层文件清单

| 文件 | 说明 |
|---|---|
| `traffic_analyzer/tools/tool_schema.py` | 工具定义层：ToolDefinition, ToolParameter, ToolConstraint, ToolRegistry |
| `traffic_analyzer/tools/tool_router.py` | 工具路由层：ToolRequest, ToolResponse, ToolRouter（同步/异步/批量） |
| `traffic_analyzer/tools/tool_registry.py` | 注册集成：默认 Router 单例，注册所有内置工具 |
| `tests/tools/test_tool_router.py` | 路由层测试：30 项测试覆盖解析/校验/执行/错误处理 |
