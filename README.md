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
   - 场景理解帧提取：两段式采样（前 5 秒密集 + 后段均匀），共 20 帧
   - 精确帧提取：自适应采样关键时刻（4 FPS）
    |
    v
2. 场景理解 (scene_understanding)
   - 单次综合 VLM 调用，输入 20 帧（前 5 秒密集 + 后段均匀）
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

### 7. VLM 调用结果缓存

基于图像内容 + Prompt 文本的 SHA-256 哈希缓存，避免重复调用：
- LRU 淘汰策略，默认最多缓存 128 条
- 命中时直接返回缓存结果，零 token 消耗
- 可通过环境变量 `LLM_ENABLE_CACHE=false` 关闭

```python
# 查看缓存统计
stats = vlm_engine.get_usage_stats()
print(f"缓存命中率: {stats['cache_hit_rate']:.1%}")
print(f"缓存节省调用: {stats['cache_hits']}")
```

### 8. 可插拔 PipelineStep 架构

将分析流程拆分为独立的 `PipelineStep` 子类，每个步骤自带重试和回退：
- `SceneUnderstandingStep` — 场景理解（支持 1 次重试 + 空结果回退）
- `EventDetectionStep` — 事件检测（单事件失败不影响其他事件）
- `PostProcessStep` — 后处理（支持回退到原始结果）

步骤级别的失败被隔离，不会导致整个 pipeline 崩溃。

### 9. Prompt 版本管理与 A/B 测试

`prompt_templates.yaml` 支持同一 `template_id` 的多个版本：

```yaml
prompt_templates:
  - template_id: "direct_event_detection"
    name: "Direct Detection v1"
    version: "1.0"
    user_prompt: "..."
  - template_id: "direct_event_detection"
    name: "Direct Detection v2"
    version: "2.0"
    user_prompt: "...改进后的prompt..."
    traffic_percentage: 30   # 30% 流量使用此版本
```

版本选择优先级：
1. 环境变量 `PROMPT_VERSION_direct_event_detection=2.0`
2. A/B 流量分割（`traffic_percentage`）
3. 默认最高版本号

### 10. 逐步骤耗时日志

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
│   └── prompt_templates.yaml      # VLM Prompt 模板库（支持多版本）
├── core/
│   ├── config_manager.py          # 配置加载、验证、热重载
│   ├── logic_engine.py            # 逻辑链执行引擎
│   ├── pipeline_steps.py          # 可插拔分析步骤（重试/回退）
│   ├── report_generator.py        # 报告生成
│   ├── video_preprocessor.py      # 视频帧提取（coarse + precision）
│   └── vlm_engine.py              # VLM 调用封装（支持多提供商 + 缓存）
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

支持的环境变量：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_PROVIDER` | VLM 提供商 (`anthropic`/`google`/`aliyun`) | `anthropic` |
| `LLM_API_KEY` | API Key | - |
| `LLM_MODEL` | 模型名称 | `claude-sonnet-4-6` |
| `LLM_MAX_TOKENS` | 最大输出 token | `4096` |
| `LLM_TEMPERATURE` | 采样温度 | `0.2` |
| `LLM_TIMEOUT` | API 超时（秒） | `120` |
| `LLM_MAX_RETRIES` | 最大重试次数 | `3` |
| `LLM_ENABLE_CACHE` | 启用 VLM 结果缓存 | `true` |
| `LLM_CACHE_MAX_SIZE` | 缓存最大条目数 | `128` |
| `SCENE_UNDERSTANDING_MIN_FRAMES` | 场景理解最少帧数 | `30` |
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

校验内容包括：
- YAML 语法和结构
- 事件类别 → 逻辑链/Prompt 模板的交叉引用
- 跨事件推断规则的源/目标事件有效性
- 分支步骤的 `true_next_step` / `false_next_step` 目标存在性

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

### 5. 批量推理与评估

项目提供两个批量处理脚本，位于 `scripts/` 目录：

#### 批量推理 (`batch_infer.py`)

对目录下的所有视频批量执行分析：

```bash
python3 scripts/batch_infer.py \
  --video-dir ./videos \
  --output-dir ./reports \
  --log-dir ./logs \
  --workers 4 \
  --format markdown \
  --min-frames 30
```

参数说明：

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

脚本会自动匹配视频与已存在的报告，跳过已处理的视频（除非加 `--no-skip-existing`）。

#### 批量评估 (`batch_evaluate.py`)

将推理报告与真实标签对比，输出每类事件的精确率、召回率、F1 分数：

```bash
# 基本用法（从视频文件名提取真实标签）
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_result.json

# 使用独立标注文件
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --gt-mode annotation_file \
  --annotation-file ./annotations.json \
  --output evaluation_result.json

# 单类别模式（只评估 config 中 is_active=true 的事件）
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --single-class \
  --config-dir ./traffic_analyzer/config \
  --output evaluation_result.json
```

参数说明：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--video-dir` / `-v` | 视频目录（用于提取真实标签） | - |
| `--report-dir` / `-r` | 报告目录（`.md` 或 `.json`） | - |
| `--output` | 评估结果输出路径（支持 `.json` / `.md` / `.html`） | `evaluation_result.json` |
| `--gt-mode` | 真实标签来源 (`filename` / `annotation_file`) | `filename` |
| `--annotation-file` | 标注文件路径（JSON 或 CSV） | - |
| `--single-class` | 只评估 `is_active=true` 的事件 | - |
| `--config-dir` / `-c` | 配置目录（配合 `--single-class`） | `./traffic_analyzer/config` |

**单类别模式 (`--single-class`)**：当某些事件被设为 `is_active: false` 时，这些事件会被完全排除在评估指标之外，避免关闭的事件拉低整体分数。

**输出格式**：根据 `--output` 的文件扩展名自动选择：
- `.json` — 原始 JSON 数据，含每事件和每视频指标
- `.md` — Markdown 表格，含事件汇总 + 逐视频详情表（`file://` 可点击链接）
- `.html` — 交互式 HTML 报告（见下文）

评估结果包含：
- 每类事件的 TP / FP / FN / 精确率 / 召回率 / F1
- 宏平均（Macro Average）指标
- 每个视频的预测 vs 真实标签对照表
- Markdown 表格格式直接输出到终端

#### HTML 交互式报告

通过 `--output report.html` 生成交互式 HTML 评估报告：

```bash
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.html \
  --single-class
```

特性：
- 左侧：事件统计表 + 逐视频结果表（支持筛选 ✅/❌）
- 右侧：视频播放器 + Markdown 报告预览面板
- 点击表格行播放视频，点击报告链接预览 Markdown
- 所有数据内联嵌入，使用 `file://` 绝对路径，可直接双击打开，无需 HTTP 服务器

#### 完整批量工作流示例

```bash
# 1. 批量推理（4 并行 worker，保存日志）
python3 scripts/batch_infer.py \
  --video-dir ./测试视频 \
  --output-dir ./output \
  --log-dir ./log \
  --workers 4 \
  --format markdown

# 2. 生成 Markdown 评估报告
python3 scripts/batch_evaluate.py \
  --video-dir ./测试视频 \
  --report-dir ./output \
  --output ./evaluation_report.md \
  --single-class

# 3. 生成 HTML 交互式报告
python3 scripts/batch_evaluate.py \
  --video-dir ./测试视频 \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class
```

---

## 支持的 VLM 提供商

- **Anthropic** (Claude) — 默认推荐
- **Google** (Gemini)
- **Aliyun** (通义千问)

在 `.env` 中配置提供商和 API Key。


## Tool-Call 风格日志输出

运行时会输出类似现代 AI Agent (Cursor / Cline / Claude Code) 的工具调用轨迹日志,例如:

```
[INFO] 14:30:00 🔧 tool_call: video_preprocessor.process(video='clip.mp4')
[INFO] 14:30:03   ↳ result: coarse=20, precision=41 | elapsed=3.0s
[INFO] 14:30:03 🔧 tool_call: vlm_engine.scene_understanding(provider='claude', frames=20)
[INFO] 14:30:31   ↳ result: roads=4, density='normal' | elapsed=28.0s
[INFO] 14:30:31 🔧 tool_call: reasoning_chain.execute(event='E7', steps=4)
[INFO] 14:30:31   🔧 step[1/4]: vlm_call.candidate_extraction(provider='claude')
[INFO] 14:30:39     ↳ result: candidates=2 vehicles | elapsed=8.0s
```

通过环境变量 `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL` 切换粒度:

| 值 | 行为 |
|---|---|
| `off` | 不输出任何 tool_call 日志 |
| `macro` | 仅顶层调用,不打嵌套 step[i/N] |
| `mid` | 顶层 + 嵌套 (**默认**) |
| `fine` | 预留,未来扩展 VLM 单次调用 / schema 校验 |

示例:

```bash
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=off python -m traffic_analyzer ...    # 关闭
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=macro python -m traffic_analyzer ...  # 仅顶层
```

此日志是**纯显示层**,不影响并行/性能/结果,关掉后输出二进制编码与开启时完全一致。
