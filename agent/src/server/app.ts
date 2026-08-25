/**
 * agent 运行时的 HTTP + SSE 服务(node:http 手写,无新增依赖):
 *
 *   GET    /health                 → {status:'ok'}
 *   POST   /sessions               → {sessionId}(workspaceDir 必填且须为已存在目录)
 *   GET    /sessions               → {sessions:[{id,workspaceDir,mode,title,createdAt,lastActiveAt}]}
 *   GET    /sessions/{id}/history  → {entries:[TimelineEntry]}(渲染友好的时间线)
 *   POST   /sessions/{id}/compact  → {status:'ok',compacted,beforeTokens,afterTokens}
 *                                      (立即压缩该 session 历史;进行中 → 409)
 *   POST   /sessions/{id}/recall   {entryIndex} → {status:'ok'}(撤回:删除
 *                                      entries[entryIndex..] 并同步截断 kosong
 *                                      messages;进行中 → 409;越界/非 user → 400)
 *   DELETE /sessions/{id}          → {status:'ok'}(同时取消挂起审批、删盘)
 *   POST   /chat                   → SSE 流(text/event-stream,每事件一行 'data: {json}\n\n')
 *   POST   /approval               → 审批回执(见 approvalBridge.ts)
 *
 * 上下文窗口:AGENT_CONTEXT_TOKENS(默认 262144 = 256k)。/chat 每步 generate
 * 后按真实 usage 透传 context_usage 事件并记录 session.lastKnownUsage(GET
 * /sessions 摘要带 usedTokens);上一步用量 ≥ 窗口 × 0.85 时,下一步 generate
 * 前自动压缩(替换老工具结果为占位)并透传 compaction 事件。
 *
 * 错误统一 {error:{code,message}};未知 session → 404。同 session 的 /chat
 * 用简单互斥串行,不同 session 并行。provider / tools 均可注入以便测试。
 *
 * 持久化:SessionManager 委托 node:sqlite(<workspaceDir>/.agent/sessions.db);
 * /chat 的 SSE 事件流在转发的同时累积 TimelineEntry(user/assistant/tool/
 * approval/detection),每轮结束批量落盘。POST /chat 支持可选 images(最多
 * 4 张,base64 或 dataURL),转成 kosong image ContentPart 附在该轮 user message。
 */
import { readFileSync, statSync } from 'node:fs';
import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { fileURLToPath } from 'node:url';

import type { ContentPart, Message } from '#/message';
import type { ChatProvider } from '#/provider';

import { createProviderFromEnv } from '../llm/provider';
import { runAgentLoop, type AgentLoopEvent } from '../loop/agentLoop';
import {
  compactMessages,
  createCompactionConfig,
  estimateMessagesTokens,
} from '../loop/compaction';
import { CallbackApprovalService } from '../permissions/approval';
import { PermissionGate } from '../permissions/gate';
import type { PermissionMode, PermissionPolicyContext } from '../permissions/types';
import { registerBuiltinTools } from '../tools/builtin';
import type { ToolAccesses } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { ApprovalBridge, type ApprovalDecisionInput } from './approvalBridge';
import { SessionManager, type Session } from './session';
import type { TimelineEntry } from './storage';

export interface ProviderHandle {
  readonly provider: ChatProvider;
  readonly model: string;
}

export interface AgentServerOptions {
  /** 构造 LLM provider;默认 createProviderFromEnv()(惰性,首次使用时创建)。 */
  readonly providerFactory?: () => ProviderHandle;
  /** 按 session 构造工具注册表;默认 registerBuiltinTools(workspaceDir)。 */
  readonly toolsFactory?: (session: Session) => ToolRegistry;
  /** system prompt;默认读 agent/prompts/chat_system.md(缺失回退 detect_system.md)。 */
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
}

export interface AgentServer {
  readonly server: Server;
  readonly sessions: SessionManager;
  close(): Promise<void>;
}

interface SessionRuntime {
  readonly registry: ToolRegistry;
  readonly gate: ExecutionSnapshotGate;
  readonly bridge: ApprovalBridge;
  /** 同 session 的 /chat 串行锁:true 时有进行中的轮次。 */
  busy: boolean;
}

/**
 * 默认 system prompt:agent/prompts/chat_system.md(统一对话);文件不存在时
 * 回退 detect_system.md 并打警告(并行任务产出 chat_system.md 后即走主路径)。
 */
export function defaultSystemPrompt(promptsDir?: string): string {
  const dir = promptsDir ?? fileURLToPath(new URL('../../prompts', import.meta.url));
  try {
    return readFileSync(`${dir}/chat_system.md`, 'utf8');
  } catch {
    console.warn('[agent-server] chat_system.md 不存在,回退 detect_system.md');
    return readFileSync(`${dir}/detect_system.md`, 'utf8');
  }
}

/**
 * 快照每次裁决的执行元数据:ApprovalRequest 只带 action/description,
 * SSE 的 approval_request 事件还需要 accesses,这里按 toolCallId 暂存。
 * 快照 Map 由调用方持有,以便 approvalService 闭包在 gate 构造前引用。
 */
class ExecutionSnapshotGate extends PermissionGate {
  constructor(
    options: ConstructorParameters<typeof PermissionGate>[0],
    private readonly snapshots: Map<string, { accesses: ToolAccesses }>,
  ) {
    super(options);
  }

  override async authorize(
    context: Omit<PermissionPolicyContext, 'mode'>,
  ): ReturnType<PermissionGate['authorize']> {
    this.snapshots.set(context.toolCall.id, { accesses: context.execution.accesses ?? [] });
    try {
      return await super.authorize(context);
    } finally {
      this.snapshots.delete(context.toolCall.id);
    }
  }
}

const MAX_BODY_BYTES = 16 * 1024 * 1024;
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

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    size += buf.length;
    if (size > MAX_BODY_BYTES) throw new Error('request body too large');
    chunks.push(buf);
  }
  const raw = Buffer.concat(chunks).toString('utf8').trim();
  if (raw === '') return {};
  return JSON.parse(raw) as unknown;
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(payload);
}

function sendError(res: ServerResponse, status: number, code: string, message: string): void {
  sendJson(res, status, { error: { code, message } });
}

function writeSseEvent(res: ServerResponse, event: unknown): void {
  res.write(`data: ${JSON.stringify(event)}\n\n`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
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
  const sessions = new SessionManager({
    ...(options.sessionIdleMs !== undefined ? { idleMs: options.sessionIdleMs } : {}),
    ...(options.sweepIntervalMs !== undefined
      ? { sweepIntervalMs: options.sweepIntervalMs }
      : {}),
    ...(options.restoreWorkspaceDirs !== undefined
      ? { workspaces: options.restoreWorkspaceDirs }
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
    const snapshots = new Map<string, { accesses: ToolAccesses }>();
    const gate = new ExecutionSnapshotGate(
      {
        mode: session.mode,
        approvalService: new CallbackApprovalService((request) =>
          bridge.requestApproval(request, {
            accesses: snapshots.get(request.toolCallId)?.accesses ?? [],
          }),
        ),
      },
      snapshots,
    );
    return { registry: toolsFactory(session), gate, bridge, busy: false };
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

  const handleCreateSession = (res: ServerResponse, body: unknown): void => {
    if (!isRecord(body) || typeof body.workspaceDir !== 'string' || body.workspaceDir === '') {
      sendError(res, 400, 'invalid_request', 'workspaceDir is required and must be a string');
      return;
    }
    const mode: PermissionMode = body.mode === 'yolo' ? 'yolo' : 'manual';
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

  const handleGetHistory = (res: ServerResponse, sessionId: string): void => {
    const session = sessions.get(sessionId);
    if (session === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    sendJson(res, 200, { entries: session.entries });
  };

  const handleDeleteSession = (res: ServerResponse, sessionId: string): void => {
    if (!sessions.delete(sessionId)) {
      sendError(res, 404, 'session_not_found', `unknown session: ${sessionId}`);
      return;
    }
    // onExpire 已取消挂起审批并清理 runtime。
    sendJson(res, 200, { status: 'ok' });
  };

  /** 手动压缩:立即对该 session 的 messages 执行 compactMessages(强制触发,
   * 不走过阈判断;无可压缩内容时 noop)。进行中的轮次返回 409。 */
  const handleCompact = (res: ServerResponse, sessionId: string): void => {
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
    const beforeTokens = estimateMessagesTokens(session.messages);
    const outcome = compactMessages(
      session.messages,
      createCompactionConfig(contextTokens),
      true,
    );
    if (outcome.compacted) sessions.replaceMessages(session.id, outcome.messages);
    sendJson(res, 200, {
      status: 'ok',
      compacted: outcome.compacted,
      beforeTokens,
      afterTokens: estimateMessagesTokens(session.messages),
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
    req: IncomingMessage,
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

    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });

    const controller = new AbortController();
    req.on('close', () => {
      controller.abort();
    });

    const emit = (event: unknown): void => {
      writeSseEvent(res, event);
    };

    // ---- 时间线条目累积(与 SSE 转发并行,每轮结束批量落盘) ----
    const turnEntries: TimelineEntry[] = [];
    const userEntry: TimelineEntry = {
      kind: 'user',
      text: body.input,
      images,
      ...(videoPath !== undefined ? { videoPath } : {}),
      at: Date.now(),
    };
    turnEntries.push(userEntry);

    let assistantText = '';
    let assistantThink = '';
    const flushAssistant = (): void => {
      if (assistantText === '' && assistantThink === '') return;
      turnEntries.push({ kind: 'assistant', text: assistantText, think: assistantThink, at: Date.now() });
      assistantText = '';
      assistantThink = '';
    };
    /** tool_call_start 暂存,tool_result 到达时配对成一条 tool 条目。 */
    const pendingCalls = new Map<string, { name: string; arguments: string | null }>();

    runtime.bridge.bindEmitter((event) => {
      emit(event);
      turnEntries.push({
        kind: 'approval',
        requestId: event.requestId,
        toolName: event.toolName,
        approvalRule: event.approvalRule,
        ...(event.description !== undefined ? { description: event.description } : {}),
        at: Date.now(),
      });
    });
    runtime.bridge.bindSettleHook((requestId, response) => {
      for (let i = turnEntries.length - 1; i >= 0; i -= 1) {
        const entry = turnEntries[i];
        if (entry !== undefined && entry.kind === 'approval' && entry.requestId === requestId) {
          entry.decision = response.decision;
          break;
        }
      }
    });

    const userText =
      videoPath === undefined ? body.input : `视频路径:${videoPath}\n\n${body.input}`;
    const userContent: ContentPart[] = [{ type: 'text', text: userText }];
    for (const url of images) {
      userContent.push({ type: 'image_url', imageUrl: { url } });
    }
    const userMessage: Message = { role: 'user', content: userContent, toolCalls: [] };
    const baseLength = session.messages.length;
    // entry ↔ message 映射:记录本轮 user 消息写入前的 messages 长度,
    // recall 该 user 条目时按此值截断 messages(见 storage.ts messageIndex)。
    userEntry.messageIndex = baseLength;
    const messages = [...session.messages, userMessage];
    sessions.appendMessages(session.id, [userMessage]);

    const { provider, model } = providerFactory();

    try {
      const result = await runAgentLoop({
        provider,
        model,
        systemPrompt,
        registry: runtime.registry,
        gate: runtime.gate,
        messages,
        compaction: { maxContextTokens: contextTokens },
        signal: controller.signal,
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
              turnEntries.push({
                kind: 'tool',
                toolCallId: event.toolCallId,
                name: event.name,
                arguments: call?.arguments ?? null,
                output: event.result.output,
                isError: event.isError,
                at: Date.now(),
              });
              break;
            }
            case 'step_done':
              flushAssistant();
              break;
            case 'done':
              flushAssistant();
              if (event.reason === 'stop_turn' && event.stopResult?.note !== undefined) {
                let data: unknown;
                try {
                  data = JSON.parse(event.stopResult.note);
                } catch {
                  data = event.stopResult.note;
                }
                emit({ type: 'detection', data });
                turnEntries.push({ kind: 'detection', data, at: Date.now() });
              }
              emit({ type: 'done', reason: event.reason });
              return;
          }
          emit(event);
        },
      });
      // 回灌后的增量消息(assistant / tool)并入会话历史(user 已在上面追加)。
      sessions.appendMessages(session.id, result.messages.slice(baseLength + 1));
    } catch (error) {
      emit({
        type: 'done',
        reason: 'error',
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      flushAssistant();
      sessions.appendEntries(session.id, turnEntries);
      runtime.bridge.unbindSettleHook();
      runtime.bridge.unbindEmitter();
      runtime.busy = false;
      res.end();
    }
  };

  const server = createHttpServer((req, res) => {
    void (async (): Promise<void> => {
      const url = new URL(req.url ?? '/', 'http://127.0.0.1');
      try {
        if (req.method === 'GET' && url.pathname === '/health') {
          sendJson(res, 200, { status: 'ok' });
          return;
        }
        if (req.method === 'POST' && url.pathname === '/sessions') {
          handleCreateSession(res, await readJsonBody(req));
          return;
        }
        if (req.method === 'GET' && url.pathname === '/sessions') {
          handleListSessions(res);
          return;
        }
        const historyMatch = /^\/sessions\/([^/]+)\/history$/.exec(url.pathname);
        if (req.method === 'GET' && historyMatch !== null) {
          const sessionId = historyMatch[1];
          if (sessionId === undefined) {
            sendError(res, 400, 'invalid_request', 'session id is required');
            return;
          }
          handleGetHistory(res, sessionId);
          return;
        }
        const compactMatch = /^\/sessions\/([^/]+)\/compact$/.exec(url.pathname);
        if (req.method === 'POST' && compactMatch !== null) {
          const sessionId = compactMatch[1];
          if (sessionId === undefined) {
            sendError(res, 400, 'invalid_request', 'session id is required');
            return;
          }
          handleCompact(res, sessionId);
          return;
        }
        const recallMatch = /^\/sessions\/([^/]+)\/recall$/.exec(url.pathname);
        if (req.method === 'POST' && recallMatch !== null) {
          const sessionId = recallMatch[1];
          if (sessionId === undefined) {
            sendError(res, 400, 'invalid_request', 'session id is required');
            return;
          }
          handleRecall(res, sessionId, await readJsonBody(req));
          return;
        }
        const sessionMatch = /^\/sessions\/([^/]+)$/.exec(url.pathname);
        if (req.method === 'DELETE' && sessionMatch !== null) {
          const sessionId = sessionMatch[1];
          if (sessionId === undefined) {
            sendError(res, 400, 'invalid_request', 'session id is required');
            return;
          }
          handleDeleteSession(res, sessionId);
          return;
        }
        if (req.method === 'POST' && url.pathname === '/approval') {
          handleApproval(res, await readJsonBody(req));
          return;
        }
        if (req.method === 'POST' && url.pathname === '/chat') {
          await handleChat(req, res, await readJsonBody(req));
          return;
        }
        sendError(res, 404, 'not_found', `${req.method ?? ''} ${url.pathname}`);
      } catch (error) {
        if (!res.headersSent) {
          sendError(
            res,
            400,
            'bad_request',
            error instanceof Error ? error.message : String(error),
          );
        } else {
          res.end();
        }
      }
    })();
  });

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
