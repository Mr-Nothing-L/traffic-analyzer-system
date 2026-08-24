/**
 * server 模块单元测试:假 ChatProvider(ScriptedProvider,参考
 * loop/loop.test.ts)+ 假工具,起真实 node:http 服务(随机端口),
 * 覆盖 /sessions → /chat 的 SSE 事件序列、approval 往返、未知 session 404。
 * 不打真实模型 API。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { Message, StreamedMessagePart, ToolCall } from '#/message';
import type {
  ChatProvider,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '#/provider';
import type { Tool } from '#/tool';

import type { ExecutableTool, ExecutableToolResult } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { createAgentServer, type AgentServer } from './app';
import type { Session } from './session';

// ---------------------------------------------------------------------------
// 假 provider / 假工具
// ---------------------------------------------------------------------------

class ScriptedProvider implements ChatProvider {
  readonly name = 'scripted';
  readonly modelName = 'scripted-model';
  readonly thinkingEffort = null;
  readonly histories: Message[][] = [];
  private readonly script: StreamedMessagePart[][];

  constructor(script: StreamedMessagePart[][]) {
    this.script = [...script];
  }

  generate(
    _systemPrompt: string,
    _tools: Tool[],
    history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
    this.histories.push(history.map((m) => m));
    const parts = this.script.shift();
    if (parts === undefined) return Promise.reject(new Error('script exhausted'));
    return Promise.resolve(streamOf(parts));
  }

  withThinking(_effort: ThinkingEffort): ChatProvider {
    return this;
  }
}

function streamOf(parts: StreamedMessagePart[]): StreamedMessage {
  return {
    async *[Symbol.asyncIterator]() {
      for (const part of parts) yield part;
    },
    id: null,
    usage: null,
    finishReason: 'completed',
    rawFinishReason: 'stop',
  };
}

function toolCall(id: string, name: string, args: unknown): ToolCall {
  return { type: 'function', id, name, arguments: JSON.stringify(args) };
}

function text(text: string): StreamedMessagePart {
  return { type: 'text', text };
}

/** 只读假工具(accesses 为空,manual 模式下 default-readonly-approve 直接放行)。 */
function echoTool(): ExecutableTool {
  return {
    name: 'echo',
    description: 'fake echo tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: 'echo()',
      execute: () => Promise.resolve({ output: 'echo-ok' }),
    }),
  };
}

/** 写文件假工具(manual 模式下触发 fallback-ask)。 */
function writeTool(): ExecutableTool {
  return {
    name: 'write_file',
    description: 'fake write tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [{ kind: 'file' as const, operation: 'write' as const, path: '/tmp/x' }],
      approvalRule: 'write_file(/tmp/x)',
      execute: () => Promise.resolve({ output: 'write-ok' }),
    }),
  };
}

/** 模拟 submit_detection:stopTurn + note 携带结构化 JSON。 */
function submitTool(payload: unknown): ExecutableTool {
  return {
    name: 'submit_detection',
    description: 'fake submit tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: 'submit_detection',
      execute: (): Promise<ExecutableToolResult> =>
        Promise.resolve({
          output: '检测结果已提交',
          stopTurn: true,
          note: JSON.stringify(payload),
        }),
    }),
  };
}

// ---------------------------------------------------------------------------
// HTTP / SSE 测试辅助
// ---------------------------------------------------------------------------

interface SseEvent {
  readonly type: string;
  readonly [key: string]: unknown;
}

/** 逐个读取 SSE 事件;流结束返回 null。 */
function sseReader(body: ReadableStream<Uint8Array>): () => Promise<SseEvent | null> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  return async (): Promise<SseEvent | null> => {
    for (;;) {
      const boundary = buffer.indexOf('\n\n');
      if (boundary >= 0) {
        const raw = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const line = raw.split('\n').find((l) => l.startsWith('data:'));
        if (line !== undefined) return JSON.parse(line.slice(5).trim()) as SseEvent;
        continue;
      }
      const { done, value } = await reader.read();
      if (done) return null;
      buffer += decoder.decode(value, { stream: true });
    }
  };
}

async function readUntilDone(next: () => Promise<SseEvent | null>): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  for (;;) {
    const event = await next();
    if (event === null) break;
    events.push(event);
    if (event.type === 'done') break;
  }
  return events;
}

// ---------------------------------------------------------------------------
// 测试 harness
// ---------------------------------------------------------------------------

let workspace: string;
let agentServer: AgentServer;
let baseUrl: string;

async function startServer(
  provider: ScriptedProvider,
  tools: ExecutableTool[],
): Promise<void> {
  agentServer = createAgentServer({
    providerFactory: () => ({ provider, model: provider.modelName }),
    toolsFactory: (_session: Session) => {
      const registry = new ToolRegistry();
      for (const tool of tools) registry.register(tool);
      return registry;
    },
    systemPrompt: 'sys',
  });
  await new Promise<void>((resolve) => {
    agentServer.server.listen(0, '127.0.0.1', resolve);
  });
  const address = agentServer.server.address();
  if (address === null || typeof address === 'string') throw new Error('no address');
  baseUrl = `http://127.0.0.1:${address.port}`;
}

async function postJson(
  urlPath: string,
  body: unknown,
): Promise<{ status: number; json: unknown }> {
  const res = await fetch(`${baseUrl}${urlPath}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: await res.json() };
}

async function createSession(mode?: string): Promise<string> {
  const { status, json } = await postJson('/sessions', {
    workspaceDir: workspace,
    ...(mode !== undefined ? { mode } : {}),
  });
  expect(status).toBe(200);
  const sessionId = (json as { sessionId?: string }).sessionId;
  expect(typeof sessionId).toBe('string');
  return sessionId as string;
}

function startChat(sessionId: string, input: string, videoPath?: string): Promise<Response> {
  return fetch(`${baseUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, input, ...(videoPath !== undefined ? { videoPath } : {}) }),
  });
}

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-server-test-'));
});

afterEach(async () => {
  await agentServer?.close();
  rmSync(workspace, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('agent server', () => {
  it('GET /health → {status:"ok"}', async () => {
    await startServer(new ScriptedProvider([]), [echoTool()]);
    const res = await fetch(`${baseUrl}/health`);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: 'ok' });
  });

  it('POST /sessions:workspaceDir 必须是已存在目录;mode 默认 manual', async () => {
    await startServer(new ScriptedProvider([]), [echoTool()]);

    const bad = await postJson('/sessions', { workspaceDir: path.join(workspace, 'nope') });
    expect(bad.status).toBe(400);
    expect(bad.json).toMatchObject({ error: { code: 'invalid_workspace' } });

    const missing = await postJson('/sessions', {});
    expect(missing.status).toBe(400);

    const sessionId = await createSession();
    const session = agentServer.sessions.get(sessionId);
    expect(session?.mode).toBe('manual');
    expect(session?.workspaceDir).toBe(workspace);
  });

  it('POST /chat 未知 session → 404 {error:{code,message}}', async () => {
    await startServer(new ScriptedProvider([]), [echoTool()]);
    const { status, json } = await postJson('/chat', { sessionId: 'ghost', input: 'hi' });
    expect(status).toBe(404);
    expect(json).toMatchObject({ error: { code: 'session_not_found' } });
  });

  it('POST /chat:工具轮 → 文本轮 → done 的 SSE 事件序列', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'echo', { a: 1 })],
      [text('最终回答')],
    ]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '分析一下', '/data/video.mp4');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));

    const types = events.map((e) => e.type);
    expect(types).toEqual([
      'tool_call_start',
      'tool_result',
      'step_done',
      'text_delta',
      'step_done',
      'done',
    ]);
    expect(events[1]).toMatchObject({ toolCallId: 'c1', name: 'echo', isError: false });
    expect(events[3]).toMatchObject({ text: '最终回答' });
    expect(events[5]).toMatchObject({ reason: 'completed' });

    // 用户消息带 videoPath 说明,工具结果已回灌进第二轮历史。
    const firstHistory = provider.histories[0] ?? [];
    expect(JSON.stringify(firstHistory[0]?.content)).toContain('/data/video.mp4');
    const secondHistory = provider.histories[1] ?? [];
    expect(secondHistory.some((m) => m.role === 'tool' && m.toolCallId === 'c1')).toBe(true);

    // 会话历史已累积:user + assistant + tool + assistant
    expect(agentServer.sessions.get(sessionId)?.messages.map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
    ]);
  });

  it('approval 往返:manual 模式收到 approval_request,POST /approval approved 后继续', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', { path: '/tmp/x' })],
      [text('写完了')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const res = await startChat(sessionId, '写个文件');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);

    // 读到 approval_request 为止
    let approval: SseEvent | null = null;
    const prefix: SseEvent[] = [];
    for (;;) {
      const event = await next();
      if (event === null) break;
      prefix.push(event);
      if (event.type === 'approval_request') {
        approval = event;
        break;
      }
    }
    expect(approval).not.toBeNull();
    expect(approval).toMatchObject({
      toolName: 'write_file',
      approvalRule: 'write_file(/tmp/x)',
    });
    expect(prefix.map((e) => e.type)).toEqual(['tool_call_start', 'approval_request']);

    // 审批通过,流程继续
    const approvalRes = await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    expect(approvalRes.status).toBe(200);

    const rest = await readUntilDone(next);
    const restTypes = rest.map((e) => e.type);
    expect(restTypes).toEqual(['tool_result', 'step_done', 'text_delta', 'step_done', 'done']);
    expect(rest[0]).toMatchObject({ toolCallId: 'c1', name: 'write_file', isError: false });
    expect(rest.at(-1)).toMatchObject({ reason: 'completed' });
  });

  it('POST /approval 未知 requestId → 404', async () => {
    await startServer(new ScriptedProvider([]), [echoTool()]);
    const { status, json } = await postJson('/approval', {
      requestId: 'ghost',
      decision: 'approved',
    });
    expect(status).toBe(404);
    expect(json).toMatchObject({ error: { code: 'approval_not_found' } });
  });

  it('submit_detection 的 stopTurn:先发 detection 事件再 done(stop_turn)', async () => {
    const payload = { binary_encoding: '0_0_0_0_0_0_0_0_0_0_0', normal: true };
    const provider = new ScriptedProvider([[toolCall('c1', 'submit_detection', {})]]);
    await startServer(provider, [submitTool(payload)]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '分析视频');
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));

    const types = events.map((e) => e.type);
    expect(types).toEqual([
      'tool_call_start',
      'tool_result',
      'step_done',
      'detection',
      'done',
    ]);
    expect(events[3]).toMatchObject({ type: 'detection', data: payload });
    expect(events[4]).toMatchObject({ reason: 'stop_turn' });
  });

  it('同 session 的 /chat 互斥:进行中的轮次返回 409', async () => {
    // provider 第一轮发起需要审批的写操作并挂起,期间第二个 /chat 应 409。
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', {})],
      [text('ok')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const res = await startChat(sessionId, 'first');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    // 等审批挂起(说明第一轮正在进行)
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }

    const busy = await postJson('/chat', { sessionId, input: 'second' });
    expect(busy.status).toBe(409);
    expect(busy.json).toMatchObject({ error: { code: 'chat_in_progress' } });

    // 收尾:审批通过,流正常结束
    await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    const rest = await readUntilDone(next);
    expect(rest.at(-1)).toMatchObject({ reason: 'completed' });
  });
});
