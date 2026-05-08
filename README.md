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
   - 场景理解帧提取：前 5 秒 @ 2 FPS（10 帧，0.5s 间隔）
   - 精确帧提取：自适应采样关键时刻（4 FPS）
    |
    v
2. 场景理解 (scene_understanding)
   - 单次综合 VLM 调用，输入 10 帧密集帧
   - 基于规则的方向判断（双向+隔离带 → 默认左来右去）
   - 输出结构化信息：道路结构、车流方向、天气、
     pedestrian_present、non_motor_vehicle_present、
     scene_description 标签（{类别：状态}格式）
    |
    v
3. 事件检测（并行 + 串行）
   - direct_vlm (4 个事件): 并行批量 VLM 调用
   - logic_chain: 执行配置的多步逻辑链（串行）
   - scene_tag: 直接从场景理解输出推断（零 VLM 调用）
    |
    v
4. 后处理
   - 跨事件推断（配置的推断规则）
   - 布尔字段推断（scene_tag 事件）
   - 结构化标签推断（scene_tag 事件）
    |
    v
5. 报告生成
   - Markdown 报告（人工可读，含每步耗时分析）
   - 二进制编码 {bit_0_bit_1_..._bit_n}
```

---

## 关键优化特性

### 1. 并行 direct_vlm 检测

4 个独立的 `direct_vlm` 事件通过 `ThreadPoolExecutor` **并发执行**，将串行的 ~200s 压缩到 ~50s。

### 2. 密集帧场景理解

`scene_understanding` 使用**前 5 秒 @ 2 FPS**（10 帧，0.5s 间隔），而非全视频均匀采样：
- 车辆位移更小，匹配更准确
- 不受视频总长度影响

### 3. 基于规则的方向判断

删除容易出错的 6 步运动分析，改用**规则默认值**：
- **双向主路 + 隔离带** → 左侧来向（toward_bottom），右侧去向（toward_top），置信度 1.0
- **匝道** → 与主路同向，置信度 0.9
- **单车道** → 语义判断（车辆大小变化），置信度 0.6-0.8

scene_understanding 耗时从 ~200s 降至 ~30s。

### 4. 像素位移估计（倒车检测）

倒车检测 Prompt 要求 VLM 输出像素级位移估计：
- 以画面百分比估计特征点坐标变化
- 位移 > 2% 对角线 → "significant"
- 覆盖 VLM "看起来没动" 的主观误判

### 5. 可调帧数（`--min-frames`）

通过 CLI 参数控制所有 VLM 调用的输入帧数：

```bash
python3 -m traffic_analyzer analyze \
  --video video.mp4 \
  --min-frames 10        # 最少 10 帧，更快但可能欠采样
```

默认 30 帧。降低帧数可显著提速（尤其 scene_understanding），但可能影响精度。

### 6. 图像缩放到 720p

上传 VLM 前自动缩放到 720p，减少 ~55% 传输量，避免 API 超时。

### 7. 逐步骤耗时日志

分析结束时输出每步耗时分解：

```
Step timing breakdown:
  preprocessing:       12.50s
  scene_understanding:  28.54s
  event_detection:      54.43s
  post_processing:       0.00s
  report_generation:     0.00s
  TOTAL:               193.12s
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

### 1. 配置 LLM 提供商

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# 编辑 .env，设置 API Key 和模型
```

### 2. 验证配置

```bash
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config
```

### 3. 运行分析

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

### 4. Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)
```

---

## 支持的 VLM 提供商

- **Anthropic** (Claude) — 默认推荐
- **Google** (Gemini)
- **Aliyun** (通义千问)

在 `.env` 中配置提供商和 API Key。
