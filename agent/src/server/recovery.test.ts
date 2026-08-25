/**
 * 轮次持久化与断连恢复(P1)集成测试:假 ChatProvider + 假工具,起真实
 * node:http 服务(随机端口),覆盖:
 *   - SSE 断连不杀轮次:loop 跑完并落盘,busy 释放;
 *   - 按步增量落盘:轮次进行中磁盘上已可见 user 条目与 user 消息;
 *   - GET /sessions/{id}/events?fromSeq=N:续传条目 + inProgress 标记;
 *   - SSE 事件带 seq(已落盘水位),单调不减,done 时等于条目总数。
 * 不打真实模型 API。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { Message, StreamedMessagePart, ToolCall } from '#/message';
import type {
  ChatProvider,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '#/provider';
import type { Tool } from '#/tool';

import type { ExecutableTool } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { createAgentServer, type AgentServer } from './app';
import type { Session } from './session';
import type { TimelineEntry } from './storage';

// ---------------------------------------------------------------------------
// 假 provider / 假工具(与 server.test.ts 同一模式)
// ---------------------------------------------------------------------------

class ScriptedProvider implements ChatProvider {
  readonly name = 'scripted';
  readonly modelName = 'scripted-model';
  readonly thinkingEffort = null;
  private readonly script: StreamedMessagePart[][];

  constructor(script: StreamedMessagePart[][]) {
    this.script = [...script];
  }

  generate(
    _systemPrompt: string,
    _tools: Tool[],
    _history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
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

/** 只读假工具(accesses 为空,任何模式直接放行)。 */
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

/** 写文件假工具(manual 模式下触发审批挂起)。 */
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
  provider: ScriptedProvider,
  tools: ExecutableTool[],
): Promise<void> {
  const created = createAgentServer({
    providerFactory: () => ({ provider, model: provider.modelName }),
    toolsFactory: (_session: Session) => {
      const registry = new ToolRegistry();
      for (const tool of tools) registry.register(tool);
      return registry;
    },
    systemPrompt: 'sys',
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

interface EventsBody {
  events: { seq: number; entry: TimelineEntry }[];
  inProgress: boolean;
}

async function getEvents(sessionId: string, fromSeq?: number): Promise<EventsBody> {
  const { status, json } = await getJson(
    `/sessions/${sessionId}/events${fromSeq === undefined ? '' : `?fromSeq=${fromSeq}`}`,
  );
  expect(status).toBe(200);
  return json as EventsBody;
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

/** 发起 chat 并读到 approval_request(轮次挂起),返回 requestId 与读取器。 */
async function chatUntilApproval(
  sessionId: string,
  input: string,
  signal?: AbortSignal,
): Promise<{ requestId: string; next: () => Promise<SseEvent | null> }> {
  const res = await fetch(`${baseUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, input }),
    ...(signal !== undefined ? { signal } : {}),
  });
  expect(res.status).toBe(200);
  if (res.body === null) throw new Error('no body');
  const next = sseReader(res.body);
  for (;;) {
    const event = await next();
    if (event === null) throw new Error('stream ended before approval_request');
    if (event.type === 'approval_request') {
      return { requestId: String(event.requestId), next };
    }
  }
}

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-recovery-test-'));
});

afterEach(async () => {
  await agentServer?.close();
  agentServer = undefined;
  rmSync(workspace, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('断连恢复与增量落盘', () => {
  it('SSE 断连不杀轮次:审批放行后 loop 跑完并落盘,busy 释放', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', {})],
      [text('写完了')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    // 挂起审批后断开 SSE(模拟刷新页面)。
    const controller = new AbortController();
    const { requestId } = await chatUntilApproval(sessionId, '写个文件', controller.signal);
    controller.abort();
    await sleep(50); // 等服务端感知 close

    // 轮次仍活着:审批请求还可回执;放行后轮次继续跑完。
    const approval = await postJson('/approval', { requestId, decision: 'approved' });
    expect(approval.status).toBe(200);

    // 轮询 events 端点直到轮次结束。
    let body = await getEvents(sessionId);
    for (let i = 0; i < 100 && body.inProgress; i += 1) {
      await sleep(20);
      body = await getEvents(sessionId);
    }
    expect(body.inProgress).toBe(false);

    // 完整落盘:entries 与 messages 与正常一轮无异。
    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'approval', 'tool', 'assistant']);
    expect(entries[1]).toMatchObject({ kind: 'approval', decision: 'approved' });
    expect(entries[2]).toMatchObject({ kind: 'tool', name: 'write_file', isError: false });
    expect(entries[3]).toMatchObject({ kind: 'assistant', text: '写完了' });
    expect(agentServer?.sessions.get(sessionId)?.messages.map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
    ]);
  });

  it('按步增量落盘:轮次进行中磁盘已可见 user 条目与 user 消息', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', {})],
      [text('写完了')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const { requestId, next } = await chatUntilApproval(sessionId, '写个文件');

    // 审批挂起中:磁盘上 user 条目已落(还带 title);messages 已落 user。
    // approval 条目随 settle(回执 decision 回填内存条目)后的 step_done
    // 一并落盘,挂起期间尚未落盘。
    expect(diskRows(sessionId, 'entries').map((e) => e.kind)).toEqual(['user']);
    expect(diskRows(sessionId, 'messages')).toHaveLength(1);

    // 放行 → 轮次完成:磁盘与内存一致。
    await postJson('/approval', { requestId, decision: 'approved' });
    await readUntilDone(next);
    expect(diskRows(sessionId, 'entries').map((e) => e.kind)).toEqual([
      'user',
      'approval',
      'tool',
      'assistant',
    ]);
    expect(diskRows(sessionId, 'messages').map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
    ]);
    // approval 条目在 settle 回填 decision 后才落盘:磁盘上带 decision。
    const approvalEntry = diskRows(sessionId, 'entries')[1];
    expect(approvalEntry).toMatchObject({ kind: 'approval', decision: 'approved' });
  });

  it('GET /sessions/{id}/events:fromSeq 过滤、inProgress、404/400', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'echo', {})],
      [text('回答')],
      [toolCall('c2', 'write_file', {})],
      [text('ok')],
    ]);
    await startServer(provider, [echoTool(), writeTool()]);
    const sessionId = await createSession('yolo');

    // 一轮完成后:fromSeq=0 返回全部条目,seq 从 1 连续递增。
    const res = await fetch(`${baseUrl}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, input: 'hi' }),
    });
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const all = await getEvents(sessionId, 0);
    expect(all.inProgress).toBe(false);
    expect(all.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(all.events.map((e) => e.entry.kind)).toEqual(['user', 'tool', 'assistant']);

    // fromSeq 过滤:只返回水位之后的部分。
    const tail = await getEvents(sessionId, 1);
    expect(tail.events.map((e) => e.seq)).toEqual([2, 3]);
    expect((await getEvents(sessionId, 3)).events).toEqual([]);
    // fromSeq 缺省 = 0。
    expect((await getEvents(sessionId)).events).toHaveLength(3);

    // 轮次进行中 → inProgress=true。
    const manual = await createSession('manual');
    const { requestId, next } = await chatUntilApproval(manual, '写个文件');
    expect((await getEvents(manual)).inProgress).toBe(true);
    await postJson('/approval', { requestId, decision: 'approved' });
    await readUntilDone(next);
    expect((await getEvents(manual)).inProgress).toBe(false);

    // 未知 session → 404;非法 fromSeq → 400。
    const ghost = await getJson('/sessions/ghost/events');
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });
    for (const bad of ['abc', '-1', '1.5']) {
      const resBad = await getJson(`/sessions/${sessionId}/events?fromSeq=${bad}`);
      expect(resBad.status).toBe(400);
      expect(resBad.json).toMatchObject({ error: { code: 'invalid_request' } });
    }
  });

  it('SSE 事件带 seq(已落盘水位):单调不减,done 时等于条目总数', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'echo', {})],
      [text('回答')],
    ]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');

    const res = await fetch(`${baseUrl}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, input: 'hi' }),
    });
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));

    const seqs = events.map((e) => e.seq);
    for (const seq of seqs) expect(typeof seq).toBe('number');
    const sorted = [...seqs].sort((a, b) => (a as number) - (b as number));
    expect(seqs).toEqual(sorted);

    // done 事件的水位 = 落盘条目总数(user/tool/assistant = 3)。
    const done = events.at(-1);
    expect(done).toMatchObject({ type: 'done', seq: 3 });
    const history = await getJson(`/sessions/${sessionId}/history`);
    expect((history.json as { entries: TimelineEntry[] }).entries).toHaveLength(3);
  });
});
