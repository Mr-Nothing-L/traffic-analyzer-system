/**
 * server 模块单元测试:假 ChatProvider(ScriptedProvider,参考
 * loop/loop.test.ts)+ 假工具,起真实 node:http 服务(随机端口),
 * 覆盖 /sessions → /chat 的 SSE 事件序列、approval 往返、未知 session 404。
 * 不打真实模型 API。
 */
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Message, StreamedMessagePart, ToolCall } from '#/message';
import type {
  ChatProvider,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '#/provider';
import type { Tool } from '#/tool';
import type { TokenUsage } from '#/usage';

import type { ExecutableTool, ExecutableToolResult } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { createAgentServer, defaultSystemPrompt, type AgentServer } from './app';
import { SUMMARY_PREFIX, SUMMARY_SYSTEM_PROMPT } from '../loop/summarize';
import type { Session } from './session';
import type { TimelineEntry } from './storage';

// ---------------------------------------------------------------------------
// 假 provider / 假工具
// ---------------------------------------------------------------------------

class ScriptedProvider implements ChatProvider {
  readonly name = 'scripted';
  readonly modelName = 'scripted-model';
  readonly thinkingEffort = null;
  readonly histories: Message[][] = [];
  private readonly script: StreamedMessagePart[][];
  /** 与 script 逐步对应的 usage(缺省 null = provider 不上报)。 */
  private readonly usages: (TokenUsage | null)[];
  /** 摘要调用的应答队列;Error = 摘要失败(测回退);队列空 = 默认失败。 */
  private readonly summaries: (StreamedMessagePart[] | Error)[];

  constructor(
    script: StreamedMessagePart[][],
    usages: (TokenUsage | null)[] = [],
    summaries: (StreamedMessagePart[] | Error)[] = [],
  ) {
    this.script = [...script];
    this.usages = [...usages];
    this.summaries = [...summaries];
  }

  generate(
    systemPrompt: string,
    _tools: Tool[],
    history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
    // 按调用内容分场景:摘要调用(system prompt 为摘要指令)走 summaries 队列。
    if (systemPrompt === SUMMARY_SYSTEM_PROMPT) {
      const summary = this.summaries.shift();
      if (summary === undefined) return Promise.reject(new Error('summary not scripted'));
      if (summary instanceof Error) return Promise.reject(summary);
      return Promise.resolve(streamOf(summary));
    }
    this.histories.push(history.map((m) => m));
    const parts = this.script.shift();
    if (parts === undefined) return Promise.reject(new Error('script exhausted'));
    return Promise.resolve(streamOf(parts, this.usages.shift() ?? null));
  }

  withThinking(_effort: ThinkingEffort): ChatProvider {
    return this;
  }
}

function streamOf(parts: StreamedMessagePart[], usage: TokenUsage | null = null): StreamedMessage {
  return {
    async *[Symbol.asyncIterator]() {
      for (const part of parts) yield part;
    },
    id: null,
    usage,
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
let agentServer: AgentServer | undefined;
let baseUrl: string;

async function startServer(
  provider: ScriptedProvider,
  tools: ExecutableTool[],
  extra?: { restoreWorkspaceDirs?: string[]; contextTokens?: number },
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
    ...(extra?.contextTokens !== undefined ? { contextTokens: extra.contextTokens } : {}),
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

function startChat(
  sessionId: string,
  input: string,
  videoPath?: string,
  images?: string[],
): Promise<Response> {
  return fetch(`${baseUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sessionId,
      input,
      ...(videoPath !== undefined ? { videoPath } : {}),
      ...(images !== undefined ? { images } : {}),
    }),
  });
}

async function getJson(
  urlPath: string,
  method: string = 'GET',
): Promise<{ status: number; json: unknown }> {
  const res = await fetch(`${baseUrl}${urlPath}`, { method });
  return { status: res.status, json: await res.json() };
}

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-server-test-'));
});

afterEach(async () => {
  await agentServer?.close();
  agentServer = undefined;
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
    const session = agentServer?.sessions.get(sessionId);
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
    expect(agentServer?.sessions.get(sessionId)?.messages.map((m) => m.role)).toEqual([
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

  it('持久化往返:重建 server 后 history 完整、kosong messages 恢复供续跑', async () => {
    const provider = new ScriptedProvider([[text('第一轮回答')], [text('第二轮回答')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '第一轮输入');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    // DB 文件已落在 workspace 的 .agent/ 下
    expect(existsSync(path.join(workspace, '.agent', 'sessions.db'))).toBe(true);

    // 重建 server(模拟进程重启),从磁盘恢复 session
    await agentServer?.close();
    await startServer(provider, [echoTool()], { restoreWorkspaceDirs: [workspace] });

    const list = await getJson('/sessions');
    expect(list.status).toBe(200);
    const summaries = (list.json as { sessions: { id: string; title: string }[] }).sessions;
    expect(summaries.map((s) => s.id)).toEqual([sessionId]);
    expect(summaries[0]?.title).toBe('第一轮输入');

    const history = await getJson(`/sessions/${sessionId}/history`);
    expect(history.status).toBe(200);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'assistant']);
    expect(entries[0]).toMatchObject({ kind: 'user', text: '第一轮输入', images: [] });
    expect(entries[1]).toMatchObject({ kind: 'assistant', text: '第一轮回答', think: '' });

    // 续跑:恢复的 kosong messages 应出现在 provider 收到的历史里
    const res2 = await startChat(sessionId, '第二轮输入');
    if (res2.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res2.body));
    const secondHistory = provider.histories[1] ?? [];
    expect(secondHistory.map((m) => m.role)).toEqual(['user', 'assistant', 'user']);
  });

  it('GET /sessions:列表字段齐全,title 取首轮用户输入前 30 字', async () => {
    const provider = new ScriptedProvider([[text('ok')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');
    const longInput = '一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十';

    const res = await startChat(sessionId, longInput);
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const { status, json } = await getJson('/sessions');
    expect(status).toBe(200);
    const sessions = (json as { sessions: Record<string, unknown>[] }).sessions;
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({
      id: sessionId,
      workspaceDir: workspace,
      mode: 'yolo',
      title: longInput.slice(0, 30),
    });
    expect(typeof sessions[0]?.createdAt).toBe('number');
    expect(typeof sessions[0]?.lastActiveAt).toBe('number');
  });

  it('DELETE /sessions/{id}:清理内存与磁盘;未知 id → 404', async () => {
    const provider = new ScriptedProvider([[text('ok')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '待删除的会话');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const del = await getJson(`/sessions/${sessionId}`, 'DELETE');
    expect(del.status).toBe(200);
    expect(del.json).toEqual({ status: 'ok' });

    expect((await getJson(`/sessions/${sessionId}/history`)).status).toBe(404);
    expect((await getJson('/sessions', 'DELETE')).status).toBe(404);
    const ghost = await getJson('/sessions/ghost', 'DELETE');
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });

    // 重建后磁盘上也不存在该 session
    await agentServer?.close();
    await startServer(provider, [echoTool()], { restoreWorkspaceDirs: [workspace] });
    const list = await getJson('/sessions');
    expect((list.json as { sessions: unknown[] }).sessions).toEqual([]);
  });

  it('images 附件:转成 image ContentPart 进入 user message,上限 4 张', async () => {
    const provider = new ScriptedProvider([[text('看到了')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');

    const images = [
      'aGVsbG8=', // 裸 base64 → 补 dataURL 前缀
      'data:image/jpeg;base64,anBlZw==', // dataURL 原样保留
      'aW1nMw==',
      'aW1nNA==',
      'aW1nNQ==', // 超出上限被丢弃
    ];
    const res = await startChat(sessionId, '看图分析', undefined, images);
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const firstHistory = provider.histories[0] ?? [];
    const userMessage = firstHistory[0];
    const imageParts = (userMessage?.content ?? []).filter((p) => p.type === 'image_url');
    expect(imageParts).toEqual([
      { type: 'image_url', imageUrl: { url: 'data:image/png;base64,aGVsbG8=' } },
      { type: 'image_url', imageUrl: { url: 'data:image/jpeg;base64,anBlZw==' } },
      { type: 'image_url', imageUrl: { url: 'data:image/png;base64,aW1nMw==' } },
      { type: 'image_url', imageUrl: { url: 'data:image/png;base64,aW1nNA==' } },
    ]);

    // user 条目里保留同样的 4 张图
    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const userEntry = entries[0];
    expect(userEntry?.kind).toBe('user');
    expect(userEntry !== undefined && userEntry.kind === 'user' ? userEntry.images : []).toHaveLength(4);
  });

  it('approval 条目:request 与回执 decision 都进历史', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', { path: '/tmp/x' })],
      [text('写完了')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const res = await startChat(sessionId, '写个文件');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    let requestId: string | undefined;
    for (;;) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') {
        requestId = (event as unknown as { requestId: string }).requestId;
        break;
      }
    }
    await postJson('/approval', { requestId, decision: 'approved' });
    await readUntilDone(next);

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'approval', 'tool', 'assistant']);
    expect(entries[1]).toMatchObject({
      kind: 'approval',
      requestId,
      toolName: 'write_file',
      decision: 'approved',
    });
    expect(entries[2]).toMatchObject({ kind: 'tool', name: 'write_file', isError: false });
  });

  it('context_usage:SSE 透传真实用量,GET /sessions 摘要带 usedTokens', async () => {
    const provider = new ScriptedProvider(
      [[text('回答')]],
      [{ inputOther: 500, inputCacheRead: 60, inputCacheCreation: 7, output: 40 }],
    );
    await startServer(provider, [echoTool()], { contextTokens: 8000 });
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, 'hi');
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));

    // inputCacheCreation 不计入上下文占用:500 + 60 + 40 = 600
    const usage = events.find((e) => e.type === 'context_usage');
    expect(usage).toMatchObject({ usedTokens: 600, maxTokens: 8000 });

    const list = await getJson('/sessions');
    const sessions = (list.json as { sessions: Record<string, unknown>[] }).sessions;
    expect(sessions[0]).toMatchObject({ id: sessionId, usedTokens: 600 });
  });

  it('POST /sessions/{id}/compact:LLM 摘要替换压缩区,返回 summarized 与前后 token 估算', async () => {
    // 大输出工具:压缩后 before/after token 估算差异明显
    const bigEcho: ExecutableTool = {
      name: 'echo',
      description: 'fake echo tool',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'echo()',
        execute: () => Promise.resolve({ output: 'x'.repeat(2000) }),
      }),
    };
    const summaryText = '视频 演示区/v1.mp4;事件 2 检出,证据帧 f3,置信度中;结论尚未提交';
    const provider = new ScriptedProvider(
      [
        [toolCall('c1', 'echo', {})],
        [text('第一轮完')],
        [toolCall('c2', 'echo', {})],
        [text('第二轮完')],
      ],
      [],
      [[text(summaryText)]],
    );
    await startServer(provider, [bigEcho]);
    const sessionId = await createSession('yolo');

    for (const input of ['第一轮', '第二轮']) {
      const res = await startChat(sessionId, input);
      if (res.body === null) throw new Error('no body');
      await readUntilDone(sseReader(res.body));
    }

    const compact = await postJson(`/sessions/${sessionId}/compact`, {});
    expect(compact.status).toBe(200);
    const body = compact.json as {
      status: string;
      compacted: boolean;
      summarized: boolean;
      beforeTokens: number;
      afterTokens: number;
    };
    expect(body.status).toBe('ok');
    expect(body.compacted).toBe(true);
    expect(body.summarized).toBe(true);
    expect(body.afterTokens).toBeLessThan(body.beforeTokens);

    // 压缩区(第一轮)被一条摘要 user 消息替换;保留区(第二轮)不受影响
    const messages = agentServer?.sessions.get(sessionId)?.messages ?? [];
    const first = messages[0];
    expect(first?.role).toBe('user');
    const firstText = JSON.stringify(first?.content);
    expect(firstText).toContain(SUMMARY_PREFIX);
    expect(firstText).toContain('演示区/v1.mp4');
    expect(messages.some((m) => m.toolCallId === 'c1')).toBe(false);
    const recent = messages.find((m) => m.toolCallId === 'c2');
    expect(JSON.stringify(recent?.content)).toContain('x'.repeat(2000));

    // 压缩已落盘:重建 server(模拟进程重启)后恢复的仍是压缩后的历史
    await agentServer?.close();
    await startServer(provider, [bigEcho], { restoreWorkspaceDirs: [workspace] });
    const restored = agentServer?.sessions.get(sessionId)?.messages ?? [];
    expect(restored.length).toBe(messages.length);
    expect(JSON.stringify(restored[0]?.content)).toContain(SUMMARY_PREFIX);
    // 压缩区(第一轮,含 c1 的大输出)已折叠;保留区(第二轮 c2)原样保留
    expect(restored.some((m) => m.toolCallId === 'c1')).toBe(false);
    expect(restored.some((m) => m.toolCallId === 'c2')).toBe(true);
  });

  it('POST /sessions/{id}/compact:单轮会话无可压缩内容 → noop(compacted=false)', async () => {
    const provider = new ScriptedProvider([[text('ok')]]);
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, 'hi');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const compact = await postJson(`/sessions/${sessionId}/compact`, {});
    expect(compact.status).toBe(200);
    expect(compact.json).toMatchObject({ status: 'ok', compacted: false, summarized: false });
  });

  it('POST /sessions/{id}/compact:未知 session → 404;进行中 → 409', async () => {
    const provider = new ScriptedProvider([
      [toolCall('c1', 'write_file', {})],
      [text('ok')],
    ]);
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const ghost = await postJson('/sessions/ghost/compact', {});
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });

    // 挂起审批使轮次保持进行中
    const res = await startChat(sessionId, '写个文件');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }

    const busy = await postJson(`/sessions/${sessionId}/compact`, {});
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

  it('defaultSystemPrompt:chat_system.md 缺失时回退 detect_system.md 并打警告', () => {
    const promptsDir = mkdtempSync(path.join(tmpdir(), 'agent-prompts-test-'));
    try {
      writeFileSync(path.join(promptsDir, 'detect_system.md'), 'DETECT-PROMPT');
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      try {
        expect(defaultSystemPrompt(promptsDir)).toBe('DETECT-PROMPT');
        expect(warnSpy).toHaveBeenCalledOnce();

        warnSpy.mockClear();
        writeFileSync(path.join(promptsDir, 'chat_system.md'), 'CHAT-PROMPT');
        expect(defaultSystemPrompt(promptsDir)).toBe('CHAT-PROMPT');
        expect(warnSpy).not.toHaveBeenCalled();
      } finally {
        warnSpy.mockRestore();
      }
    } finally {
      rmSync(promptsDir, { recursive: true, force: true });
    }
  });
});
