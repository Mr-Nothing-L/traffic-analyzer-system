# Traffic Analyzer / 交通事件分析框架

基于多模态大模型（VLM）的高速公路监控视频交通事件检测框架，支持 10 类事件识别，输出二进制编码结果。

---

## Features

- **10 类交通事件检测**：违法停车、应急车道占用、交通事故、行人出现、摩托车出现、严重拥堵、道路施工、车辆逆行/倒车、抛洒物、实线变道
- **多提供商 VLM 支持**：Claude、GPT-4o、Gemini、Qwen-VL 等
- **可配置逻辑链**：通过 YAML 配置复杂事件的检测流程（vlm_call → compute → condition → aggregate）
- **CV 轨迹交叉验证**：可选接入外部计算机视觉轨迹数据进行结果校验
- **双路视频采样**：粗采样（1 FPS）用于场景理解，精采样（4 FPS）用于事件细节分析
- **结构化报告输出**：JSON / Markdown 格式，含二进制编码 `{bit_0_bit_1_..._bit_n}`

## Architecture

```
Video Input
    |
    v
[VideoPreprocessor]  -- Two-pass sampling (coarse 1 FPS + precision 4 FPS)
    |
    v
[VLMInferenceEngine] -- Scene understanding + Event detection
    |
    v
[LogicEngine]        -- Configurable logic chains for hard cases
    |
    v
[ExternalAdapter]    -- Optional CV track cross-validation
    |
    v
[ReportGenerator]    -- JSON / Markdown reports with binary encoding
```

## Installation

```bash
pip install pydantic jinja2 pyyaml python-dotenv opencv-python-headless anthropic openai google-generativeai
```

## Configuration

复制并编辑 `.env` 文件：

```bash
# VLM 提供商
VLM_PROVIDER=anthropic

# API 配置
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_BASE_URL=https://api.anthropic.com
# 第三方 API 示例：
# ANTHROPIC_BASE_URL=https://api.kimi.com/coding/

# 视频采样参数
VIDEO_SAMPLE_FPS=1.0
VIDEO_PRECISION_FPS=4.0

# LLM 参数
LLM_MAX_TOKENS=4096
LLM_TEMPERATURE=0.2
```

> **安全提示**：`.env` 文件已加入 `.gitignore`，请勿提交到版本控制。

## Usage

### 验证配置

```bash
python -m traffic_analyzer validate-config
```

### 分析视频

```bash
# JSON 输出（默认）
python -m traffic_analyzer analyze --video path/to/video.mp4

# Markdown 报告
python -m traffic_analyzer analyze --video video.mp4 --format markdown --output report.md

# 结合 CV 轨迹交叉验证
python -m traffic_analyzer analyze --video video.mp4 --cv-tracks tracks.json
```

### 分析流水线（7 Steps）

分析执行时日志输出的 7 个步骤：

| Step | 说明 |
|:----:|:-----|
| 1/7 | **视频预处理** — 双路采样（粗采样 1 FPS + 精采样 4 FPS）|
| 2/7 | **场景理解** — VLM 分析道路结构、车流方向、天气光照等 |
| 3/7 | **加载 CV 轨迹** — 如有外部轨迹数据则加载（可选）|
| 4/7 | **事件检测** — 对 10 类事件逐一检测（direct_vlm / logic_chain）|
| 5/7 | **后处理推断** — 根据规则推断隐含事件（如违法停车→应急车道占用）|
| 6/7 | **交叉验证** — 用 CV 轨迹验证 VLM 检测结果（可选）|
| 7/7 | **生成报告** — 输出 JSON / Markdown 报告及二进制编码 |

### 分析速度

- **22 秒视频**端到端分析约 **3–4 分钟**
- 瓶颈：串行 VLM 调用（场景理解 + 事件检测）

## Event Categories

| Bit | Code | Event (EN) | Event (ZH) | Mode |
|:---:|:----:|:-----------|:-----------|:-----|
| 0 | A | Illegal Parking | 违法停车 | direct_vlm |
| 1 | B | Emergency Lane Occupancy | 应急车道占用 | logic_chain |
| 2 | C | Traffic Accident | 交通事故 | direct_vlm |
| 3 | D | Person Presence | 高速公路行人出现 | direct_vlm |
| 4 | E | Motorcycle Presence | 摩托车出现 | direct_vlm |
| 5 | F | Heavy Congestion | 严重拥堵 | direct_vlm |
| 6 | G | Road Construction | 道路施工 | direct_vlm |
| 7 | H | Vehicle Reversing | 车辆逆行/倒车 | logic_chain |
| 8 | J | Thrown Objects | 抛洒物 | direct_vlm |
| 9 | K | Lane Change over Solid Line | 实线变道 | logic_chain |

Binary encoding: `{1_0_1_0_0_0_0_0_1_0}` → events 0, 2, 8 detected.

## Logic Chain Steps

复杂事件通过 `logic_chains.yaml` 中的逻辑链检测，支持的步骤类型：

| Step Type | Description |
|:----------|:------------|
| `vlm_call` | 调用 VLM 进行视觉推理 |
| `compute` | 本地 Python 表达式计算（过滤、聚合等）|
| `condition` | 条件分支，根据表达式结果跳转 |
| `cv_fusion` | 融合外部 CV 轨迹数据 |
| `loop` | 遍历集合并执行子链 |
| `aggregate` | 聚合中间变量生成最终结果 |

变量通过 `${variable.path}` 语法在链中流转。

## Logic Chain Details / 逻辑链详解

当前有 **3 个事件**通过逻辑链检测，每个链的完整流程如下：

### 1. 应急车道占用 (`emergency_lane_occupancy` → event_id 1)

| Step | Type | 说明 |
|:-----|:-----|:-----|
| EL1 | `vlm_call` | 调用 `emergency_lane_location` 模板，定位应急车道区域 |
| EL2 | `vlm_call` | 调用 `emergency_lane_vehicle_tracking` 模板，传入 EL1 的区域结果，追踪进入/占用应急车道的车辆 |
| EL3 | `condition` | 判断 `len(emergency_vehicles) > 0`，有车辆则跳 EL4，否则跳 EL5 |
| EL4 | `compute` | True 分支：构建 `detected=True` 结果 |
| EL5 | `compute` | False 分支：构建 `detected=False` 结果 |

### 2. 车辆逆行/倒车 (`vehicle_reversing` → event_id 7)

| Step | Type | 说明 |
|:-----|:-----|:-----|
| — | `precondition` | **前置条件**：`event_results[0].detected or event_results[1].detected or scene_tags['停车'].startswith('有') or scene_tags['应急车道车辆'].startswith('有')`。只有违法停车或应急车道占用被检测到时，或场景描述标签显示相关事件时，才执行本链 |
| S1 | `vlm_call` | 调用 `direct_reversing_detection` 模板，传入全部 coarse frames。VLM 对每辆可疑车辆执行 **6 步检查**：(1)选对比帧 (2)固定参照物定位变化 (3)车头朝向 (4)车身移动方向 (5)与正常车流对比 (6)车头 vs 车身一致性 |
| S2 | `condition` | 判断 `reversing_result.detected`，无论结果都进入 S3 |
| S3 | `aggregate` | 从 `reversing_result` 提取 `detected`、`vehicles`、`confidence`、`reasoning` 构建最终 `EventResult` |

**倒车检测的 VLM 推理要求**：
- 必须输出具体对比的帧号/时间戳
- 必须使用固定参照物（护栏、标线、锥桶）描述位置变化
- 必须分别写出**车头朝向**和**车身移动方向**
- 必须将移动方向与**正常车流方向**对比
- 即使 `detected=false`，也必须输出详细的分析推理过程

### 3. 实线变道 (`lane_change_solid` → event_id 9)

| Step | Type | 说明 |
|:-----|:-----|:-----|
| LC1 | `vlm_call` | 调用 `lane_marking_identification` 模板，识别所有车道标线并分类为 solid / dashed |
| LC2 | `vlm_call` | 调用 `lane_change_tracking` 模板，传入 LC1 的标线结果，追踪车辆变道行为 |
| LC3 | `compute` | 执行 Python 函数 `filter_solid()`，过滤出跨越 **solid** 标线的变道违规记录 |
| LC4 | `aggregate` | 聚合违规记录构建最终 `EventResult` |

## Post-Processing Rules / 后处理规则

所有事件先由 VLM 直接检测或逻辑链检测，然后通过后处理做**兜底推断**和**冲突修正**：

| 规则 | 作用 |
|:-----|:-----|
| Rule 1 | 违法停车实例若在应急车道/路肩 → 推断应急车道占用 |
| Rule 2 | `pedestrian_present` / `non_motor_vehicle_present` / `thrown_object_present` bool 字段为 `False` → 强制标记对应事件未检测；为 `True` → 推断检测到 |
| Rule 3 | 解析 `scene_description` 中的结构化标签 `{类别：内容}`。标签为 `有` 且**直接检测未检测到**时兜底推断；标签为 `无` 时**不覆盖**已有的正面检测结果 |

**关键原则**：场景描述的结构化标签只做**兜底补充**，优先级低于直接检测 / 逻辑链结果。

## Scene Description Format / 场景描述结构化格式

`scene_understanding` 要求 VLM 在 `scene_description` 中使用统一的 `{类别：内容}` 标签：

```
{车流方向：左侧道路从底部向顶部行驶（从近到远），右侧道路从顶部向底部行驶（从远到近）}
{行人：无}
{应急车道车辆：有，右侧道路应急车道有工程车作业}
{停车：无}
{工程车：有，右侧道路有带警示灯的工程作业车辆}
{非机动车：无}
{交通事故：无}
```

- `无` = 该事件不存在
- `有，...` = 该事件存在，后面跟具体描述
- 后处理通过正则解析这些标签，不再做关键词匹配

## Analysis Process in Report / 报告中的分析过程

所有逻辑链事件（无论最终是否检测到）都会在报告中输出完整的**分析过程**：

- **condition 步骤**：显示条件表达式和判断结果（`true/false`）及跳转目标
- **aggregate 步骤**：显示聚合了哪些字段及其值
- **VLM 推理**：即使 `detected=false`，也会显示 VLM 的详细推理过程（如对比了哪些帧、参照物变化、车头朝向等）

示例（未检测到倒车）：

```markdown
#### 分析过程
- Step S1 (VLM): success=True, tokens=1150
- Step S2 (Condition): expr=`reversing_result.get('detected', False)` -> false (next=S3)
- Step S3 (Aggregate): mapped 4 keys -> detected=False, instances=[], confidence=0.0, reasoning="..."
- VLM 推理: 检查了右侧应急车道的工程车，对比第1帧和第8帧，该车相对于交通锥的位置向画面下方移动，与正常车流方向一致，因此不是倒车。
- 最终结果: detected=False, confidence=0.00, instances=0
```

## Project Structure

```
traffic_analyzer/
├── config/                    # YAML 配置
│   ├── event_categories.yaml  # 事件类别定义
│   ├── logic_chains.yaml      # 逻辑链定义
│   └── prompt_templates.yaml  # 提示词模板
├── core/                      # 核心模块
│   ├── config_manager.py
│   ├── video_preprocessor.py  # 视频预处理（双路采样）
│   ├── vlm_engine.py          # 多提供商 VLM 调用
│   ├── logic_engine.py        # 逻辑链执行引擎
│   ├── report_generator.py    # 报告生成
│   └── external_adapter.py    # CV 轨迹融合
├── orchestrator/
│   └── analysis_orchestrator.py  # 流水线编排
├── models/
│   └── schemas.py             # Pydantic 数据模型
├── cli.py                     # 命令行入口
└── main.py                    # 程序入口
```

## Testing

```bash
python -m pytest traffic_analyzer/tests/ -v
```

## License

MIT
