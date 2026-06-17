# 更新日志

## [v4.0.0] 2026-06-16 — 专家 Agent 重构与 event_id=4 远距离 ROI 量化增强

### 核心变更

#### 1. event_id=4（非机动车/摩托车出现）远距离 ROI 增强量化
- **ROI confidence 从离散等级改为连续数值**: `high` / `medium` / `low` 三档改为 `0.0-1.0` 连续置信度
- **增强逻辑重构**: 专家 Agent 根据连续 confidence 阈值决定是否进行 ROI 裁剪放大与二次 VLM 判别
- **Prompt 与配置同步更新**: `prompt_templates.yaml` 和 `report_generator.py` 中相关描述统一改为 0-1 量化指标

#### 2. 专家 Agent（Expert Agent）重构
- 远距离非机动车增强流程与主裁决流程解耦，逻辑更清晰
- ROI 增强结果与 adjudication 层数据对齐方式优化
- 支持事件激活状态（`is_active`）动态控制，当前仅 event_id 3/4/5/6 处于激活状态

#### 3. 工具系统持续完善
- 工具调用链路稳定性提升
- YOLO Track Tool 与专家 Agent 集成细节调优
- 工具配置（`event_categories.yaml`、`prompt_templates.yaml`）随事件逻辑同步更新

#### 4. 测试覆盖
- `traffic_analyzer/tests/test_expert_agent_far_enhancement.py`: event_id=4 远距离增强量化指标测试
- `traffic_analyzer/tests/test_report_generator.py`: 报告生成与量化 confidence 一致性测试

### 关键文件变更

| 文件 | 变更 |
|---|---|
| `traffic_analyzer/core/expert_agent.py` | event_id=4 远距离 ROI 增强逻辑重构，confidence 量化 |
| `traffic_analyzer/core/report_generator.py` | 报告生成适配 0-1 连续 confidence |
| `traffic_analyzer/config/prompt_templates.yaml` | 非机动车/摩托车 ROI 增强 prompt 改为 0-1 量化描述 |
| `traffic_analyzer/tests/test_expert_agent_far_enhancement.py` | 新增/更新量化指标测试 |
| `traffic_analyzer/tests/test_report_generator.py` | 更新报告生成测试 |

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
- **YOLO Track Tool**: 基于 YOLOv8 + ByteTrack 的车辆检测跟踪工具
  - 输出: 带跟踪框的关键帧、位移矢量表、静止车辆判定
  - 配置: `stationary_threshold`（静止阈值）、`conf_threshold`（置信度）

#### 3. 配置驱动集成
- `event_categories.yaml`: 事件加 `tools: ["yolo_track_tool"]` 字段启用工具
- `prompt_templates.yaml`: 模板加 `available_tools` 字段声明可用工具
- 专家 Agent 自动检测工具配置，优先走 Native API，失败 fallback 到常规调用

### Docker 容器化

#### 基础环境
- **CPU 版**: `python:3.11-slim-bookworm` + PyTorch CPU + OpenCV + Ultralytics
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
- `tests/tools/test_expert_agent_tools.py`: 7 项测试覆盖工具调用全流程
- `tests/tools/test_tool_router.py`: 工具路由层单元测试
- `tests/tools/test_yolo_track_tool.py`: YOLO 跟踪工具测试（含 mock 视频）

### 关键文件变更

| 文件 | 变更 |
|---|---|
| `traffic_analyzer/core/expert_agent.py` | +806 行: Native API 工具调用、fallback 逻辑、二次 VLM 调用 |
| `traffic_analyzer/core/vlm_engine.py` | +275 行: `call_with_tools()`、`call_with_tool_results()` |
| `traffic_analyzer/tools/tool_schema.py` | +448 行: 工具定义、Anthropic/OpenAI 格式转换 |
| `traffic_analyzer/tools/tool_router.py` | +533 行: 工具路由、请求解析、执行分发 |
| `traffic_analyzer/tools/yolo_track_tool.py` | +510 行: YOLOv8 检测 + ByteTrack 跟踪 |
| `traffic_analyzer/config/prompt_templates.yaml` | 违停/逆行 prompt 重构，加工具调用说明 |
| `Dockerfile` / `docker-compose.yml` | 容器化配置，host 网络模式 |

### 已知限制

1. **Kimi 模型不支持原生 tool_use**: 当前通过文本解析 `<tool_call>` fallback 解决
2. **逆行检测 adjudication 问题**: 第二次 VLM 返回 `detected=True`，但 adjudication 阶段可能改为 `False`（待排查）
3. **YOLO 首次加载慢**: 模型下载 + 初始化约 30-60s

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
- YOLO 跟踪工具初版
- 专家 Agent 字符串解析 `<tool_call>` 方式
