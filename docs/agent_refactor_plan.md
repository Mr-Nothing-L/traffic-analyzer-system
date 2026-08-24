# Agent 化架构重构计划(纯 TS agent 运行时)

> 关联 goal:将 traffic-analyzer 重构为「纯 TypeScript agent 运行时」架构。
> 基线 commit:`b241ae2`(重构前快照)。模型:本地 vLLM `qwen3.8-27b-fp8` @ `http://10.103.0.6:8003/v1`(OpenAI 兼容,`--enable-auto-tool-choice --tool-call-parser qwen3_xml`)。

## 目标架构

```
┌────────────────────────────────────────────────────┐
│ Web UI — Vue 3 + TS + Naive UI SPA                 │  frontend/
│ agent 对话视图 · 批量推理视图 · 看板/SFT(保留)      │
└───────────────────┬────────────────────────────────┘
                    │ REST /api/* · SSE
┌───────────────────▼────────────────────────────────┐
│ FastAPI web 层(保留,瘦身)                        │  traffic_analyzer/web/
│ auth · workspace · jobs(批量流水线)· SSE 总线     │
│ 新增:/api/agent/* → 反向代理到 Node agent 服务     │
└──────┬────────────────────────────┬────────────────┘
       │ 子进程                      │ HTTP(本地回环)
┌──────▼───────────────┐   ┌────────▼─────────────────┐
│ Python 批量流水线     │   │ TS agent 运行时(新增)   │  agent/
│ (保留/精简,跑批量)   │   │ loop · tools · 权限 · 沙盒│
└──────────────────────┘   └────────┬─────────────────┘
                                    │ HTTP 工具调用
                          ┌─────────▼─────────────────┐
                          │ Python 工具服务(新增薄层) │  traffic_analyzer/toolserver/
                          │ video_meta/extract_frames/ │
                          │ draw_boxes/...(复用 CV 代码)│
                          └───────────────────────────┘
```

## 模块拆解

### 1. `agent/` — TypeScript agent 运行时(核心新模块)

**kimi-code 调研结论**(2026-08-24,调研报告要点):kimi-code = pnpm monorepo,核心在 `packages/agent-core-v2`(loop/工具/权限)+ `packages/kosong`(独立 LLM 抽象层,未发布 npm)。kosong 因未发布 npm,采用 **vendor 方式**复制源码进 `agent/`(MIT,保留 LICENSE 与出处注释)。不照搬:v2 的 DI 容器、事件溯源状态、wire 持久化、feature/plugin 体系(过重)。

- `agent/src/kosong/` — **vendor 自 kimi-code**:协议无关 Message/Tool 模型、`generate()` 单次生成器(交错 tool call 路由、tool call 延迟到流结束派发)、OpenAI 兼容 provider(chat-completions 流式 delta 转换、tool_call_id 规整)。
- `agent/src/llm/` — provider 配置适配:从 `traffic_analyzer/config/.env` 的 `LLM_PROVIDER_0_*` 读取(aliyun = OpenAI 兼容),构造 kosong provider。
- **工具契约**(照搬 `agent-core-v2/src/tool/toolContract.ts`):`Tool = {name, description, parameters}`;`resolveExecution` 返回 `{accesses, approvalRule, execute}`——**工具先声明资源访问**(`ToolAccesses.file('write', path)`),权限链与并发调度都消费这份元数据。
- **权限系统**(照搬责任链模式,`agent-core-v2/docs/Permission.md`):`PermissionMode = 'manual'|'yolo'|'auto'`;policy 责任链首个命中赢,返回 `approve|deny|ask(携带续体)`;yolo 模式物理过滤 ask 节点;会话级批准记忆(`scope:'session'`);**硬约束 ≠ 权限**(沙盒越界等走不可豁免的 veto,不进权限链)。
- **沙盒**(照搬 `agent-core-v2/src/tool/path-access.ts`):`isWithinWorkspace` 前缀判定、`canonicalizePath`、敏感文件清单(`.env`/私钥/credentials,豁免 `.example/.sample/.template` 与 `.pub`);越界抛 `PATH_OUTSIDE_WORKSPACE`(硬 veto)。
- `agent/src/tools/` — 工具注册表(Map 即可,不要 DI)+ 工具"三件套"组织(zod schema + .md 描述 + 实现);同批 tool calls 按 accesses 冲突检测并行调度(照搬 toolScheduler)。首批工具:
  - `video_meta` / `extract_frames` / `draw_boxes`(HTTP 调 Python 工具服务)
  - `submit_detection`(结构化输出契约:11 位编码 + 事件实例 + 报告 → 保证格式化输出)
  - `read_file` / `write_file` / `run_script`(沙盒内,供 agent 复用脚本)
- `agent/src/permissions/` — yolo 模式(全放行)/ approval 模式(逐次确认),对齐 kimi-code 语义;破坏性/越界操作永远需要确认。
- `agent/src/sandbox/` — 工作区边界强制:所有文件路径解析后必须在 workspace 内;脚本存放 `sandbox/` 目录;禁全局环境修改。
- `agent/src/loop/` — agent loop(简化版 Turn/Step):while(tool_calls){ generate → 权限裁决 → 冲突调度执行 → 结果回灌 };`maxStepsPerTurn` 上限;比率触发压缩(triggerRatio 0.85、reserved 50k、只在 user 消息后安全边界切,照搬 `fullCompaction/strategy.ts` 参数)。
- `agent/src/server/` — HTTP + SSE 服务(供 FastAPI 代理):`POST /chat`(SSE 流:delta/tool_call/tool_result/approval_request/done)、`POST /approval/{id}`(审批回执)、`GET /health`。
- `agent/tests/` — vitest,mock LLM server 覆盖 loop/permissions/sandbox/tools。

### 2. `traffic_analyzer/toolserver/` — Python 视频工具服务(薄层)

- 复用 `web/frames.py` 的 `read_video_meta`/`read_frame_jpeg` 与远增强的画框代码,暴露为本地 HTTP 端点(默认 127.0.0.1:8601)。
- TS 工具层只做 HTTP client,CV 全部留在 Python。

### 3. Web 集成

- FastAPI 新增 `/api/agent/*` 反向代理 → Node agent 服务(SSE 透传);agent 服务由 web 层按需拉起(子进程,端口分配)。
- 前端新增 AgentChatView,复用 `stores/chat.ts` 流式框架 + `useEvents.ts`;tool_call 作为新 SSE 事件类型渲染工具调用气泡。
- 批量推理视图保留,走现有 Python 流水线(qwen3 经 aliyun provider 已可用)。

### 4. Python 瘦身(选择性删除)

- 保留:config(事件/YAML)、models(数据结构)、video_preprocessor、web 层、tools 相关 CV 代码、批量推理入口。
- 可删:expert_agent 系列的"单次大 prompt"实现若被 agent 工具化取代(按批量流水线是否还依赖决定;批量模式保留流水线则不删,仅精简)。
- `web/chat/qa.py` 旧快速对话:被 agent 对话取代后删除。

## 阶段与验证

| 阶段 | 内容 | 验证 |
|---|---|---|
| P0 | 基线 commit、codegraph 索引、kimi-code 调研、依赖安装 | 已完成 |
| P1 | agent/ 骨架:llm client + tool registry + loop + permissions + sandbox + vitest | `npm test` 绿(mock LLM) |
| P2 | Python 工具服务 + TS 工具接通真实视频 | 工具级集成测试(真实视频抽帧) |
| P3 | agent 检测编排:系统 prompt + submit_detection 契约 | 真实端到端:演示区视频 → 11 位编码 + 报告 |
| P4 | web/前端集成:代理 + AgentChatView + 批量模式保留 | e2e 冒烟通过 |
| P5 | 瘦身清理 + pytest 全绿 + codegraph sync + 文档更新 | 全量验证 |
| P6 | 重要节点 git commit(用户已授权) | — |

## 输出契约(不变)

11 位二进制编码 `{bit_1_..._bit_11}`(位序=事件编号,位 9 保留恒 0)+ Markdown 报告 + SFT 样本(批量模式)。事件定义仍以 `config/event_categories.yaml` 为准。

## 实施状态(2026-08-24)

- **P0 完成**:基线 commit `b241ae2`、codegraph 索引、kimi-code 调研、依赖安装。
- **P1 完成**:`agent/` 骨架落地(kosong vendored、llm provider、tool registry/scheduler、loop、permissions、sandbox),`npx vitest run` 104 个测试全绿(mock LLM)。
- **P2 完成**:`traffic_analyzer/toolserver/` Python 工具服务(127.0.0.1:8601,`--workspace` 必填、越界 403)+ TS 工具层经 HTTP 接通真实视频。
- **P3 完成**:检测编排(系统 prompt + `submit_detection` 契约)端到端跑通,演示区视频产出 11 位编码 + Markdown 报告。
- **P4 完成**:web/前端集成落地——`web/agentproxy/` 反向代理 `/api/agent/*` 并在 startup 自动拉起 toolserver + agent 服务(`AGENT_RUNTIME_ENABLE=0` 关闭);前端 `/agent` 路由 AgentChatView(权限模式选择、工具气泡、审批卡片、检测结果卡);批量推理视图保留。
- **P5 部分完成(瘦身从简)**:pytest 809 个全绿、文档已更新;旧 `web/chat/` 快速对话~~未删除~~**已删除**(对话统一,见下)。
- **P6 完成**:重要节点 git commit 已做。
- **对话统一(2026-08-24 晚些)**:旧「快速对话」与「Agent 检测」合并为统一对话——agent 运行时(TS)为唯一对话后端,旧 Python `web/chat/` 已删除;agent server 补齐持久化(`<workspace>/.agent/sessions.db`)、`GET /sessions` 列表、`GET /sessions/{id}/history`(entries 时间线)、`DELETE /sessions/{id}` 与 `/chat` 图片附件(dataURL ≤4);系统 prompt 统一为 `agent/prompts/chat_system.md`(问答 + 检测双能力,模型自主判断意图,正式检测必须 `submit_detection` 收尾);web 代理同步透传三个新路由。
