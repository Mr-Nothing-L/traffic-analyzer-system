# Traffic Analyzer / 交通事件分析系统

基于多模态大模型（VLM）的高速公路监控视频交通事件检测框架，支持 **10 类事件识别**，输出二进制编码结果。所有事件检测模式、模板、推断规则均通过 YAML 配置，新增事件无需修改代码。

---

## 支持的事件

| ID | 事件名称 | 检测模式 | 说明 |
|---|---|---|---|
| 0 | 违法停车 | `direct_vlm` | 单次 VLM 调用直接检测 |
| 1 | 应急车道占用 | `scene_tag` | 从场景描述标签推断，零 VLM 调用 |
| 2 | 交通事故 | `logic_chain` | 多步逻辑链，基于场景标签解析 |
| 3 | 高速公路行人出现 | `scene_tag` | 从 `pedestrian_present` 布尔字段推断 |
| 4 | 摩托车出现 | `scene_tag` | 从 `non_motor_vehicle_present` 布尔字段推断 |
| 5 | 严重拥堵 | `direct_vlm` | 单次 VLM 调用直接检测 |
| 6 | 道路施工 | `direct_vlm` | 单次 VLM 调用直接检测 |
| 7 | 车辆逆行/倒车 | `logic_chain` | 多步逻辑链，帧对比分析 |
| 8 | 抛洒物 | `direct_vlm` | 单次 VLM 调用直接检测 |
| 9 | 实线变道 | `logic_chain` | 多步逻辑链，车道线识别+变道追踪 |

---

## 三种检测模式

### 1. `direct_vlm` — 直接 VLM 检测

为每个事件配置专用 Prompt 模板，单次 VLM 调用完成检测。适合需要直接视觉判断的事件。

- **配置项**: `prompt_template_id` — 引用 `prompt_templates.yaml` 中的模板
- **优点**: 简单直接
- **缺点**: 可能误判（如将工程车误判为事故）

### 2. `logic_chain` — 多步逻辑链检测

通过 YAML 定义多步骤检测流程（`vlm_call` → `compute` → `condition` → `aggregate`）。适合需要多步推理的复杂事件。

- **配置项**: `logic_chain_id` — 引用 `logic_chains.yaml` 中的逻辑链
- **示例**: 逆行检测使用 3 步链（VLM 帧对比 → 条件判断 → 结果聚合）
- **优点**: 结构化推理，可融入先验知识
- **缺点**: 更多 VLM 调用，延迟更高

### 3. `scene_tag` — 场景标签推断（零 VLM 调用）

完全不调用 VLM，直接从 `scene_understanding` 的结构化输出推断结果。

- **配置项**:
  - `scene_boolean_field` — `SceneInfo` 布尔字段名（如 `pedestrian_present`）
  - `scene_tag_key` — `scene_description` 中的标签键（如 `应急车道车辆`）
- **优点**: 零额外 VLM 调用，最快，最可靠
- **缺点**: 仅适用于可从场景整体分析确定的事件

---

## 配置化设计

### 事件配置 (`event_categories.yaml`)

每个事件可独立配置检测模式和相关参数：

```yaml
- event_id: 1
  name: "Emergency Lane Occupancy"
  name_zh: "应急车道占用"
  detection_mode: "scene_tag"
  scene_tag_key: "应急车道车辆"  # 从 scene_description 标签推断
  is_active: true
```

#### 关闭某个事件检测

将 `is_active` 设为 `false` 即可关闭该事件的检测（保留二进制编码位，节省 VLM 调用）：

```yaml
- event_id: 9
  name: "Lane Change over Solid Line"
  name_zh: "实线变道"
  # ...
  is_active: false   # 关闭此事件检测
```

**注意**：不要直接注释掉事件定义，否则二进制编码位数会改变，影响下游解析。使用 `is_active: false` 是正确做法。

### 跨事件推断规则 (`cross_event_inference_rules`)

支持在 YAML 中配置跨事件推断，无需改代码：

```yaml
cross_event_inference_rules:
  - rule_id: "parking_to_emergency"
    target_event_id: 1      # 推断目标：应急车道占用
    source_event_id: 0      # 源事件：违法停车
    source_description_keywords: ["shoulder", "emergency", "路肩", "应急"]
    confidence_multiplier: 0.9
```

### Prompt 模板 (`prompt_templates.yaml`)

所有 VLM 调用的 Prompt 集中管理，支持 Jinja2 变量替换。`direct_vlm` 事件通过 `prompt_template_id` 引用模板。

---

## 分析流程

```
视频输入
    |
    v
1. 视频预处理
   - 场景理解帧提取：从整个视频均匀选取 30 帧
   - 精确帧提取：自适应采样关键时刻（4 FPS）
    |
    v
2. 场景理解 (scene_understanding)
   - 单次综合 VLM 调用，输入 30 帧均匀分布帧
   - 输出结构化信息：道路结构、车流方向、天气、
     pedestrian_present、non_motor_vehicle_present、
     scene_description 标签（{类别：状态}格式）
    |
    v
3. 事件检测（按事件类别）
   - direct_vlm: 使用事件专用模板单次 VLM 调用
   - logic_chain: 执行配置的多步逻辑链
   - scene_tag: 直接从场景理解输出推断
    |
    v
4. 后处理
   - 跨事件推断（配置的推断规则）
   - 布尔字段推断（scene_tag 事件）
   - 结构化标签推断（scene_tag 事件）
    |
    v
5. 报告生成
   - Markdown 报告（人工可读）
   - 二进制编码 {bit_0_bit_1_..._bit_n}
```

---

## 项目结构

```
traffic_analyzer/
├── config/
│   ├── event_categories.yaml      # 事件定义、检测模式、推断配置
│   ├── logic_chains.yaml          # 多步逻辑链定义
│   └── prompt_templates.yaml      # VLM Prompt 模板库
├── core/
│   ├── config_manager.py          # 配置加载、验证、热重载
│   ├── logic_engine.py            # 逻辑链执行引擎
│   ├── report_generator.py        # 报告生成
│   ├── video_preprocessor.py      # 视频帧提取（coarse + precision）
│   └── vlm_engine.py              # VLM 调用封装（支持多提供商）
├── models/
│   └── schemas.py                 # Pydantic 数据模型
├── orchestrator/
│   └── analysis_orchestrator.py   # 分析流程编排器
└── config/
    └── .env                       # LLM 提供商配置（API Key 等）
```

---

## 快速开始

```bash
# 配置 LLM 提供商
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# 编辑 .env，设置 API Key 和模型

# 运行分析
python3 -c "
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator
orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)
"
```

---

## 支持的 VLM 提供商

- **Anthropic** (Claude) — 默认推荐
- **Google** (Gemini)
- **Aliyun** (通义千问)

在 `.env` 中配置提供商和 API Key。
