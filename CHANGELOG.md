# 更新日志

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
