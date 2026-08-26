# agent/ — TypeScript agent 运行时

交通事件检测的统一对话与检测后端:纯 TypeScript 实现的 agent 运行时(架构参考
MoonshotAI/kimi-code),以 HTTP + SSE 服务形式运行,替代旧的「闭源 API + 单次
大 prompt」Python 对话流水线。问答与正式检测共用同一入口,正式检测必须以
`submit_detection` 工具调用收尾(结构化输出契约)。

## 目录结构

```
agent/
├── src/
│   ├── kosong/        # vendored 的 LLM 抽象层(provider/message/tool/generate)
│   ├── llm/           # 从 .env 构造 ChatProvider(provider.ts / env.ts)
│   ├── loop/          # agent 主循环(agentLoop)、上下文压缩(compaction/summarize)
│   ├── tools/         # 工具注册表、调度器与内置工具(builtin/)
│   ├── permissions/   # 权限门(gate/policies)与审批服务(approval)
│   ├── sandbox/       # 路径沙盒(path-access):文件/脚本工具限定在工作区内
│   └── server/        # HTTP + SSE 服务(app.ts)、入口(main.ts)、会话持久化
├── prompts/           # 系统 prompt 资产
├── config/            # 工具清单与结构化输出契约
├── package.json
└── vitest.config.ts
```

### kosong(vendored 依赖)

`src/kosong/` 原样 vendor 自
[MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code/tree/main/packages/kosong)
的 `packages/kosong`(v0.5.5,MIT,见该目录 `LICENSE` 与 `VENDORED.md`),
提供 `ChatProvider` 抽象、`Message`/`ContentPart` 类型与流式 `generate`。
`package.json` 的 `imports` 把 `#/*` 映射到该目录。不要随手改;需要时从上游
同步。

### 各模块职责

- `llm/`:读仓库根 `traffic_analyzer/config/.env` 的 `LLM_PROVIDER_*` 配置构造
  provider。当前仅支持 OpenAI 兼容协议(aliyun / vllm / openai 等,走
  `OpenAILegacyChatProvider` chat-completions 流式);anthropic/google 原生
  协议尚未接入,配置了会抛错。
- `loop/`:多步「generate → 工具调用 → 回灌消息」主循环;按真实 usage 跟踪
  上下文用量,达到窗口 85% 自动压缩(优先 LLM 摘要,失败回退占位替换)。
- `tools/`:8 个内置工具——`video_meta` / `extract_frames` / `draw_boxes` /
  `load_video`(经 HTTP 调 Python 工具服务)、`read_file` / `write_file` /
  `run_script`(沙盒内)、`submit_detection`(结构化检测输出,即停止信号);
  description 与 parameters(模型可见 JSON Schema)来自 `config/toolset.json`。
  另有 `spawn_subagent` 由 server 组装时闭包注入。
- `permissions/`:三档权限模式 `manual` / `auto` / `yolo`;写类工具按策略
  裁决,需审批时经 SSE `approval_request` 事件挂起等待 `/approval` 回执。
- `sandbox/`:路径安全——文件与脚本工具的所有路径必须 resolve 在 session
  工作区(及显式附加目录)内,越界拒绝。
- `server/`:`node:http` 手写的 HTTP + SSE 服务,无新增依赖。会话持久化在
  `<workspaceDir>/.agent/sessions.db`(node:sqlite),时间线条目随 SSE 流
  累积落盘。路由见 `src/server/app.ts` 顶部注释。

## 如何开发

```bash
cd agent
npm install          # 安装依赖
npx vitest run       # 跑全部测试(等同 npm test;mock LLM,不依赖真实服务)
npm run typecheck    # tsc --noEmit 类型检查
```

## 如何独立运行

```bash
cd agent
npm run serve        # tsx src/server/main.ts,默认 http://127.0.0.1:8602
```

独立运行的两个前置依赖:

1. **LLM provider 配置**:读仓库根 `traffic_analyzer/config/.env` 的
   `LLM_PROVIDER_0_*`(primary),必须是 OpenAI 兼容协议(本地 vLLM 或
   aliyun 均可)。缺配置启动后首次对话时报错。
2. **Python 工具服务**(视频工具的后端):

   ```bash
   python3 -m traffic_analyzer.toolserver --workspace <工作区目录> --port 8601
   ```

   agent 经 `TOOLSERVER_URL`(默认 `http://127.0.0.1:8601`)访问;端口不一致
   时设 `TOOLSERVER_URL` 指向实际地址。

常用环境变量(默认值与完整清单见 `traffic_analyzer/config/.env.example`):
`AGENT_PORT`(8602)、`AGENT_HOST`(127.0.0.1)、`AGENT_CONTEXT_TOKENS`
(262144)、`AGENT_MAX_TOKENS`(兜底 16384)、`AGENT_RESTORE_WORKSPACES`
(逗号分隔的工作区目录,启动时恢复磁盘历史会话)。

## 与 web 层的关系

正常使用不需要手动起本服务:FastAPI web 层(`traffic_analyzer/web/agentproxy/`)
在 startup 时以子进程自动拉起工具服务 + agent server(`npx tsx
src/server/main.ts`,cwd 为本目录),并把 `/api/agent/*` 反向代理过来(SSE
透传)。`AGENT_RUNTIME_ENABLE=0` 可整体关闭;端口被占用时降级为
`port_occupied` 并在 `/api/agent/health` 报告。工作区切换时 web 层会旁路调用
`POST /workspaces/restore` 恢复该工作区的磁盘历史会话。

## prompts/ 与 config/ 资产

- `prompts/chat_system.md`:统一对话系统 prompt(问答 + 检测双能力,模型自主
  判断意图;正式检测必须 `submit_detection` 收尾)。这是 server 默认加载的主
  prompt,缺失即启动失败。事件定义摘要/裁决规则段不手抄:prompt 中放置
  `{{EVENT_DEFINITIONS}}`、`{{ADJUDICATION_RULES}}`、`{{ACTIVE_EVENT_COUNT}}`、
  `{{ACTIVE_EVENT_ID_LIST}}` 占位符,启动时用 `config/event_contract.json` 渲染。
- `config/event_contract.json`:事件契约生成物(活跃事件编号、名称、定义、
  标注边界、裁决规则、编码位宽),由仓库根
  `python3 scripts/gen_agent_event_contract.py` 从
  `traffic_analyzer/config/event_categories.yaml` 与 `annotation_spec.yaml`
  生成,勿手改;`submit_detection` 的活跃事件枚举/编码位宽从它派生。
  pytest(`traffic_analyzer/tests/test_agent_event_contract.py`)用 `--check`
  守护生成物与 YAML 同步。
- `config/toolset.json`:给模型看的工具清单;每个工具的 description 与
  parameters(模型可见 JSON Schema)在本文件维护,`registerBuiltinTools`
  启动时加载并原样发给 provider——改工具描述/参数 schema 改这里,不改代码;
  缺条目或缺 description 会启动失败。parameters 支持
  `{"$ref": "./xxx.json"}` 相对引用(从本目录解析)。帧数/体积上限的唯一
  执法者是 Python toolserver,本文件的限额数字须与其一致(pytest 守护)。
- `config/submit_detection.schema.json`:`submit_detection` 参数的 JSON Schema
  (11 位事件编码 + 证据 + 报告的结构化契约),toolset.json 以 `$ref` 引用,
  注册时内联;event_id 枚举与编码位宽在加载时由 event_contract.json 注入
  (文件中的静态值为文档参考,漂移由 vitest 守护)。
