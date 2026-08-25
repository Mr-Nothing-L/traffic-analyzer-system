# 纯 TS agent 运行时 + 统一对话架构

## 背景

闭源 API 时代,所用模型服务不提供可用的工具调用(function calling)能力,对话式分析只能靠「单次大 prompt 流水线」:一次性把系统指令、事件定义、抽帧结果全部塞进一次调用,要求模型直接产出结构化 JSON。这条路线有两个已知局限:

- **输出不可控**:模型不保证产出合法 JSON,代码层需要手写 JSON 修复、重试与降级回填等补偿逻辑(见 [ADR-0003](0003-three-layer-review-architecture.md) 影响部分对裁决 JSON 补偿的描述)。
- **交互不可编排**:模型无法主动调用工具——不能自己决定抽哪些帧、看哪段视频,多步推理链无法落地。

`scripts/agent_spike.py` 的 spike 验证改变了这个前提:本地 vLLM 部署的 `qwen3.8-27b-fp8`(OpenAI 兼容端点,启动参数 `--enable-auto-tool-choice --tool-call-parser qwen3_xml`)的工具调用能力已实测可用。spike 用三个用例验证(报告见 `output/agent_spike_report.json`):Q1(应调工具)实际发起 4 次调用,其中一次 `draw_boxes` 参数类型错误由模型在下一轮自行纠正;Q2(多工具链)3 次调用全部成功;Q3(常识问答)正确地一次工具都没调。结论:工具调用可靠,agent 化编排可行。

## 决策

1. **纯 TypeScript agent 运行时**(`agent/`):新建独立 Node 服务,承载 agent loop、工具注册与调度、权限、沙盒,通过 HTTP + SSE 对外服务(`POST /chat`、`POST /approval`、会话管理路由),由 FastAPI 的 `web/agentproxy/` 反向代理到 `/api/agent/*` 并在 web 层 startup 时拉起。

2. **vendor kosong,不自研 LLM 抽象层**:`agent/src/kosong/` 复制自 MoonshotAI/kimi-code 的 `packages/kosong`(v0.5.5,MIT,保留 LICENSE 与 `VENDORED.md` 出处说明)。kosong 未发布 npm,故采用 vendor 方式;它提供协议无关的 Message/Tool 模型与 OpenAI 兼容 provider 的流式转换,恰好覆盖所需,不值得自研。

3. **权限责任链三模式**:`PermissionMode = 'yolo' | 'manual' | 'auto'`(`agent/src/permissions/`),policy 责任链首个命中赢,返回 approve / deny / ask;工具通过契约先声明资源访问(accesses),权限链与并发调度共同消费这份元数据。

4. **沙盒硬边界独立于权限链**:工作区路径越界(`PATH_OUTSIDE_WORKSPACE`)、敏感文件(`PATH_SENSITIVE`)等走不可豁免的硬 veto(`agent/src/sandbox/path-access.ts`),不进入权限链——任何模式(包括 yolo)都不能批准越界。

5. **Python 退为工具服务 + 批量流水线**:`traffic_analyzer/toolserver/`(默认 127.0.0.1:8601,`--workspace` 必填、越界 403)把 video_meta / extract_frames / draw_boxes 等 CV 能力暴露为本地 HTTP 端点,TS 工具层只做 HTTP client;批量推理仍走既有 Python 流水线。

6. **统一对话取代旧快速对话**:agent 运行时为唯一对话后端,旧 Python `web/chat/` 快速对话已删除;系统 prompt 统一为 `agent/prompts/chat_system.md`(问答 + 检测双能力,正式检测必须 `submit_detection` 收尾);会话持久化到 `<workspace>/.agent/sessions.db`。

## 备选方案与拒绝理由

- **Python 内嵌 agent loop**:在现有 Python 代码里直接写 agent 循环。拒绝理由:Python 侧即将瘦身,继续在其上叠加交互层会加重遗留负担;且调研对象 kimi-code 的 loop/权限/沙盒实现是 TS,照搬成熟实现比在 Python 里重造一套验证成本更低。
- **保留两套对话**(旧快速对话 + 新 agent 对话并存)。拒绝理由:两个后端意味着两份会话模型、两套前端视图与双倍的维护面;旧快速对话的能力是 agent 对话的真子集,保留它没有收益。
- **自研 LLM 抽象层**。拒绝理由:kosong 已覆盖消息模型、流式 delta 转换与 tool call 派发,自研只是重复劳动;vendor 方式还保留了按需同步上游修复的通道。

## 后果

**正面**:

- **工具化**:模型可自主编排 video_meta / extract_frames / draw_boxes 等工具,多步推理链落地,取代单次大 prompt。
- **权限与沙盒**:三模式权限链 + 不可豁免的沙盒硬边界,交互式分析的文件/脚本操作有了明确安全模型。
- **结构化输出保证**:`submit_detection` 契约工具让正式检测结果以工具参数形式产出,11 位编码 + 报告由代码侧构造,不再依赖手写 JSON 修复。

**负面**:

- **双语言运行时**:TS agent 服务 + Python toolserver/web 两套进程,运维与排错成本高于单语言部署。
- **kosong vendor 的同步责任**:上游修复与改进不会自动流入,需要人工跟踪 kimi-code 仓库并手动同步(见 `agent/src/kosong/VENDORED.md`)。
- **会话恢复复杂度**:会话按工作区分库存储(`<workspace>/.agent/sessions.db`),agent server 启动时需从配置的多个 workspace 逐个恢复历史 session,跨工作区的会话语义与恢复边界比单库方案复杂。
