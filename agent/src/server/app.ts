/**
 * agent 运行时的 HTTP + SSE 服务(node:http 手写,无新增依赖):
 *
 *   GET  /health           → {status:'ok'}
 *   POST /sessions         → {sessionId}(workspaceDir 必填且须为已存在目录)
 *   POST /chat             → SSE 流(text/event-stream,每事件一行 'data: {json}\n\n')
 *   POST /approval         → 审批回执(见 approvalBridge.ts)
 *
 * 错误统一 {error:{code,message}};未知 session → 404。同 session 的 /chat
 * 用简单互斥串行,不同 session 并行。provider / tools 均可注入以便测试。
 */
import { readFileSync, statSync } from 'node:fs';
import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { fileURLToPath } from 'node:url';

import { createUserMessage } from '#/message';
import type { ChatProvider } from '#/provider';

import { createProviderFromEnv } from '../llm/provider';
import { runAgentLoop, type AgentLoopEvent } from '../loop/agentLoop';
import { CallbackApprovalService } from '../permissions/approval';
import { PermissionGate } from '../permissions/gate';
import type { PermissionMode, PermissionPolicyContext } from '../permissions/types';
import { registerBuiltinTools } from '../tools/builtin';
import type { ToolAccesses } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { ApprovalBridge, type ApprovalDecisionInput } from './approvalBridge';
import { SessionManager, type Session } from './session';

export interface ProviderHandle {
  readonly provider: ChatProvider;
  readonly model: string;
}

export interface AgentServerOptions {
  /** 构造 LLM provider;默认 createProviderFromEnv()(惰性,首次使用时创建)。 */
  readonly providerFactory?: () => ProviderHandle;
  /** 按 session 构造工具注册表;默认 registerBuiltinTools(workspaceDir)。 */
  readonly toolsFactory?: (session: Session) => ToolRegistry;
  /** system prompt;默认读 agent/prompts/detect_system.md。 */
  readonly systemPrompt?: string;
  /** 审批挂起超时(ms),默认 5 分钟。 */
  readonly approvalTimeoutMs?: number;
  /** session idle 过期(ms),默认 2h。 */
  readonly sessionIdleMs?: number;
  /** 过期清扫周期(ms),默认 60s。 */
  readonly sweepIntervalMs?: number;
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

/** 默认 system prompt:agent/prompts/detect_system.md。 */
export function defaultSystemPrompt(): string {
  return readFileSync(
    fileURLToPath(new URL('../../prompts/detect_system.md', import.meta.url)),
    'utf8',
  );
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

const MAX_BODY_BYTES = 1024 * 1024;

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

export function createAgentServer(options: AgentServerOptions = {}): AgentServer {
  const systemPrompt = options.systemPrompt ?? defaultSystemPrompt();

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
    const session = sessions.get(body.sessionId);
    const runtime = runtimes.get(body.sessionId);
    if (session === undefined || runtime === undefined) {
      sendError(res, 404, 'session_not_found', `unknown session: ${body.sessionId}`);
      return;
    }
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
    runtime.bridge.bindEmitter(emit);

    const userText =
      videoPath === undefined ? body.input : `视频路径:${videoPath}\n\n${body.input}`;
    const baseLength = session.messages.length;
    const messages = [...session.messages, createUserMessage(userText)];
    sessions.appendMessages(session.id, [createUserMessage(userText)]);

    const { provider, model } = providerFactory();

    try {
      const result = await runAgentLoop({
        provider,
        model,
        systemPrompt,
        registry: runtime.registry,
        gate: runtime.gate,
        messages,
        signal: controller.signal,
        onEvent: (event: AgentLoopEvent) => {
          if (event.type === 'done') {
            if (event.reason === 'stop_turn' && event.stopResult?.note !== undefined) {
              try {
                emit({ type: 'detection', data: JSON.parse(event.stopResult.note) });
              } catch {
                emit({ type: 'detection', data: event.stopResult.note });
              }
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
