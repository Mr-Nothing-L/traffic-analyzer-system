/**
 * steer / cancel(P2)集成测试:假 ChatProvider + 可控假工具,起真实
 * node:http 服务(随机端口),覆盖:
 *   - POST /sessions/{id}/cancel:进行中轮次 → loop 以 cancelled 收尾且
 *     已完成部分落盘完整;审批挂起期间 cancel → 挂起审批立即以 cancelled
 *     落定、轮次立即收尾(不拖满审批超时);无进行中轮次 → 409
 *     no_active_turn;未知 session 404;
 *   - POST /sessions/{id}/steer:注入后下一步 generate 的 history 含新 user
 *     消息,SSE 有 steer 事件,条目/消息增量落盘;轮末仍未消费的 steer 被
 *     丢弃并发 steer_dropped 事件,不进入下一轮 messages;无进行中轮次 → 409;
 *     断连(无活跃流)时仍落盘。
 * 不打真实模型 API。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type {
  Message,
  StreamedMessagePart,
  ToolCall,
  ChatProvider,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '../llm/kosong';
import { ScriptedProvider, streamOf, text, toolCall } from '../testkit/scriptedProvider';

import type { ExecutableTool } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { createAgentServer, type AgentServer } from './app';
import type { Session } from './session';

// ---------------------------------------------------------------------------
// 假 provider / 假工具(与 recovery.test.ts 同一模式)
// ---------------------------------------------------------------------------

/** 手动放行的 provider:holdNextGenerate() 后的下一次 generate 挂起,直到
 *  返回的 release 被调用——制造「steer 在最终 generate 期间到达」的确定性
 *  窗口(此后本轮不会再有 shouldSteer 消费点)。 */
class HoldableProvider implements ChatProvider {
  readonly name = 'holdable';
  readonly modelName = 'holdable-model';
  readonly thinkingEffort = null;
  readonly histories: Message[][] = [];
  private readonly script: StreamedMessagePart[][];
  private gate: Promise<void> = Promise.resolve();

  constructor(script: StreamedMessagePart[][]) {
    this.script = [...script];
  }

  holdNextGenerate(): () => void {
    let release!: () => void;
    this.gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    return release;
  }

  generate(
    _systemPrompt: string,
    _tools: Tool[],
    history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
    this.histories.push(history.map((m) => m));
    const gate = this.gate;
    this.gate = Promise.resolve();
    return gate.then(() => {
      const parts = this.script.shift();
      if (parts === undefined) return Promise.reject(new Error('script exhausted'));
      return streamOf(parts);
    });
  }

  withThinking(_effort: ThinkingEffort): ChatProvider {
    return this;
  }
}

/** 闸门工具:execute 开始后可等待,release() 后返回成功。 */
function gatedTool(): {
  tool: ExecutableTool;
  started: Promise<void>;
  release: () => void;
} {
  let markStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    started,
    release,
    tool: {
      name: 'gated',
      description: 'fake gated tool',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'gated()',
        execute: () => {
          markStarted();
          return gate.then(() => ({ output: 'gated-ok' }));
        },
      }),
    },
  };
}

/** 可中止工具:execute 开始后挂起,直到 ctx.signal abort 时拒绝。 */
function abortableTool(): { tool: ExecutableTool; started: Promise<void> } {
  let markStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  return {
    started,
    tool: {
      name: 'abortable',
      description: 'fake abortable tool',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'abortable()',
        execute: (ctx) => {
          markStarted();
          return new Promise((_resolve, reject) => {
            ctx.signal.addEventListener('abort', () => reject(new Error('aborted')), {
              once: true,
            });
          });
        },
      }),
    },
  };
}

/** 写文件假工具(manual 模式下触发审批挂起,与 server.test.ts 同款)。 */
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

// ---------------------------------------------------------------------------
// HTTP / SSE 测试辅助
// ---------------------------------------------------------------------------

interface SseEvent {
  readonly type: string;
  readonly [key: string]: unknown;
}

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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// 测试 harness
// ---------------------------------------------------------------------------

let workspace: string;
let agentServer: AgentServer | undefined;
let baseUrl: string;

async function startServer(
  provider: ChatProvider,
  tools: ExecutableTool[],
  extra?: { approvalTimeoutMs?: number },
): Promise<void> {
  const created = createAgentServer({
    providerFactory: () => ({ provider, model: provider.modelName }),
    toolsFactory: (_session: Session) => {
      const registry = new ToolRegistry();
      for (const tool of tools) registry.register(tool);
      return registry;
    },
    systemPrompt: 'sys',
    ...(extra?.approvalTimeoutMs !== undefined
      ? { approvalTimeoutMs: extra.approvalTimeoutMs }
      : {}),
  });
  agentServer = created;
  await new Promise<void>((resolve) => {
    created.server.listen(0, '127.0.0.1', resolve);
  });
  const address = created.server.address();
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

async function getJson(urlPath: string): Promise<{ status: number; json: unknown }> {
  const res = await fetch(`${baseUrl}${urlPath}`);
  return { status: res.status, json: await res.json() };
}

async function createSession(mode: string = 'yolo'): Promise<string> {
  const { status, json } = await postJson('/sessions', { workspaceDir: workspace, mode });
  expect(status).toBe(200);
  return (json as { sessionId: string }).sessionId;
}

async function startChat(
  sessionId: string,
  input: string,
  signal?: AbortSignal,
): Promise<() => Promise<SseEvent | null>> {
  const res = await fetch(`${baseUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, input }),
    ...(signal !== undefined ? { signal } : {}),
  });
  expect(res.status).toBe(200);
  if (res.body === null) throw new Error('no body');
  return sseReader(res.body);
}

/** 直读磁盘 entries/messages(绕过内存态,验证落盘)。 */
function diskRows(sessionId: string, table: 'entries' | 'messages'): Record<string, unknown>[] {
  const db = new DatabaseSync(path.join(workspace, '.agent', 'sessions.db'));
  try {
    const column = table === 'entries' ? 'entry_json' : 'message_json';
    return db
      .prepare(`SELECT ${column} FROM ${table} WHERE session_id = ? ORDER BY seq ASC`)
      .all(sessionId)
      .map((row) => JSON.parse(String(row[column])) as Record<string, unknown>);
  } finally {
    db.close();
  }
}

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-steer-test-'));
});

afterEach(async () => {
  await agentServer?.close();
  agentServer = undefined;
  rmSync(workspace, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('POST /sessions/{id}/cancel', () => {
  it('取消进行中轮次:loop 以 cancelled 收尾,已完成部分落盘完整', async () => {
    const provider = new ScriptedProvider({ script: [[toolCall('c1', 'abortable', {})]] });
    const { tool, started } = abortableTool();
    await startServer(provider, [tool]);
    const sessionId = await createSession();

    const next = await startChat(sessionId, '跑一个长任务');
    await started; // 工具执行中

    const cancel = await postJson(`/sessions/${sessionId}/cancel`, {});
    expect(cancel.status).toBe(200);
    expect(cancel.json).toEqual({ status: 'ok' });

    const events = await readUntilDone(next);
    expect(events.at(-1)).toMatchObject({ type: 'done', reason: 'cancelled' });

    // 落盘完整:user 条目 + tool 条目(取消合成 isError 结果);messages 与
    // 正常半截轮次一致(user/assistant/tool),恢复时无需尾部修复。
    expect(diskRows(sessionId, 'entries').map((e) => e.kind)).toEqual(['user', 'tool']);
    expect(diskRows(sessionId, 'entries')[1]).toMatchObject({ isError: true });
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
    ]);
    // busy 释放:可以再发 /chat。
    const events2 = await getJson(`/sessions/${sessionId}/events`);
    expect((events2.json as { inProgress: boolean }).inProgress).toBe(false);
  });

  it('审批挂起期间 cancel:挂起审批立即以 cancelled 落定,轮次立即收尾', async () => {
    // approvalTimeoutMs 拉大:若 cancel 打不断审批挂起,本用例会拖满超时。
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'write_file', {})],
      [text('不会到达')], // 脚本富余:cancel 后不应再 generate
    ] });
    await startServer(provider, [writeTool()], { approvalTimeoutMs: 60_000 });
    const sessionId = await createSession('manual');

    const next = await startChat(sessionId, '写个文件');
    let requestId = '';
    for (;;) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') {
        requestId = (event as unknown as { requestId: string }).requestId;
        break;
      }
    }

    const cancel = await postJson(`/sessions/${sessionId}/cancel`, {});
    expect(cancel.status).toBe(200);
    expect(cancel.json).toEqual({ status: 'ok' });

    const events = await readUntilDone(next);
    expect(events.at(-1)).toMatchObject({ type: 'done', reason: 'cancelled' });
    // 审批被取消语义落定后工具合成拒绝结果回灌,轮次在下一步边界收尾,
    // 不再发起第二次 generate。
    expect(provider.histories).toHaveLength(1);

    // 审批条目以 cancelled 落定;工具条目 isError;messages 半截不悬挂。
    expect(diskRows(sessionId, 'entries').map((e) => e.kind)).toEqual([
      'user',
      'approval',
      'tool',
    ]);
    expect(diskRows(sessionId, 'entries')[1]).toMatchObject({
      kind: 'approval',
      requestId,
      decision: 'cancelled',
    });
    expect(diskRows(sessionId, 'entries')[2]).toMatchObject({ kind: 'tool', isError: true });
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
    ]);

    // 事后回执已落定的审批 → 404;busy 已释放。
    const late = await postJson('/approval', { requestId, decision: 'approved' });
    expect(late.status).toBe(404);
    const events2 = await getJson(`/sessions/${sessionId}/events`);
    expect((events2.json as { inProgress: boolean }).inProgress).toBe(false);
  });

  it('无进行中轮次 → 409 no_active_turn;未知 session → 404', async () => {
    const provider = new ScriptedProvider({ script: [] });
    await startServer(provider, []);
    const sessionId = await createSession();

    const idle = await postJson(`/sessions/${sessionId}/cancel`, {});
    expect(idle.status).toBe(409);
    expect(idle.json).toMatchObject({ error: { code: 'no_active_turn' } });

    const ghost = await postJson('/sessions/ghost/cancel', {});
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });
  });
});

describe('POST /sessions/{id}/steer', () => {
  it('注入后下一步 generate 的 history 含新 user 消息,条目/消息增量落盘', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'gated', {})],
      [text('收到,换方向')],
    ] });
    const { tool, started, release } = gatedTool();
    await startServer(provider, [tool]);
    const sessionId = await createSession();

    const next = await startChat(sessionId, '先分析这个视频');
    await started; // 工具执行中:轮次在进行

    const steer = await postJson(`/sessions/${sessionId}/steer`, { input: '换个方向,先看车牌' });
    expect(steer.status).toBe(200);
    expect(steer.json).toEqual({ status: 'ok', queued: true });

    release();
    const events = await readUntilDone(next);
    expect(events.at(-1)).toMatchObject({ type: 'done', reason: 'completed' });

    // SSE 有 steer 事件。
    expect(events.some((e) => e.type === 'steer' && e.text === '换个方向,先看车牌')).toBe(true);

    // 第二步 generate 的 history 尾部是注入的 user 消息。
    expect(provider.histories).toHaveLength(2);
    const last = provider.histories[1]?.at(-1);
    expect(last?.role).toBe('user');
    const lastText = last?.content
      .filter((p) => p.type === 'text')
      .map((p) => (p.type === 'text' ? p.text : ''))
      .join('');
    expect(lastText).toBe('换个方向,先看车牌');

    // 落盘:steer 的 user 条目插在 tool 条目之后、assistant 之前;messages 同理。
    expect(diskRows(sessionId, 'entries').map((e) => e.kind)).toEqual([
      'user',
      'tool',
      'user',
      'assistant',
    ]);
    expect(diskRows(sessionId, 'entries')[2]).toMatchObject({
      kind: 'user',
      text: '换个方向,先看车牌',
    });
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'user',
      'assistant',
    ]);
  });

  it('轮末(cancel)未消费的 steer:丢弃并发 steer_dropped,不进入下一轮 messages', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'abortable', {})],
      [text('第二轮回答')],
    ] });
    const { tool, started } = abortableTool();
    await startServer(provider, [tool]);
    const sessionId = await createSession();

    const next = await startChat(sessionId, '跑任务');
    await started; // 工具执行中:steer 排队,但 cancel 后不会再有消费点
    const steer = await postJson(`/sessions/${sessionId}/steer`, { input: '换方向' });
    expect(steer.status).toBe(200);
    const cancel = await postJson(`/sessions/${sessionId}/cancel`, {});
    expect(cancel.status).toBe(200);

    const events = await readUntilDone(next);
    expect(events.at(-1)).toMatchObject({ type: 'done', reason: 'cancelled' });
    // 插话未生效:丢弃并经 SSE 告知(steer_dropped 先于 done)。
    expect(events.some((e) => e.type === 'steer_dropped' && e.text === '换方向')).toBe(true);
    // steer 的 user 消息未注入、未落盘。
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
    ]);

    // 下一轮:新 user 消息直接跟在上一轮 tool 之后,不含被丢弃的插话。
    const next2 = await startChat(sessionId, '第二轮');
    await readUntilDone(next2);
    expect(provider.histories).toHaveLength(2);
    const second = provider.histories[1] ?? [];
    expect(second.map((m) => m.role)).toEqual(['user', 'assistant', 'tool', 'user']);
    expect(JSON.stringify(second)).not.toContain('换方向');
  });

  it('轮次正常结束:最终 generate 期间到达的 steer 丢弃并发 steer_dropped,不进入下一轮', async () => {
    // 单步轮次:唯一一次 generate 挂起期间 steer 到达,此后不再有 shouldSteer
    // 消费点,轮末必须丢弃(旧行为会留给下一轮、注入在新消息之后)。
    const provider = new HoldableProvider([[text('第一轮完')], [text('第二轮完')]]);
    await startServer(provider, []);
    const sessionId = await createSession();

    const release = provider.holdNextGenerate();
    const next = await startChat(sessionId, '第一轮');
    const steer = await postJson(`/sessions/${sessionId}/steer`, { input: '插话太晚了' });
    expect(steer.status).toBe(200);
    expect(steer.json).toEqual({ status: 'ok', queued: true });
    release();

    const events = await readUntilDone(next);
    expect(events.at(-1)).toMatchObject({ type: 'done', reason: 'completed' });
    expect(events.some((e) => e.type === 'steer_dropped' && e.text === '插话太晚了')).toBe(true);
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual(['user', 'assistant']);

    // 下一轮 messages:上一轮 user/assistant + 新 user,没有插话。
    const next2 = await startChat(sessionId, '第二轮');
    await readUntilDone(next2);
    const second = provider.histories[1] ?? [];
    expect(second.map((m) => m.role)).toEqual(['user', 'assistant', 'user']);
    expect(JSON.stringify(second)).not.toContain('插话太晚了');
  });

  it('无进行中轮次 → 409 no_active_turn;未知 session → 404;空 input → 400', async () => {
    const provider = new ScriptedProvider({ script: [] });
    await startServer(provider, []);
    const sessionId = await createSession();

    const idle = await postJson(`/sessions/${sessionId}/steer`, { input: 'hi' });
    expect(idle.status).toBe(409);
    expect(idle.json).toMatchObject({ error: { code: 'no_active_turn' } });

    const ghost = await postJson('/sessions/ghost/steer', { input: 'hi' });
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });

    const bad = await postJson(`/sessions/${sessionId}/steer`, { input: '' });
    expect(bad.status).toBe(400);
    expect(bad.json).toMatchObject({ error: { code: 'invalid_request' } });
  });

  it('断连(无活跃流)时 steer 仍落盘,轮次继续跑完', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'gated', {})],
      [text('完成了')],
    ] });
    const { tool, started, release } = gatedTool();
    await startServer(provider, [tool]);
    const sessionId = await createSession();

    const controller = new AbortController();
    await startChat(sessionId, '跑一个长任务', controller.signal);
    await started;

    // 客户端断开(模拟刷新页面),轮次不 abort(P1 语义)。
    controller.abort();
    await sleep(50);

    const steer = await postJson(`/sessions/${sessionId}/steer`, { input: '断连时注入' });
    expect(steer.status).toBe(200);
    expect(steer.json).toEqual({ status: 'ok', queued: true });

    release();
    // 轮询 events 端点直到轮次结束。
    let body = (await getJson(`/sessions/${sessionId}/events`)).json as { inProgress: boolean };
    for (let i = 0; i < 100 && body.inProgress; i += 1) {
      await sleep(20);
      body = (await getJson(`/sessions/${sessionId}/events`)).json as { inProgress: boolean };
    }
    expect(body.inProgress).toBe(false);

    // 断连只丢 SSE,不丢落盘:steer 条目与消息都在。
    expect(diskRows(sessionId, 'entries').map((e) => e.kind)).toEqual([
      'user',
      'tool',
      'user',
      'assistant',
    ]);
    expect(diskRows(sessionId, 'entries')[2]).toMatchObject({
      kind: 'user',
      text: '断连时注入',
    });
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'user',
      'assistant',
    ]);
  });
});
