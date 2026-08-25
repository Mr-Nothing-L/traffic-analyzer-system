/**
 * POST /sessions/{id}/recall 单元测试:假 ChatProvider + 假工具,起真实
 * node:http 服务(随机端口),覆盖撤回截断正确性(entries 与 kosong messages
 * 同步截断、撤回后续跑 /chat 历史正确、重启恢复后仍一致)与 400/404/409。
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

/** 只读假工具(accesses 为空,manual 模式下直接放行)。 */
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

/** 写文件假工具(manual 模式下触发审批挂起,用于构造 chat_in_progress)。 */
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

// ---------------------------------------------------------------------------
// 测试 harness
// ---------------------------------------------------------------------------

let workspace: string;
let agentServer: AgentServer | undefined;
let baseUrl: string;

async function startServer(
  provider: ScriptedProvider,
  tools: ExecutableTool[],
  extra?: { restoreWorkspaceDirs?: string[] },
): Promise<void> {
  const created = createAgentServer({
    providerFactory: () => ({ provider, model: provider.modelName }),
    toolsFactory: (_session: Session) => {
      const registry = new ToolRegistry();
      for (const tool of tools) registry.register(tool);
      return registry;
    },
    systemPrompt: 'sys',
    ...(extra?.restoreWorkspaceDirs !== undefined
      ? { restoreWorkspaceDirs: extra.restoreWorkspaceDirs }
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

/** 跑完一轮 /chat 并消费完 SSE 流。 */
async function runChatTurn(sessionId: string, input: string): Promise<void> {
  const res = await fetch(`${baseUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, input }),
  });
  expect(res.status).toBe(200);
  if (res.body === null) throw new Error('no body');
  await readUntilDone(sseReader(res.body));
}

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-recall-test-'));
});

afterEach(async () => {
  await agentServer?.close();
  agentServer = undefined;
  rmSync(workspace, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('POST /sessions/{id}/recall', () => {
  it('撤回第二轮:entries 与 messages 同步截断,续跑 /chat 历史正确', async () => {
    // 每轮:工具调用 + 文本 → entries [user, tool, assistant],
    // messages 追加 [user, assistant, tool, assistant]。
    const provider = new ScriptedProvider([
      [toolCall('c1', 'echo', {})],
      [text('第一轮回答')],
      [toolCall('c2', 'echo', {})],
      [text('第二轮回答')],
      [text('第三轮回答')],
    ]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();

    await runChatTurn(sessionId, '第一轮输入');
    await runChatTurn(sessionId, '第二轮输入');

    const before = agentServer?.sessions.get(sessionId);
    expect(before?.entries.map((e) => e.kind)).toEqual([
      'user', 'tool', 'assistant', 'user', 'tool', 'assistant',
    ]);
    expect(before?.messages.map((m) => m.role)).toEqual([
      'user', 'assistant', 'tool', 'assistant', 'user', 'assistant', 'tool', 'assistant',
    ]);

    // 撤回第二轮的 user 条目(index 3):其 messageIndex = 4(第一轮 4 条消息)。
    const recall = await postJson(`/sessions/${sessionId}/recall`, { entryIndex: 3 });
    expect(recall.status).toBe(200);
    expect(recall.json).toEqual({ status: 'ok' });

    const after = agentServer?.sessions.get(sessionId);
    expect(after?.entries.map((e) => e.kind)).toEqual(['user', 'tool', 'assistant']);
    expect(after?.messages.map((m) => m.role)).toEqual([
      'user', 'assistant', 'tool', 'assistant',
    ]);

    // 续跑:provider 收到的历史 = 截断后的 4 条 + 新 user。
    await runChatTurn(sessionId, '第三轮输入');
    const thirdHistory = provider.histories.at(-1) ?? [];
    expect(thirdHistory.map((m) => m.role)).toEqual([
      'user', 'assistant', 'tool', 'assistant', 'user',
    ]);
    const firstUser = thirdHistory[0];
    expect(JSON.stringify(firstUser?.content)).toContain('第一轮输入');
    expect(JSON.stringify(thirdHistory)).not.toContain('第二轮输入');

    // 撤回后时间线只保留第一轮 + 第三轮。
    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual([
      'user', 'tool', 'assistant', 'user', 'assistant',
    ]);
  });

  it('撤回后持久化与内存一致:重建 server 恢复的是截断后的历史', async () => {
    const provider = new ScriptedProvider([
      [text('第一轮回答')],
      [text('第二轮回答')],
      [text('第三轮回答')],
    ]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();

    await runChatTurn(sessionId, '第一轮输入');
    await runChatTurn(sessionId, '第二轮输入');

    // 撤回第二轮 user 条目(index 2,entries: user, assistant, user, assistant)。
    const recall = await postJson(`/sessions/${sessionId}/recall`, { entryIndex: 2 });
    expect(recall.status).toBe(200);

    // 模拟进程重启:从磁盘恢复,恢复的应是截断后的状态。
    await agentServer?.close();
    await startServer(provider, [echoTool()], { restoreWorkspaceDirs: [workspace] });

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'assistant']);
    expect(entries[0]).toMatchObject({ kind: 'user', text: '第一轮输入' });

    // 恢复的 messages 续跑:历史 = 第一轮的 [user, assistant] + 新 user。
    await runChatTurn(sessionId, '第三轮输入');
    const restoredHistory = provider.histories.at(-1) ?? [];
    expect(restoredHistory.map((m) => m.role)).toEqual(['user', 'assistant', 'user']);
    expect(JSON.stringify(restoredHistory)).not.toContain('第二轮输入');
  });

  it('撤回首轮 user 条目(index 0):清空全部历史与标题', async () => {
    const provider = new ScriptedProvider([[text('回答')], [text('第二轮回答')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();
    await runChatTurn(sessionId, '唯一的一轮');

    const recall = await postJson(`/sessions/${sessionId}/recall`, { entryIndex: 0 });
    expect(recall.status).toBe(200);

    const session = agentServer?.sessions.get(sessionId);
    expect(session?.entries).toEqual([]);
    expect(session?.messages).toEqual([]);
    expect(session?.title).toBe('');

    // 续跑等同于全新会话。
    await runChatTurn(sessionId, '重新提问');
    expect(provider.histories.at(-1)?.map((m) => m.role)).toEqual(['user']);
    expect(agentServer?.sessions.get(sessionId)?.title).toBe('重新提问');
  });

  it('未知 session → 404;entryIndex 越界/非 user/非法 → 400', async () => {
    const provider = new ScriptedProvider([[text('回答')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();
    await runChatTurn(sessionId, 'hi');

    const ghost = await postJson('/sessions/ghost/recall', { entryIndex: 0 });
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });

    // entries: [user(0), assistant(1)];index 2 越界,index 1 是 assistant。
    const outOfRange = await postJson(`/sessions/${sessionId}/recall`, { entryIndex: 2 });
    expect(outOfRange.status).toBe(400);
    expect(outOfRange.json).toMatchObject({ error: { code: 'invalid_entry' } });

    const nonUser = await postJson(`/sessions/${sessionId}/recall`, { entryIndex: 1 });
    expect(nonUser.status).toBe(400);
    expect(nonUser.json).toMatchObject({ error: { code: 'invalid_entry' } });

    for (const body of [{}, { entryIndex: -1 }, { entryIndex: 0.5 }, { entryIndex: '0' }]) {
      const bad = await postJson(`/sessions/${sessionId}/recall`, body);
      expect(bad.status).toBe(400);
      expect(bad.json).toMatchObject({ error: { code: 'invalid_request' } });
    }

    // 非法请求不影响已有历史。
    expect(agentServer?.sessions.get(sessionId)?.entries).toHaveLength(2);
    expect(agentServer?.sessions.get(sessionId)?.messages).toHaveLength(2);
  });

  it('chat 进行中 → 409 chat_in_progress', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', {})],
      [text('ok')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const res = await fetch(`${baseUrl}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, input: '写个文件' }),
    });
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    // 等审批挂起(轮次进行中)
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }

    const busy = await postJson(`/sessions/${sessionId}/recall`, { entryIndex: 0 });
    expect(busy.status).toBe(409);
    expect(busy.json).toMatchObject({ error: { code: 'chat_in_progress' } });

    // 收尾:审批通过,流正常结束;历史未被截断。
    await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    const rest = await readUntilDone(next);
    expect(rest.at(-1)).toMatchObject({ reason: 'completed' });
    expect(agentServer?.sessions.get(sessionId)?.entries.length).toBeGreaterThan(0);
  });
});
