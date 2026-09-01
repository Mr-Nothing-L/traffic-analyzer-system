/**
 * agent 运行时的 HTTP + SSE 服务(node:http 手写,无新增依赖):
 *
 *   GET    /health                 → {status:'ok'}
 *   POST   /sessions               → {sessionId}(workspaceDir 必填且须为已存在目录)
 *   GET    /sessions               → {sessions:[{id,workspaceDir,mode,title,createdAt,lastActiveAt}]}
 *   GET    /sessions/{id}/history  → {entries:[TimelineEntry]}(渲染友好的时间线)
 *   GET    /sessions/{id}/events?fromSeq=N
 *                                    → {events:[{seq,entry}],inProgress}(断线续传:
 *                                      已落盘 entries 中 seq>N 的部分;inProgress 标记
 *                                      该 session 是否有进行中的轮次;fromSeq 缺省 0,
 *                                      非法 → 400,未知 session → 404)
 *   POST   /sessions/{id}/compact  → {status:'ok',compacted,summarized,beforeTokens,afterTokens}
 *                                      (立即 LLM 摘要压缩该 session 历史,失败回退
 *                                       占位替换,压缩后落盘;进行中 → 409)
 *   POST   /sessions/{id}/recall   {entryIndex} → {status:'ok'}(撤回:删除
 *                                      entries[entryIndex..] 并同步截断 kosong
 *                                      messages;进行中 → 409;越界/非 user → 400)
 *   POST   /sessions/{id}/mode     {mode} → {status:'ok', mode}(切换权限模式
 *                                      manual|auto|yolo,内存+磁盘同步,进行中的
 *                                      轮次下一轮生效;非法 mode → 400)
 *   POST   /sessions/{id}/cancel   → {status:'ok'}(显式终止该 session 进行中
 *                                      的轮次:触发其 AbortController 并把挂起
 *                                      审批以 cancelled 落定——审批 promise 不受
 *                                      abort 信号影响,不取消会拖满审批超时;
 *                                      已完成部分照常增量落盘,loop 以 cancelled
 *                                      收尾;无进行中轮次 → 409 no_active_turn)
 *   POST   /sessions/{id}/steer    {input,videoPath?,images?}
 *                                    → {status:'ok',queued:true}(轮次进行中注入
 *                                      一条 user 消息,下一个 step 边界生效:注入时
 *                                      增量落盘(条目+消息)并经该 session 活跃流发
 *                                      SSE steer 事件(客户端已断则只落盘);轮次结束
 *                                      时仍未消费的 steer 丢弃并发 steer_dropped
 *                                      事件,不带入下一轮(否则会注入在下一轮新
 *                                      用户消息之后,时序颠倒);无进行中轮次 → 409
 *                                      no_active_turn,前端应改发 /chat)
 *   GET    /sessions/{id}/media/{name}
 *                                    → image/jpeg|png(内容寻址的媒体引用文件,
 *                                      见 mediaStore.ts:SSE/条目中的图片 dataURL
 *                                      写盘为 .agent/media/<sha256>.<ext> 后以
 *                                      /sessions/{id}/media/{name} 引用,前端经
 *                                      代理按需加载;文件名白名单校验,缺失 → 404)
 *   POST   /workspaces/restore     {workspaceDir} → {status:'ok',restored:n}
 *                                      (打开该 workspace 的 sessions.db 存储:列表
 *                                       以磁盘为准,会话内容按需懒恢复;幂等,
 *                                       workspaceDir 非已存在目录 → 400,
 *                                       sessions.db 不存在 → restored:0)
 *   DELETE /sessions/{id}          → {status:'ok'}(同时取消挂起审批、删盘,
 *                                      并 GC 不再被任何会话引用的 media 文件,
 *                                      见 mediaGc.ts;GC 失败只记日志不阻断)
 *   POST   /chat                   → SSE 流(text/event-stream,每事件一行 'data: {json}\n\n')
 *   POST   /approval               → 审批回执(见 approvalBridge.ts)
 *
 * 上下文窗口:AGENT_CONTEXT_TOKENS(默认 262144 = 256k)。/chat 每步 generate
 * 后按真实 usage 透传 context_usage 事件并记录 session.lastKnownUsage(GET
 * /sessions 摘要带 usedTokens);上一步用量 ≥ 窗口 × 0.85 时(单轨判定,见
 * compaction.ts 的 isOverContextByUsage),下一步 generate 前自动压缩(优先
 * LLM 摘要替换压缩区,失败回退占位替换)并透传 compaction 事件(带
 * summarized/beforeTokens/afterTokens);手动 /compact 与自动触发共用
 * compactMessagesWithSummary 同一路径。
 *
 * 错误统一 {error:{code,message}};未知 session → 404。同 session 的 /chat
 * 用简单互斥串行,不同 session 并行。provider / tools 均可注入以便测试。
 *
 * 持久化:SessionManager 委托 node:sqlite(<workspaceDir>/.agent/sessions.db);
 * /chat 的 SSE 事件流在转发的同时累积 TimelineEntry(user/assistant/tool/
 * approval/detection),落盘水位与消息水位统一由 TurnPersister 持有
 * (turnPersister.ts):user 条目立即落盘,之后每个 step_done 把累计条目
 * 与 loop 回灌的增量消息同步 append;轮内发生自动压缩时消息立即整体重写
 * (replaceMessages),压缩成果不等轮末——中途崩溃不回退未压缩历史;
 * finalize 兜底落盘剩余条目。SSE 断连不 abort 轮次:loop 跑完照常
 * 落盘,写出错只标记客户端断开;恢复续跑时 SessionManager 对悬挂 tool calls
 * 做尾部修复(见 repair.ts)。POST /chat 支持可选 images(最多
 * 4 张,base64 或 dataURL),转成 kosong image ContentPart 附在该轮 user message。
 *
 * 子代理:每个 session 的 registry 自动注册 spawn_subagent(app.ts 闭包注入
 * provider/gate/systemPrompt,详见 tools/builtin/spawnSubagent.ts);子 loop
 * 事件以 {type:'subagent_event', toolCallId, event} 原样透传 SSE(不落盘,
 * 结论留在对应 tool 条目的 output/note 里)。
 */
import { readFileSync, statSync } from 'node:fs';
import { createServer as createHttpServer, type Server, type ServerResponse } from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import type { ContentPart, Message, ChatProvider } from '../llm/kosong';

import { createProviderFromEnv, resolveFramesThinkingEffort } from '../llm/provider';
import { runAgentLoop, type AgentLoopEvent } from '../loop/agentLoop';
import { createCompactionConfig } from '../loop/compaction';
import { compactMessagesWithSummary } from '../loop/summarize';
import { CallbackApprovalService } from '../permissions/approval';
import { PermissionGate } from '../permissions/gate';
import type { PermissionMode } from '../permissions/types';
import {
  createSubagentListTool,
  createSubagentReportTool,
  loadToolsetEntrySpec,
  registerBuiltinTools,
  renderSystemPrompt,
  ToolserverClient,
} from '../tools/builtin';
import {
  SubagentSemaphore,
  createSpawnSubagentTool,
  createSubagentRunRegistry,
} from '../tools/builtin/spawnSubagent';
import type { DetectionPayload } from '../tools/builtin/submitDetection';
import { ToolRegistry } from '../tools/registry';

import { ApprovalBridge, type ApprovalDecisionInput, type PreviewContent } from './approvalBridge';
import { mediaContentType, mediaUrl, saveMediaFile } from './mediaStore';
import { createRouter, type RequestContext, sendJson, sendError, writeSseEvent, isRecord } from './routes';
import { SessionManager, type Session } from './session';
import type { TimelineEntry } from './storage';
import { TurnPersister } from './turnPersister';

export interface ProviderHandle {
  readonly provider: ChatProvider;
  readonly model: string;
}

export interface AgentServerOptions {
  /** 构造 LLM provider;默认 createProviderFromEnv()(惰性,首次使用时创建)。 */
  readonly providerFactory?: () => ProviderHandle;
  /** 按 session 构造工具注册表;默认 registerBuiltinTools(workspaceDir)。 */
  readonly toolsFactory?: (session: Session) => ToolRegistry;
  /** system prompt;默认读 agent/prompts/chat_system.md 并渲染事件契约占位符。 */
  readonly systemPrompt?: string;
  /** 审批挂起超时(ms),默认 5 分钟。 */
  readonly approvalTimeoutMs?: number;
  /** 上下文窗口(token);默认读 AGENT_CONTEXT_TOKENS,缺省 262144(256k)。 */
  readonly contextTokens?: number;
  /** session idle 过期(ms),默认 2h。 */
  readonly sessionIdleMs?: number;
  /** 过期清扫周期(ms),默认 60s。 */
  readonly sweepIntervalMs?: number;
  /** 启动时从这些 workspace 的 .agent/sessions.db 恢复历史 session。 */
  readonly restoreWorkspaceDirs?: readonly string[];
  /**
   * 工作区登记表路径;若提供,SessionManager 在 list() 前自查恢复其中尚未
   * 打开的工作区。缺省时读环境变量 AGENT_WORKSPACE_REGISTRY_PATH。
   */
  readonly workspaceRegistryPath?: string;
}

export interface AgentServer {
  readonly server: Server;
  readonly sessions: SessionManager;
  close(): Promise<void>;
}

/** steer 排队项:handleSteer 入队(消息按 /chat 同一规则构建),
 * 进行中轮次的下一个 step 边界由 handleChat 的 nextSteer 回调逐条取走。 */
interface QueuedSteer {
  readonly text: string;
  readonly videoPath?: string;
  readonly images: string[];
  readonly message: Message;
}

interface SessionRuntime {
  readonly registry: ToolRegistry;
  readonly gate: PermissionGate;
  readonly bridge: ApprovalBridge;
  /** 同 session 的 /chat 串行锁:true 时有进行中的轮次。 */
  busy: boolean;
  /** 进行中轮次的 AbortController(/cancel 触发;轮次结束清空)。 */
  controller: AbortController | null;
  /** steer 排队的 user 消息(注入后清空;轮次结束时未消费的丢弃并发
   *  steer_dropped 事件,不带入下一轮,避免插话注入在新用户消息之后)。 */
  readonly steerQueue: QueuedSteer[];
}

/**
 * 默认 system prompt:agent/prompts/chat_system.md(统一对话)。文件缺失即
 * 抛错(fail-fast,旧 detect_system.md 回退已删除);加载后用
 * event_contract.json 渲染事件契约占位符({{EVENT_DEFINITIONS}} 等),
 * 事件定义/裁决规则不再手抄在 prompt 里。
 */
export function defaultSystemPrompt(promptsDir?: string): string {
  const dir = promptsDir ?? fileURLToPath(new URL('../../prompts', import.meta.url));
  const template = readFileSync(`${dir}/chat_system.md`, 'utf8');
  return renderSystemPrompt(template);
}

/** POST /chat 单轮图片附件上限。 */
const MAX_IMAGES_PER_TURN = 4;
/** 默认上下文窗口:256k(本地 qwen3.8-27b-fp8 的 max_model_len)。 */
const DEFAULT_CONTEXT_TOKENS = 262_144;

/** 上下文窗口解析:显式 option > AGENT_CONTEXT_TOKENS > 默认 256k;非法值回退默认。 */
function resolveContextTokens(option: number | undefined): number {
  if (option !== undefined && option > 0) return option;
  const raw = process.env.AGENT_CONTEXT_TOKENS;
  const parsed = raw === undefined ? NaN : Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_CONTEXT_TOKENS;
}

const MAX_PREVIEW_BYTES = 32 * 1024;

/** 按工具类型构造审批内容预览。 */
function buildPreview(
  toolName: string,
  argumentsJson: string | null | undefined,
  approvalRule: string,
): PreviewContent | undefined {
  const args = ((): Record<string, unknown> | undefined => {
    if (!argumentsJson) return undefined;
    try {
      return JSON.parse(argumentsJson) as Record<string, unknown>;
    } catch {
      return undefined;
    }
  })();

  const formatArgs = (): string => {
    if (args === undefined) return argumentsJson ?? '(无参数)';
    try {
      return JSON.stringify(args, null, 2);
    } catch {
      return argumentsJson ?? '(无参数)';
    }
  };

  const truncateContent = (content: string): { content: string; truncated: boolean } => {
    const bytes = Buffer.byteLength(content, 'utf8');
    if (bytes <= MAX_PREVIEW_BYTES) return { content, truncated: false };
    const buf = Buffer.from(content, 'utf8');
    return { content: buf.subarray(0, MAX_PREVIEW_BYTES).toString('utf8'), truncated: true };
  };

  if (toolName === 'write_file') {
    const content = args !== undefined && typeof args.content === 'string' ? args.content : '';
    const rawPath = args !== undefined && typeof args.path === 'string' ? args.path : '';
    const language = path.extname(rawPath).slice(1) || 'text';
    const truncated = truncateContent(content);
    return { language, content: truncated.content, truncated: truncated.truncated };
  }

  if (toolName === 'run_script') {
    // approvalRule 形如 run_script(<已沙盒解析的绝对路径>)
    const prefix = 'run_script(';
    let scriptPath = '';
    if (approvalRule.startsWith(prefix) && approvalRule.endsWith(')')) {
      scriptPath = approvalRule.slice(prefix.length, -1);
    }
    if (scriptPath !== '') {
      try {
        const content = readFileSync(scriptPath, 'utf8');
        const language = path.extname(scriptPath).slice(1) || 'text';
        const truncated = truncateContent(content);
        return { language, content: truncated.content, truncated: truncated.truncated };
      } catch {
        // 读取失败回退为格式化参数 JSON
      }
    }
    return { language: 'json', content: formatArgs(), truncated: false };
  }

  return { language: 'json', content: formatArgs(), truncated: false };
}

/** 对未 decision 的 approval 条目,若仍挂起则补 pending 标记与 accesses/preview。 */
function enrichPendingApprovalEntry(
  entry: TimelineEntry,
  bridge: ApprovalBridge | undefined,
): void {
  if (bridge === undefined || entry.kind !== 'approval' || entry.decision !== undefined) return;
  const pending = bridge.getPending(entry.requestId);
  if (pending === undefined) return;
  entry.pending = true;
  if (entry.accesses === undefined || entry.accesses.length === 0) {
    entry.accesses = pending.accesses;
  }
  if (entry.preview === undefined) {
    entry.preview = pending.preview;
  }
}

/**
 * 工具结果传输/落盘裁剪:video part 的 dataURL 可达几十 MB(load_video),
 * SSE 与 sqlite 都承受不起,替换为占位文本;image part 的 dataURL 同样达
 * MB 级(extract_frames/track_suspects/draw_boxes),按内容寻址写盘
 * (<workspace>/.agent/media/<sha256>.<ext>)后替换为 media 引用 URL
 * (/sessions/{id}/media/{name},仍为 image_url part,前端经代理按需加载)。
 * 仅影响 SSE 事件与 entries 落盘,loop 内 messages 仍持原始内容(模型照常
 * 收到真实图片);写盘失败或非白名单图片类型时保留原 part(回退为旧行为)。
 */
function sanitizeToolOutputForTransport(
  output: string | ContentPart[],
  media: { sessionId: string; workspaceDir: string },
): string | ContentPart[] {
  if (!Array.isArray(output)) return output;
  return output.map((part): ContentPart => {
    if (part.type === 'video_url') {
      return { type: 'text', text: '[完整视频已发送给模型,不在此展示]' };
    }
    if (part.type === 'image_url') {
      const url = part.imageUrl.url;
      if (url.startsWith('data:image/')) {
        const name = saveMediaFile(media.workspaceDir, url);
        if (name !== undefined) {
          return { ...part, imageUrl: { ...part.imageUrl, url: mediaUrl(media.sessionId, name) } };
        }
      }
    }
    return part;
  });
}

/**
 * 结构化载荷(submit_detection 的 DetectionPayload)传输/落盘裁剪:逐事件
 * annotated_image(jpeg dataURL)按内容寻址写盘并替换为 media 引用 URL,
 * tool 条目的 payload、detection 事件与 detection 条目共用这一道转换。
 * 非 dataURL(已转换的 URL 引用/旧条目)与解析失败的原值原样保留。
 */
function sanitizeDetectionPayloadForTransport(
  payload: unknown,
  media: { sessionId: string; workspaceDir: string },
): unknown {
  if (!isRecord(payload) || !Array.isArray(payload['events'])) return payload;
  return {
    ...payload,
    events: (payload['events'] as unknown[]).map((event) => {
      if (!isRecord(event) || typeof event['annotated_image'] !== 'string') return event;
      const url = event['annotated_image'];
      if (!url.startsWith('data:image/')) return event;
      const name = saveMediaFile(media.workspaceDir, url);
      return name === undefined ? event : { ...event, annotated_image: mediaUrl(media.sessionId, name) };
    }),
  };
}

/** 解析可选 images 字段:接受 dataURL 或裸 base64,统一为 dataURL,最多 4 张。 */
function parseImages(body: Record<string, unknown>): string[] {
  if (!Array.isArray(body.images)) return [];
  const images: string[] = [];
  for (const item of body.images) {
    if (typeof item !== 'string' || item === '') continue;
    if (images.length >= MAX_IMAGES_PER_TURN) break;
    images.push(item.startsWith('data:') ? item : `data:image/png;base64,${item}`);
  }
  return images;
}

export function createAgentServer(options: AgentServerOptions = {}): AgentServer {
  const systemPrompt = options.systemPrompt ?? defaultSystemPrompt();
  const contextTokens = resolveContextTokens(options.contextTokens);

  let cachedProvider: ProviderHandle | undefined;
  const providerFactory =
    options.providerFactory ??
    ((): ProviderHandle => {
      cachedProvider ??= createProviderFromEnv();
      return cachedProvider;
    });

  const runtimes = new Map<string, SessionRuntime>();
  const subagentSemaphore = new SubagentSemaphore();
  const sessions = new SessionManager({
    ...(options.sessionIdleMs !== undefined ? { idleMs: options.sessionIdleMs } : {}),
    ...(options.sweepIntervalMs !== undefined
      ? { sweepIntervalMs: options.sweepIntervalMs }
      : {}),
    ...(options.restoreWorkspaceDirs !== undefined
      ? { workspaces: options.restoreWorkspaceDirs }
      : {}),
    ...(options.workspaceRegistryPath !== undefined
      ? { workspaceRegistryPath: options.workspaceRegistryPath }
      : {}),
    onExpire: (session) => {
      runtimes.get(session.id)?.bridge.cancelAll();
      runtimes.delete(session.id);
    },
  });

  const toolsFactory =
    options.toolsFactory ??
    ((session: Session): ToolRegistry => {
      const registry = new ToolRegistry();
      registerBuiltinTools(registry, { workspaceDir: session.workspaceDir });
      return registry;
    });

  const createRuntime = (session: Session): SessionRuntime => {
    const bridge = new ApprovalBridge(
      options.approvalTimeoutMs !== undefined
        ? { timeoutMs: options.approvalTimeoutMs }
        : {},
    );
    const gate = new PermissionGate({
      mode: session.mode,
      approvalService: new CallbackApprovalService((request) =>
        bridge.requestApproval(request, {
          preview: buildPreview(request.toolName, request.arguments, request.action),
        }),
      ),
    });
    const registry = toolsFactory(session);
    // spawn_subagent / subagent_list / subagent_report 在 server 组装处闭包注入
    // (provider/gate/systemPrompt 与共享 SubagentRunRegistry);自定义 toolsFactory
    // 已自带同名工具时跳过,避免覆盖。子代理运行注册表按 session 构造注入,避免全局态。
    if (registry.resolve('spawn_subagent') === undefined) {
      const subagentRegistry = createSubagentRunRegistry();
      registry.register(
        createSpawnSubagentTool({
          parentRegistry: registry,
          workspace: { workspaceDir: session.workspaceDir, additionalDirs: [] },
          providerFactory,
          gate,
          systemPrompt,
          contextTokens,
          toolserverClient: new ToolserverClient({}),
          registry: subagentRegistry,
          semaphore: subagentSemaphore,
        }),
      );
      registry.register(
        createSubagentListTool(
          { registry: subagentRegistry },
          loadToolsetEntrySpec('subagent_list'),
        ),
      );
      registry.register(
        createSubagentReportTool(
          { registry: subagentRegistry },
          loadToolsetEntrySpec('subagent_report'),
        ),
      );
    }
    return { registry, gate, bridge, busy: false, controller: null, steerQueue: [] };
  };

  /** 内存中无 runtime 时(恢复的 session)按需创建。 */
  const runtimeFor = (session: Session): SessionRuntime => {
    let runtime = runtimes.get(session.id);
    if (runtime === undefined) {
      runtime = createRuntime(session);
      runtimes.set(session.id, runtime);
    }
    return runtime;
  };

  const findBridge = (requestId: string): ApprovalBridge | undefined => {
    for (const runtime of runtimes.values()) {
      if (runtime.bridge.has(requestId)) return runtime.bridge;
    }
    return undefined;
  };

  const handleHealth = (res: ServerResponse): void => {
    sendJson(res, 200, { status: 'ok' });
  };

  const handleCreateSession = (res: ServerResponse, body: unknown): void => {
    if (!isRecord(body) || typeof body.workspaceDir !== 'string' || body.workspaceDir === '') {
      sendError(res, 400, 'invalid_request', 'workspaceDir is required and must be a string');
      return;
    }
    const mode: PermissionMode =
      body.mode === 'yolo' || body.mode === 'auto' ? body.mode : 'manual';
    let isDir = false;
    try {
      isDir = statSync(body.workspaceDir).isDirectory();
    } catch {
      isDir = false;
    }
    if (!isDir) {
      sendError(res, 400, 'invalid_workspace', `workspaceDir is not an existing directory: ${body.workspaceDir}`);
      return;
    }
    const session = sessions.create({ workspaceDir: body.workspaceDir, mode });
    runtimes.set(session.id, createRuntime(session));
    sendJson(res, 200, { sessionId: session.id });
  };

  const handleListSessions = (res: ServerResponse): void => {
    sendJson(res, 200, { sessions: sessions.list() });
  };

  /** 运行时恢复某 workspace 的磁盘历史会话(web 启动/切换工作区时代理层调用)。 */
  const handleRestoreWorkspace = (res: ServerResponse, body: unknown): void => {
    if (!isRecord(body) || typeof body.workspaceDir !== 'string' || body.workspaceDir === '') {
      sendError(res, 400, 'invalid_request', 'workspaceDir is required and must be a string');
      return;
    }
    let isDir = false;
    try {
      isDir = statSync(body.workspaceDir).isDirectory();
    } catch {
      isDir = false;
    }
    if (!isDir) {
      sendError(res, 400, 'invalid_workspace', `workspaceDir is not an existing directory: ${body.workspaceDir}`);
      return;
    }
    sendJson(res, 200, { status: 'ok', restored: sessions.restoreWorkspace(body.workspaceDir) });
  };

  const handleGetHistory = (res: ServerResponse, sessionId: string): void => {
    // 只读 entries:不物化整会话(messages 可能有几十 MB 视频 dataURL)。
    const entries = sessions.getEntries(sessionId);
    if (entries === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    const bridge = runtimes.get(sessionId)?.bridge;
    for (const entry of entries) {
      enrichPendingApprovalEntry(entry, bridge);
    }
    sendJson(res, 200, { entries });
  };

  /** 断线续传:已落盘 entries 中 seq > fromSeq 的部分(带 seq)+ 该 session
   * 是否有进行中轮次(inProgress)。前端刷新后用它补齐进度。未决审批条目
   * 若仍挂起,补 pending 标记与 accesses/preview,使恢复后可操作。 */
  const handleGetEvents = (res: ServerResponse, sessionId: string, url: URL): void => {
    const raw = url.searchParams.get('fromSeq');
    if (raw !== null && !/^\d+$/.test(raw)) {
      sendError(res, 400, 'invalid_request', 'fromSeq must be a non-negative integer');
      return;
    }
    const fromSeq = raw === null ? 0 : Number(raw);
    const events = sessions.getEntriesAfter(sessionId, fromSeq);
    if (events === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    const bridge = runtimes.get(sessionId)?.bridge;
    for (const { entry } of events) {
      enrichPendingApprovalEntry(entry, bridge);
    }
    sendJson(res, 200, { events, inProgress: runtimes.get(sessionId)?.busy === true });
  };

  /** media 引用文件:GET /sessions/{id}/media/{name}(内容寻址,可长缓存)。
   * 文件名先过 hash+扩展名白名单(防路径穿越),再按 session 所属 workspace
   * 的 .agent/media 定位;未知 session / 文件缺失 → 404。 */
  const handleGetMedia = (res: ServerResponse, sessionId: string, name: string): void => {
    const contentType = mediaContentType(name);
    const workspaceDir = contentType === undefined ? undefined : sessions.workspaceDirOf(sessionId);
    if (workspaceDir === undefined) {
      sendError(res, 404, 'media_not_found', `unknown session or media: ${sessionId}/${name}`);
      return;
    }
    let bytes: Buffer;
    try {
      bytes = readFileSync(path.join(workspaceDir, '.agent', 'media', name));
    } catch {
      sendError(res, 404, 'media_not_found', `media not found: ${name}`);
      return;
    }
    res.writeHead(200, {
      'Content-Type': contentType,
      'Content-Length': bytes.length,
      // 内容寻址:同字节即同 URL,可永久强缓存。
      'Cache-Control': 'public, max-age=31536000, immutable',
    });
    res.end(bytes);
  };

  const handleDeleteSession = (res: ServerResponse, sessionId: string): void => {
    if (!sessions.delete(sessionId)) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    // onExpire 已取消挂起审批并清理 runtime。
    sendJson(res, 200, { status: 'ok' });
  };

  /** 手动压缩:立即对该 session 的 messages 做 LLM 摘要压缩(无条件触发,
   * 与 loop 自动路径共用 compactMessagesWithSummary;摘要失败回退占位替换;
   * 无可压缩内容时 noop),压缩后 messages 整体重写落盘。进行中的轮次返回 409。 */
  const handleCompact = async (res: ServerResponse, sessionId: string): Promise<void> => {
    const session = sessions.get(sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    const runtime = runtimeFor(session);
    if (runtime.busy) {
      sendError(res, 409, 'chat_in_progress', `session ${session.id} already has a chat turn in progress`);
      return;
    }
    const { provider } = providerFactory();
    const outcome = await compactMessagesWithSummary(
      session.messages,
      createCompactionConfig(contextTokens),
      provider,
    );
    if (outcome.compacted) sessions.replaceMessages(session.id, outcome.messages);
    sendJson(res, 200, {
      status: 'ok',
      compacted: outcome.compacted,
      summarized: outcome.summarized,
      beforeTokens: outcome.beforeTokens,
      afterTokens: outcome.afterTokens,
    });
  };

  /** 撤回:截断 entries[entryIndex..] 与其后的 kosong messages(见
   * SessionManager.recall)。进行中的轮次返回 409。 */
  const handleRecall = (res: ServerResponse, sessionId: string, body: unknown): void => {
    if (
      !isRecord(body) ||
      typeof body.entryIndex !== 'number' ||
      !Number.isInteger(body.entryIndex) ||
      body.entryIndex < 0
    ) {
      sendError(res, 400, 'invalid_request', 'entryIndex is required and must be a non-negative integer');
      return;
    }
    const session = sessions.get(sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    const runtime = runtimeFor(session);
    if (runtime.busy) {
      sendError(res, 409, 'chat_in_progress', `session ${session.id} already has a chat turn in progress`);
      return;
    }
    const result = sessions.recall(sessionId, body.entryIndex);
    if (result === 'invalid_entry') {
      sendError(
        res,
        400,
        'invalid_entry',
        `entryIndex ${body.entryIndex} does not point to a recallable user entry`,
      );
      return;
    }
    sendJson(res, 200, { status: 'ok' });
  };

  /** 自定义会话标题(空串 = 恢复自动派生);未知 session → 404。 */
  const handleSetTitle = (res: ServerResponse, sessionId: string, body: unknown): void => {
    if (!isRecord(body) || typeof body.title !== 'string') {
      sendError(res, 400, 'invalid_request', 'title is required and must be a string');
      return;
    }
    if (!sessions.setTitle(sessionId, body.title)) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    sendJson(res, 200, { status: 'ok', title: sessions.get(sessionId)?.title ?? '' });
  };

  /** 切换权限模式:更新 gate(下一轮生效)并持久化到 sessions 表。 */
  const handleSetMode = (res: ServerResponse, sessionId: string, body: unknown): void => {
    if (
      !isRecord(body) ||
      (body.mode !== 'manual' && body.mode !== 'auto' && body.mode !== 'yolo')
    ) {
      sendError(res, 400, 'invalid_request', "mode must be 'manual' | 'auto' | 'yolo'");
      return;
    }
    const session = sessions.get(sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    sessions.setMode(sessionId, body.mode);
    runtimeFor(session).gate.setMode(body.mode);
    sendJson(res, 200, { status: 'ok', mode: body.mode });
  };

  /** 显式终止进行中的轮次:abort 其 controller,并把挂起审批以 cancelled
   * 落定(审批 promise 不受 abort 信号影响,不取消会拖满审批超时),loop 以
   * cancelled 收尾,已完成部分由 P1 的增量落盘保留;无进行中轮次 → 409
   * no_active_turn。 */
  const handleCancel = (res: ServerResponse, sessionId: string): void => {
    const session = sessions.get(sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    const runtime = runtimeFor(session);
    if (!runtime.busy || runtime.controller === null) {
      sendError(res, 409, 'no_active_turn', `session ${session.id} has no chat turn in progress`);
      return;
    }
    runtime.controller.abort();
    runtime.bridge.cancelAll('用户已取消本轮对话');
    sendJson(res, 200, { status: 'ok' });
  };

  /** steer:轮次进行中排队一条 user 消息,下一个 step 边界注入(见
   * handleChat 的 nextSteer 回调);无进行中轮次 → 409 no_active_turn
   * (前端应改为直接发 /chat)。 */
  const handleSteer = (res: ServerResponse, sessionId: string, body: unknown): void => {
    if (!isRecord(body) || typeof body.input !== 'string' || body.input === '') {
      sendError(res, 400, 'invalid_request', 'input is required and must be a non-empty string');
      return;
    }
    const session = sessions.get(sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    const runtime = runtimeFor(session);
    if (!runtime.busy || runtime.controller === null) {
      sendError(res, 409, 'no_active_turn', `session ${session.id} has no chat turn in progress; send /chat instead`);
      return;
    }
    const videoPath =
      typeof body.videoPath === 'string' && body.videoPath !== '' ? body.videoPath : undefined;
    const images = parseImages(body);
    const userText =
      videoPath === undefined ? body.input : `视频路径:${videoPath}\n\n${body.input}`;
    const content: ContentPart[] = [{ type: 'text', text: userText }];
    for (const url of images) {
      content.push({ type: 'image_url', imageUrl: { url } });
    }
    const queued: QueuedSteer = {
      text: body.input,
      ...(videoPath !== undefined ? { videoPath } : {}),
      images,
      message: { role: 'user', content, toolCalls: [] },
    };
    runtime.steerQueue.push(queued);
    sendJson(res, 200, { status: 'ok', queued: true });
  };

  const handleApproval = (res: ServerResponse, body: unknown): void => {
    if (!isRecord(body) || typeof body.requestId !== 'string') {
      sendError(res, 400, 'invalid_request', 'requestId is required');
      return;
    }
    const decision = body.decision;
    if (decision !== 'approved' && decision !== 'rejected' && decision !== 'cancelled') {
      sendError(res, 400, 'invalid_request', "decision must be 'approved' | 'rejected' | 'cancelled'");
      return;
    }
    const bridge = findBridge(body.requestId);
    if (bridge === undefined) {
      sendError(res, 404, 'approval_not_found', `unknown or expired requestId: ${body.requestId}`);
      return;
    }
    const input: ApprovalDecisionInput = {
      decision,
      ...(body.scope === 'session' ? { scope: 'session' as const } : {}),
      ...(typeof body.feedback === 'string' ? { feedback: body.feedback } : {}),
    };
    bridge.resolveDecision(body.requestId, input);
    sendJson(res, 200, { status: 'ok' });
  };

  const handleChat = async (
    res: ServerResponse,
    body: unknown,
  ): Promise<void> => {
    if (!isRecord(body) || typeof body.sessionId !== 'string') {
      sendError(res, 400, 'invalid_request', 'sessionId is required');
      return;
    }
    if (typeof body.input !== 'string' || body.input === '') {
      sendError(res, 400, 'invalid_request', 'input is required and must be a non-empty string');
      return;
    }
    const videoPath =
      typeof body.videoPath === 'string' && body.videoPath !== '' ? body.videoPath : undefined;
    const images = parseImages(body);
    const session = sessions.get(body.sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${body.sessionId}`);
      return;
    }
    const runtime = runtimeFor(session);
    if (runtime.busy) {
      sendError(res, 409, 'chat_in_progress', `session ${session.id} already has a chat turn in progress`);
      return;
    }
    runtime.busy = true;
    // 轮次级 AbortController:/cancel 显式终止用(P1 起断连不再 abort,
    // 停止语义由 POST /sessions/{id}/cancel 承担)。
    const controller = new AbortController();
    runtime.controller = controller;

    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    // writeHead 只排队不发送:不显式 flush,客户端要等第一个事件(可能
    // 是首个 token / 工具结果)才收到响应头,长首包时误判超时。
    res.flushHeaders();

    // 断连不杀轮次:只标记客户端断开,loop 继续跑完并落盘(maxSteps 兜底);
    // SSE 写出错同样只标记断开,不再抛出。断连后挂起的审批保留,用户可经
    // events/history 恢复后重新回执;仅在本轮仍进行中时处理(res 'close' 在
    // 正常结束时也会触发,此时 controller 已清空),防止误伤后续轮次的新审批。
    // 注:请求体在路由分发时已被 readJsonBody 读完,req 的 'close' 早已发出、
    // 此后监听不到;感知断连用 res 的 'close'(连接被提前断开或响应完成时触发)。
    let clientDisconnected = false;
    res.on('close', () => {
      if (runtime.controller !== controller) return;
      clientDisconnected = true;
      // 不再 cancelAll:显式 /cancel 与轮末兜底仍负责 settle。
    });

    // SSE 事件带 seq:发出时已落盘 entries 的水位(= 当前最大落盘 seq),
    // 前端刷新后以最后收到的 seq 调 GET /sessions/{id}/events?fromSeq=N 补齐。
    const emit = (event: unknown): void => {
      if (clientDisconnected) return;
      const payload = isRecord(event) ? { ...event, seq: session.entries.length } : event;
      try {
        writeSseEvent(res, payload);
      } catch {
        clientDisconnected = true;
      }
    };

    // ---- 轮次持久化:条目/消息双水位与压缩回写统一由 TurnPersister 持有 ----
    const persister = new TurnPersister({
      sessions,
      sessionId: session.id,
      baseMessageCount: session.messages.length,
    });
    const userEntry: TimelineEntry = {
      kind: 'user',
      text: body.input,
      images,
      ...(videoPath !== undefined ? { videoPath } : {}),
      at: Date.now(),
    };
    persister.pushEntry(userEntry);

    let assistantText = '';
    let assistantThink = '';
    /** 当前步 generate 的壁钟耗时(generate_done 事件暂存),随 assistant 条目落盘。 */
    let pendingGenerateMs: number | undefined;
    const flushAssistant = (): void => {
      if (assistantText === '' && assistantThink === '') return;
      persister.pushEntry({
        kind: 'assistant',
        text: assistantText,
        think: assistantThink,
        ...(pendingGenerateMs !== undefined ? { generateMs: pendingGenerateMs } : {}),
        at: Date.now(),
      });
      assistantText = '';
      assistantThink = '';
      pendingGenerateMs = undefined;
    };
    /** tool_call_start 暂存,tool_result 到达时配对成一条 tool 条目。 */
    const pendingCalls = new Map<string, { name: string; arguments: string | null }>();

    runtime.bridge.bindEmitter((event) => {
      emit(event);
      persister.pushEntry({
        kind: 'approval',
        requestId: event.requestId,
        toolName: event.toolName,
        approvalRule: event.approvalRule,
        ...(event.description !== undefined ? { description: event.description } : {}),
        ...(event.accesses.length > 0 ? { accesses: event.accesses } : {}),
        ...(event.preview !== undefined ? { preview: event.preview } : {}),
        at: Date.now(),
      });
      // 审批条目立即落盘:断连恢复时 events/history 能重新投递未决审批。
      persister.flushEntries();
    });
    runtime.bridge.bindSettleHook((requestId, response) => {
      // 审批条目的 decision 回填:在落盘前的 step_done 批量落盘时一并序列化。
      persister.settleApproval(requestId, response.decision);
    });

    /** 轮末未消费的 steer:丢弃并经 SSE 告知「插话未生效」,不带入下一轮
     *  (否则会注入在下一轮新用户消息之后,模型看到时间倒置的指令)。 */
    const dropUnconsumedSteers = (): void => {
      if (runtime.steerQueue.length === 0) return;
      for (const item of runtime.steerQueue.splice(0, runtime.steerQueue.length)) {
        emit({
          type: 'steer_dropped',
          text: item.text,
          images: item.images,
          ...(item.videoPath !== undefined ? { videoPath: item.videoPath } : {}),
        });
      }
    };

    const userText =
      videoPath === undefined ? body.input : `视频路径:${videoPath}\n\n${body.input}`;
    const userContent: ContentPart[] = [{ type: 'text', text: userText }];
    for (const url of images) {
      userContent.push({ type: 'image_url', imageUrl: { url } });
    }
    const userMessage: Message = { role: 'user', content: userContent, toolCalls: [] };
    // loop 的初始历史必须在 appendMessages 推进 session.messages 之前快照,
    // 否则 user 消息会重复一份。
    const turnMessages: Message[] = [...session.messages, userMessage];
    // entry ↔ message 映射:记录本轮 user 消息写入前的 messages 长度,
    // recall 该 user 条目时按此值截断 messages(见 storage.ts messageIndex)。
    userEntry.messageIndex = session.messages.length;
    sessions.appendMessages(session.id, [userMessage]);
    persister.markUserMessagePersisted();
    persister.flushEntries(); // user 条目立即落盘:崩溃也至少保留用户输入。

    const { provider } = providerFactory();

    try {
      const result = await runAgentLoop({
        provider,
        systemPrompt,
        registry: runtime.registry,
        gate: runtime.gate,
        messages: turnMessages,
        signal: controller.signal,
        // steer:每个 step 的 generate 之前由 loop 逐条取回(取走即消费);
        // 每条立即构建 user 条目并落盘(TurnPersister),经活跃流发 SSE steer
        // 事件(客户端已断则 emit 为空操作,只落盘),消息体交给 loop 追加
        // 并即时落盘。
        nextSteer: () => {
          const item = runtime.steerQueue.shift();
          if (item === undefined) return null;
          // 与 /chat 的 user 条目同一映射:注入点即该消息在 messages 中的下标
          // (loop 逐条取走、取走即落盘,session.messages 已推进)。
          const entry: TimelineEntry = {
            kind: 'user',
            text: item.text,
            images: item.images,
            ...(item.videoPath !== undefined ? { videoPath: item.videoPath } : {}),
            messageIndex: session.messages.length,
            at: Date.now(),
          };
          persister.pushEntry(entry);
          persister.flushEntries(); // steer 条目立即落盘(seq 水位先于 SSE 事件推进)。
          emit({
            type: 'steer',
            text: item.text,
            images: item.images,
            ...(item.videoPath !== undefined ? { videoPath: item.videoPath } : {}),
          });
          return item.message;
        },
        compaction: { maxContextTokens: contextTokens },
        // 抽帧(多图)后的 generate 步降思考档位,防逐帧分析烧穿 maxTokens。
        framesThinkingEffort: resolveFramesThinkingEffort(),
        onStepPersist: (update) => {
          // loop 按消息确定点即时回灌(steer user / assistant / tool 增量,
          // 或压缩后的整体折叠),TurnPersister 统一决定 append 还是重写
          // (sqlite 同步写,在 onEvent 同一调用栈内完成,不引入异步积压)。
          persister.onStepPersist(update);
        },
        onEvent: (event: AgentLoopEvent) => {
          switch (event.type) {
            case 'text_delta':
              assistantText += event.text;
              break;
            case 'think_delta':
              assistantThink += event.text;
              break;
            case 'context_usage':
              sessions.setLastKnownUsage(session.id, event.usedTokens);
              break;
            case 'tool_call_start':
              flushAssistant();
              pendingCalls.set(event.call.id, {
                name: event.call.name,
                arguments: event.call.arguments,
              });
              break;
            case 'tool_result': {
              const call = pendingCalls.get(event.toolCallId);
              pendingCalls.delete(event.toolCallId);
              // load_video 的 output 含整段视频 dataURL(可达 ~50MB):SSE 与
              // sqlite 都用裁剪版——video part 替换为占位文本;image part 的
              // dataURL 写盘为 media 引用 URL(见 sanitizeToolOutputForTransport)。
              // 模型侧 messages 不受影响(loop 内部仍持原始 output)。
              const media = { sessionId: session.id, workspaceDir: session.workspaceDir };
              const safeOutput = sanitizeToolOutputForTransport(event.result.output, media);
              // 成功结果的结构化附件(submit_detection 的检测载荷)进条目前
              // 同样把 annotated_image dataURL 转为 media 引用 URL。
              const resultPayload =
                'payload' in event.result
                  ? sanitizeDetectionPayloadForTransport(event.result.payload, media)
                  : undefined;
              persister.pushEntry({
                kind: 'tool',
                toolCallId: event.toolCallId,
                name: event.name,
                arguments: call?.arguments ?? null,
                output: safeOutput,
                isError: event.isError,
                ...(event.result.note !== undefined ? { note: event.result.note } : {}),
                ...(resultPayload !== undefined ? { payload: resultPayload } : {}),
                at: Date.now(),
              });
              emit({
                ...event,
                result: {
                  ...event.result,
                  output: safeOutput,
                  ...(resultPayload !== undefined ? { payload: resultPayload } : {}),
                },
              });
              return;
            }
            case 'step_done':
              flushAssistant();
              persister.flushEntries();
              break;
            case 'generate_done':
              // 暂存本步 generate 耗时,随本条 assistant 条目落盘(flushAssistant);
              // 事件本身经末尾 emit 透传 SSE,供前端实时展示。
              pendingGenerateMs = event.generateMs;
              break;
            case 'done': {
              dropUnconsumedSteers();
              flushAssistant();
              // stop_turn 的结构化附件(submit_detection 的检测载荷)直接从
              // payload 读取,合成 detection SSE 事件并落盘——不再有
              // JSON.parse(note) 字符串编解码。
              const stopPayload =
                event.stopResult !== undefined && 'payload' in event.stopResult
                  ? event.stopResult.payload
                  : undefined;
              if (event.reason === 'stop_turn' && stopPayload !== undefined) {
                // annotated_image dataURL 先转 media 引用 URL 再进 SSE 与落盘。
                const safePayload = sanitizeDetectionPayloadForTransport(
                  stopPayload,
                  { sessionId: session.id, workspaceDir: session.workspaceDir },
                );
                emit({ type: 'detection', data: safePayload });
                persister.pushEntry({
                  kind: 'detection',
                  data: safePayload as DetectionPayload,
                  at: Date.now(),
                });
              }
              emit({
                type: 'done',
                reason: event.reason,
                ...(event.error !== undefined ? { error: event.error } : {}),
                ...(event.truncated === true ? { truncated: true } : {}),
              });
              return;
            }
          }
          emit(event);
        },
      });
      // 轮末兜底:正常路径各消息已即时落盘,这里只补齐理论不可达的余量
      // (水位与返回值脱节的回归安全网)与剩余条目;幂等。
      persister.finalize(result.messages);
    } catch (error) {
      emit({
        type: 'done',
        reason: 'error',
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      flushAssistant();
      persister.finalize(undefined); // 失败/中断的轮次也把剩余条目落盘,保证不丢。
      dropUnconsumedSteers(); // done 未经过 onEvent(异常路径)或竞态到达时兜底。
      // 轮末兜底:强制 settle 本轮残留的挂起审批,防止悬挂(settleHook 仍
      // 绑定,审批条目的 decision 一并落定为 cancelled)。
      runtime.bridge.cancelAll('轮次已结束');
      runtime.bridge.unbindSettleHook();
      runtime.bridge.unbindEmitter();
      runtime.controller = null;
      runtime.busy = false;
      try {
        res.end();
      } catch {
        // 客户端已断开:忽略写回错误。
      }
    }
  };

  const ctx: RequestContext = {
    handleHealth,
    handleCreateSession,
    handleListSessions,
    handleRestoreWorkspace,
    handleGetHistory,
    handleGetEvents,
    handleGetMedia,
    handleCompact,
    handleRecall,
    handleSetTitle,
    handleSetMode,
    handleCancel,
    handleSteer,
    handleDeleteSession,
    handleApproval,
    handleChat,
  };

  const server = createHttpServer(createRouter(ctx));

  return {
    server,
    sessions,
    close: () =>
      new Promise<void>((resolvePromise, rejectPromise) => {
        sessions.close();
        for (const runtime of runtimes.values()) runtime.bridge.cancelAll();
        server.close((error) => (error === undefined ? resolvePromise() : rejectPromise(error)));
      }),
  };
}
