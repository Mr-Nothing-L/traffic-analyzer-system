/**
 * server 模块单元测试:假 ChatProvider(ScriptedProvider,参考
 * loop/loop.test.ts)+ 假工具,起真实 node:http 服务(随机端口),
 * 覆盖 /sessions → /chat 的 SSE 事件序列、approval 往返、未知 session 404。
 * 不打真实模型 API。
 */
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  ScriptedProvider,
  streamOf,
  text,
  toolCall,
} from '../testkit/scriptedProvider';

import type { ExecutableTool, ExecutableToolResult } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import { createAgentServer, defaultSystemPrompt, type AgentServer } from './app';
import { loadEventContract } from '../tools/builtin/eventContract';
import { SUMMARY_PREFIX } from '../loop/summarize';
import type { Session } from './session';
import type { TimelineEntry } from './storage';

// ---------------------------------------------------------------------------
// 假 provider / 假工具
// ---------------------------------------------------------------------------

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
    resolveExecution: (rawInput: unknown) => {
      const parsed = (rawInput ?? {}) as { path?: string; content?: string };
      const filePath = parsed.path ?? '/tmp/x';
      return {
        accesses: [{ kind: 'file' as const, operation: 'write' as const, path: filePath }],
        approvalRule: `write_file(${filePath})`,
        execute: () => Promise.resolve({ output: 'write-ok' }),
      };
    },
  };
}

/** 运行脚本假工具(manual 模式下触发 fallback-ask)。 */
function runScriptTool(workspaceDir: string): ExecutableTool {
  return {
    name: 'run_script',
    description: 'fake run script tool',
    parameters: { type: 'object' },
    resolveExecution: (rawInput: unknown) => {
      const parsed = (rawInput ?? {}) as { path?: string };
      const rawPath = parsed.path ?? 'script.py';
      const scriptPath = path.isAbsolute(rawPath) ? rawPath : path.join(workspaceDir, rawPath);
      return {
        accesses: [{ kind: 'all' as const }],
        approvalRule: `run_script(${scriptPath})`,
        execute: () => Promise.resolve({ output: 'script-ok' }),
      };
    },
  };
}

/** 其它会触发审批的假工具(用于 preview 回退到 JSON 的场景)。 */
function otherTool(): ExecutableTool {
  return {
    name: 'custom_tool',
    description: 'fake custom tool',
    parameters: { type: 'object' },
    resolveExecution: (rawInput: unknown) => {
      const parsed = (rawInput ?? {}) as { path?: string };
      return {
        accesses: [{ kind: 'file' as const, operation: 'write' as const, path: parsed.path ?? '/tmp/x' }],
        approvalRule: `custom_tool(${parsed.path ?? '/tmp/x'})`,
        execute: () => Promise.resolve({ output: 'custom-ok' }),
      };
    },
  };
}

/** 模拟 submit_detection:stopTurn + payload 携带结构化检测载荷。 */
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
          payload,
        }),
    }),
  };
}

/** 执行时经 ctx.onSubagentEvent 上报嵌套(子代理)事件的假工具。 */
function subEmitTool(childEvents: unknown[], note: string): ExecutableTool {
  return {
    name: 'sub_emit',
    description: 'fake tool emitting nested loop events',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: 'sub_emit()',
      execute: async (ctx): Promise<ExecutableToolResult> => {
        for (const ev of childEvents) await ctx.onSubagentEvent?.(ev);
        return { output: '子代理结论:正常', note };
      },
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
  extra?: {
    restoreWorkspaceDirs?: string[];
    contextTokens?: number;
    workspaceRegistryPath?: string;
  },
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
    ...(extra?.workspaceRegistryPath !== undefined
      ? { workspaceRegistryPath: extra.workspaceRegistryPath }
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
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const res = await fetch(`${baseUrl}/health`);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: 'ok' });
  });

  it('POST /sessions:workspaceDir 必须是已存在目录;mode 默认 manual', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);

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
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const { status, json } = await postJson('/chat', { sessionId: 'ghost', input: 'hi' });
    expect(status).toBe(404);
    expect(json).toMatchObject({ error: { code: 'session_not_found' } });
  });

  it('POST /chat:工具轮 → 文本轮 → done 的 SSE 事件序列', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'echo', { a: 1 })],
      [text('最终回答')],
    ] });
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

  it('POST /chat:subagent_event 透传 SSE 且不落盘;tool 条目带 note 结论', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'sub_emit', { task: '分析视频' })],
      [text('总结')],
    ] });
    await startServer(provider, [
      subEmitTool(
        [{ type: 'text_delta', text: '子代理思考中' }],
        '{"reason":"completed","steps":1}',
      ),
    ]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '分析一下');
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));

    // SSE 原样透传嵌套事件,归属到父 toolCallId
    const nested = events.filter((e) => e.type === 'subagent_event');
    expect(nested).toHaveLength(1);
    expect(nested[0]).toMatchObject({
      type: 'subagent_event',
      toolCallId: 'c1',
      event: { type: 'text_delta', text: '子代理思考中' },
    });

    // 子事件流不落盘;结论留在 tool 条目的 output/note
    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.some((e) => JSON.stringify(e).includes('子代理思考中'))).toBe(false);
    const toolEntry = entries.find((e) => e.kind === 'tool');
    expect(toolEntry).toMatchObject({
      name: 'sub_emit',
      note: '{"reason":"completed","steps":1}',
    });
  });

  it('approval 往返:manual 模式收到 approval_request,POST /approval approved 后继续', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'write_file', { path: '/tmp/x' })],
      [text('写完了')],
    ] });
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
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const { status, json } = await postJson('/approval', {
      requestId: 'ghost',
      decision: 'approved',
    });
    expect(status).toBe(404);
    expect(json).toMatchObject({ error: { code: 'approval_not_found' } });
  });

  it('submit_detection 的 stopTurn:payload 全链路——detection 事件、tool 条目与 detection 条目落盘', async () => {
    const payload = {
      video_path: 'demo.mp4',
      binary_encoding: '0_0_0_0_0_0_0_0_0_0_0',
      normal: true,
      events: [],
      report_markdown: '# 报告',
    };
    const provider = new ScriptedProvider({ script: [[toolCall('c1', 'submit_detection', {})]] });
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
    // tool_result 事件原样携带结构化 payload(不经字符串编解码)
    expect(events[1]).toMatchObject({
      type: 'tool_result',
      result: { output: '检测结果已提交', payload },
    });
    expect(events[3]).toMatchObject({ type: 'detection', data: payload });
    expect(events[4]).toMatchObject({ reason: 'stop_turn' });

    // 落盘:tool 条目带 payload,detection 条目 data = 结构化载荷
    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'tool', 'detection']);
    const toolEntry = entries[1];
    if (toolEntry?.kind !== 'tool') throw new Error('expected tool entry');
    expect(toolEntry.name).toBe('submit_detection');
    expect(toolEntry.payload).toEqual(payload);
    const detectionEntry = entries[2];
    if (detectionEntry?.kind !== 'detection') throw new Error('expected detection entry');
    expect(detectionEntry.data).toEqual(payload);
  });

  it('同 session 的 /chat 互斥:进行中的轮次返回 409', async () => {
    // provider 第一轮发起需要审批的写操作并挂起,期间第二个 /chat 应 409。
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'write_file', {})],
      [text('ok')],
    ] });
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
    const provider = new ScriptedProvider({ script: [[text('第一轮回答')], [text('第二轮回答')]] });
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
    const provider = new ScriptedProvider({ script: [[text('ok')]] });
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
    const provider = new ScriptedProvider({ script: [[text('ok')]] });
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
    const provider = new ScriptedProvider({ script: [[text('看到了')]] });
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
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'write_file', { path: '/tmp/x' })],
      [text('写完了')],
    ] });
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
    const provider = new ScriptedProvider({ script: [[text('回答')]], usages: [{ inputOther: 500, inputCacheRead: 60, inputCacheCreation: 7, output: 40 }] });
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
    const provider = new ScriptedProvider({ script: [
        [toolCall('c1', 'echo', {})],
        [text('第一轮完')],
        [toolCall('c2', 'echo', {})],
        [text('第二轮完')],
      ], usages: [], summaries: [[text(summaryText)]] });
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
    const provider = new ScriptedProvider({ script: [[text('ok')]] });
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
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'write_file', {})],
      [text('ok')],
    ] });
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

  it('POST /sessions/{id}/mode:切换模式并持久化;非法 mode → 400;未知 session → 404', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const sessionId = await createSession();

    const ok = await postJson(`/sessions/${sessionId}/mode`, { mode: 'auto' });
    expect(ok.status).toBe(200);
    expect(ok.json).toEqual({ status: 'ok', mode: 'auto' });
    expect(agentServer?.sessions.get(sessionId)?.mode).toBe('auto');

    const bad = await postJson(`/sessions/${sessionId}/mode`, { mode: 'paranoid' });
    expect(bad.status).toBe(400);
    expect(bad.json).toMatchObject({ error: { code: 'invalid_request' } });
    expect(agentServer?.sessions.get(sessionId)?.mode).toBe('auto');

    const ghost = await postJson('/sessions/ghost/mode', { mode: 'yolo' });
    expect(ghost.status).toBe(404);
    expect(ghost.json).toMatchObject({ error: { code: 'session_not_found' } });
  });

  it('POST /sessions/{id}/mode:切换后下一轮裁决生效(manual → yolo 后写工具直接放行)', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'write_file', {})],
      [text('第一轮完')],
      [toolCall('c2', 'write_file', {})],
      [text('第二轮完')],
    ] });
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    // 第一轮:manual 模式,写工具挂起等审批
    const res1 = await startChat(sessionId, '第一轮');
    if (res1.body === null) throw new Error('no body');
    const next1 = sseReader(res1.body);
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next1();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }
    await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    await readUntilDone(next1);

    // 切换 yolo 后第二轮:写工具不再发起审批,直接执行
    const switched = await postJson(`/sessions/${sessionId}/mode`, { mode: 'yolo' });
    expect(switched.status).toBe(200);

    const res2 = await startChat(sessionId, '第二轮');
    if (res2.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res2.body));
    const types = events.map((e) => e.type);
    expect(types).not.toContain('approval_request');
    expect(types).toEqual(['tool_call_start', 'tool_result', 'step_done', 'text_delta', 'step_done', 'done']);
    expect(events[1]).toMatchObject({ toolCallId: 'c2', name: 'write_file', isError: false });
  });

  it('POST /sessions/{id}/mode:重启恢复后 mode 保持', async () => {
    const provider = new ScriptedProvider({ script: [] });
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();

    const ok = await postJson(`/sessions/${sessionId}/mode`, { mode: 'auto' });
    expect(ok.status).toBe(200);

    await agentServer?.close();
    await startServer(provider, [echoTool()], { restoreWorkspaceDirs: [workspace] });

    const list = await getJson('/sessions');
    const sessions = (list.json as { sessions: Record<string, unknown>[] }).sessions;
    expect(sessions[0]).toMatchObject({ id: sessionId, mode: 'auto' });
  });

  it('approval_request 携带 write_file 内容预览', async () => {
    const provider = new ScriptedProvider({
      script: [
        [toolCall('c1', 'write_file', { path: '/tmp/x.txt', content: 'hello preview' })],
        [text('写完了')],
      ],
    });
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const res = await startChat(sessionId, '写文件');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }
    expect(approval).toMatchObject({
      toolName: 'write_file',
      preview: { language: 'txt', content: 'hello preview', truncated: false },
    });

    // 回执后轮次完成,再校验落盘条目携带 preview
    await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    await readUntilDone(next);

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const approvalEntry = entries.find((e) => e.kind === 'approval');
    expect(approvalEntry).toMatchObject({
      kind: 'approval',
      decision: 'approved',
      preview: { language: 'txt', content: 'hello preview', truncated: false },
    });
  });

  it('approval_request 携带 run_script 脚本内容预览', async () => {
    const scriptPath = path.join(workspace, 'script.py');
    writeFileSync(scriptPath, 'print("hello script")', 'utf8');

    const provider = new ScriptedProvider({
      script: [[toolCall('c1', 'run_script', { path: 'script.py' })]],
    });
    await startServer(provider, [runScriptTool(workspace)]);
    const sessionId = await createSession('manual');

    const res = await startChat(sessionId, '跑脚本');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }
    expect(approval).toMatchObject({
      toolName: 'run_script',
      preview: { language: 'py', content: 'print("hello script")', truncated: false },
    });

    await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    await readUntilDone(next);
  });

  it('approval_request 对其它工具回退为参数 JSON 预览', async () => {
    const provider = new ScriptedProvider({
      script: [[toolCall('c1', 'custom_tool', { path: '/tmp/x', extra: 42 })]],
    });
    await startServer(provider, [otherTool()]);
    const sessionId = await createSession('manual');

    const res = await startChat(sessionId, '自定义工具');
    if (res.body === null) throw new Error('no body');
    const next = sseReader(res.body);
    let approval: SseEvent | null = null;
    while (approval === null) {
      const event = await next();
      if (event === null) throw new Error('stream ended before approval_request');
      if (event.type === 'approval_request') approval = event;
    }
    const preview = (approval as unknown as { preview?: { language: string; content: string; truncated: boolean } }).preview;
    expect(preview?.language).toBe('json');
    expect(preview?.truncated).toBe(false);
    expect(preview?.content).toContain('"path": "/tmp/x"');
    expect(preview?.content).toContain('"extra": 42');

    await postJson('/approval', {
      requestId: (approval as unknown as { requestId: string }).requestId,
      decision: 'approved',
    });
    await readUntilDone(next);
  });

  it('断连不取消挂起审批:events/history 可重新投递并回执', async () => {
    const provider = new ScriptedProvider({
      script: [
        [toolCall('c1', 'write_file', { path: '/tmp/x', content: 'disconnect test' })],
        [text('写完了')],
      ],
    });
    await startServer(provider, [writeTool()]);
    const sessionId = await createSession('manual');

    const abortCtrl = new AbortController();
    const res = await fetch(`${baseUrl}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, input: '写个文件' }),
      signal: abortCtrl.signal,
    });
    if (res.body === null) throw new Error('no body');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let approval: SseEvent | null = null;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep: number;
        while ((sep = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const line = raw.split('\n').find((l) => l.startsWith('data:'));
          if (line === undefined) continue;
          const ev = JSON.parse(line.slice(5).trim()) as SseEvent;
          if (ev.type === 'approval_request') {
            approval = ev;
            break;
          }
        }
        if (approval !== null) break;
      }
    } finally {
      abortCtrl.abort();
    }
    expect(approval).not.toBeNull();
    const requestId = (approval as unknown as { requestId: string }).requestId;

    // 等待服务端感知 close
    await new Promise((r) => setTimeout(r, 150));

    // events 续传应把未决审批标记为 pending 并携带 preview
    const eventsRes = await getJson(`/sessions/${sessionId}/events?fromSeq=0`);
    expect(eventsRes.status).toBe(200);
    const events = (eventsRes.json as { events: Array<{ seq: number; entry: TimelineEntry }>; inProgress: boolean }).events;
    expect(eventsRes.json).toMatchObject({ inProgress: true });
    const pendingEntry = events.find((e) => e.entry.kind === 'approval');
    expect(pendingEntry).toBeDefined();
    expect(pendingEntry?.entry).toMatchObject({
      kind: 'approval',
      pending: true,
      preview: { language: 'text', content: 'disconnect test', truncated: false },
    });

    // history 同样应标记 pending
    const historyRes = await getJson(`/sessions/${sessionId}/history`);
    const historyEntries = (historyRes.json as { entries: TimelineEntry[] }).entries;
    const historyApproval = historyEntries.find((e) => e.kind === 'approval');
    expect(historyApproval).toMatchObject({ pending: true });

    // 回执后轮次继续
    await postJson('/approval', { requestId, decision: 'approved' });

    // 轮询补齐直到结束
    for (let i = 0; i < 20; i += 1) {
      await new Promise((r) => setTimeout(r, 100));
      const poll = await getJson(`/sessions/${sessionId}/events?fromSeq=0`);
      const body = poll.json as { inProgress: boolean; events: Array<{ seq: number; entry: TimelineEntry }> };
      if (!body.inProgress) {
        expect(body.events.some((e) => e.entry.kind === 'tool')).toBe(true);
        break;
      }
      if (i === 19) throw new Error('turn did not finish after approval');
    }
  });

  it('defaultSystemPrompt:渲染事件契约占位符;chat_system.md 缺失即抛错(fail-fast)', () => {
    const promptsDir = mkdtempSync(path.join(tmpdir(), 'agent-prompts-test-'));
    try {
      // 缺失 → 启动即抛错(detect_system.md 回退已删除)
      expect(() => defaultSystemPrompt(promptsDir)).toThrow(/chat_system\.md/);

      writeFileSync(
        path.join(promptsDir, 'chat_system.md'),
        '头部\n当前共 {{ACTIVE_EVENT_COUNT}} 个活跃类别,编号 {{ACTIVE_EVENT_ID_LIST}}。\n{{EVENT_DEFINITIONS}}\n{{ADJUDICATION_RULES}}\n',
      );
      const rendered = defaultSystemPrompt(promptsDir);
      expect(rendered).not.toMatch(/\{\{[A-Z_]+\}\}/);
      expect(rendered).toContain('共 10 个活跃类别,编号 1-8、10-11');
      expect(rendered).toContain('违法停车');
      expect(rendered).toContain('应急车道静止触发双事件');
    } finally {
      rmSync(promptsDir, { recursive: true, force: true });
    }
  });

  it('defaultSystemPrompt:模板缺少任一占位符或含未知占位符均抛错', () => {
    const promptsDir = mkdtempSync(path.join(tmpdir(), 'agent-prompts-test-'));
    try {
      writeFileSync(
        path.join(promptsDir, 'chat_system.md'),
        '只有 {{ACTIVE_EVENT_COUNT}},缺其余占位符',
      );
      expect(() => defaultSystemPrompt(promptsDir)).toThrow(/占位符/);

      writeFileSync(
        path.join(promptsDir, 'chat_system.md'),
        '{{EVENT_DEFINITIONS}}\n{{ADJUDICATION_RULES}}\n{{ACTIVE_EVENT_COUNT}} {{ACTIVE_EVENT_ID_LIST}}\n{{UNKNOWN_TOKEN}}',
      );
      expect(() => defaultSystemPrompt(promptsDir)).toThrow(/未知占位符/);
    } finally {
      rmSync(promptsDir, { recursive: true, force: true });
    }
  });

  it('仓库 chat_system.md 渲染后覆盖全部活跃事件且无残留占位符', () => {
    const rendered = defaultSystemPrompt();
    const contract = loadEventContract();
    for (const event of contract.events) {
      expect(rendered).toContain(`**${event.name_zh}**`);
      expect(rendered).toContain(event.definition.split('\n')[0] as string);
      for (const condition of event.boundary_conditions) {
        expect(rendered).toContain(condition);
      }
    }
    for (const rule of contract.adjudication_rules) {
      expect(rendered).toContain(rule.name);
    }
    expect(rendered).not.toMatch(/\{\{[A-Z_]+\}\}/);
  });
});

// ---------------------------------------------------------------------------
// POST /workspaces/restore:运行后恢复磁盘历史会话(web 启动/切换工作区时
// 由代理层调用;覆盖 server 重启后内存索引为空、磁盘数据还在的场景)
// ---------------------------------------------------------------------------

describe('POST /workspaces/restore', () => {
  it('chat 后重启 → GET /sessions 为空 → restore 后历史可见(含 history)', async () => {
    const provider = new ScriptedProvider({ script: [[text('检测完成')]] });
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();

    const res = await startChat(sessionId, '检测这个视频');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    // 重启:新 server 不传 restoreWorkspaceDirs,内存索引为空。
    await agentServer?.close();
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);

    const before = await getJson('/sessions');
    expect((before.json as { sessions: unknown[] }).sessions).toEqual([]);

    const restored = await postJson('/workspaces/restore', { workspaceDir: workspace });
    expect(restored.status).toBe(200);
    expect(restored.json).toEqual({ status: 'ok', restored: 1 });

    const after = await getJson('/sessions');
    const sessions = (after.json as { sessions: Record<string, unknown>[] }).sessions;
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({
      id: sessionId,
      workspaceDir: workspace,
      title: '检测这个视频',
    });

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: { kind: string }[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'assistant']);
  });

  it('幂等:重复调用不报错不重复,第二次 restored:0', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const sessionId = await createSession();
    // 模拟「代理层重复调用」:同一会话已在内存,restore 不得覆盖/重复。
    const first = await postJson('/workspaces/restore', { workspaceDir: workspace });
    expect(first.json).toEqual({ status: 'ok', restored: 0 });
    const second = await postJson('/workspaces/restore', { workspaceDir: workspace });
    expect(second.json).toEqual({ status: 'ok', restored: 0 });

    const list = await getJson('/sessions');
    const sessions = (list.json as { sessions: { id: string }[] }).sessions;
    expect(sessions.map((s) => s.id)).toEqual([sessionId]);
  });

  it('workspaceDir 不是已存在目录 → 400 invalid_workspace;缺字段 → 400', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);

    const bad = await postJson('/workspaces/restore', {
      workspaceDir: path.join(workspace, 'nope'),
    });
    expect(bad.status).toBe(400);
    expect(bad.json).toMatchObject({ error: { code: 'invalid_workspace' } });

    const missing = await postJson('/workspaces/restore', {});
    expect(missing.status).toBe(400);
    expect(missing.json).toMatchObject({ error: { code: 'invalid_request' } });
  });

  it('sessions.db 不存在 → restored:0,且不在磁盘上创建 .agent 目录', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const empty = mkdtempSync(path.join(tmpdir(), 'agent-restore-empty-'));
    try {
      const res = await postJson('/workspaces/restore', { workspaceDir: empty });
      expect(res.status).toBe(200);
      expect(res.json).toEqual({ status: 'ok', restored: 0 });
      expect(existsSync(path.join(empty, '.agent'))).toBe(false);
    } finally {
      rmSync(empty, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 切换会话慢的根因回归:restore/history 不得物化整会话。messages 里可能有
// 几十 MB 的视频 dataURL(load_video),restore 与 GET /history 只需要
// entries;若它们解析 messages,启动/首次点击会被全量 JSON.parse 阻塞。
// 用损坏的 message_json 作探针:任何对 messages 的解析都会立刻炸掉。
// ---------------------------------------------------------------------------

describe('history/restore 只读 entries(不解析 messages)', () => {
  it('messages 行损坏时:启动恢复、列表、history 仍正常', async () => {
    const provider = new ScriptedProvider({ script: [[text('检测完成')]] });
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();

    const res = await startChat(sessionId, '检测这个视频');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));
    await agentServer?.close();

    // 直接把 messages 表改成非法 JSON(模拟超大/异常消息行)。
    const db = new DatabaseSync(path.join(workspace, '.agent', 'sessions.db'));
    db.prepare('UPDATE messages SET message_json = ?').run('{broken');
    db.close();

    // 重启并经启动路径恢复(构造器 workspaces):只开库,不物化。
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()], {
      restoreWorkspaceDirs: [workspace],
    });

    // 列表以磁盘行为准:不碰 messages。
    const list = await getJson('/sessions');
    expect(list.status).toBe(200);
    const sessions = (list.json as { sessions: { id: string }[] }).sessions;
    expect(sessions.map((s) => s.id)).toEqual([sessionId]);

    // history 只读 entries:不物化整会话,损坏的 messages 不影响。
    const history = await getJson(`/sessions/${sessionId}/history`);
    expect(history.status).toBe(200);
    const entries = (history.json as { entries: { kind: string }[] }).entries;
    expect(entries.map((e) => e.kind)).toEqual(['user', 'assistant']);

    // POST /workspaces/restore 运行时路径:storage 已开 → 幂等 0,不炸。
    const restored = await postJson('/workspaces/restore', { workspaceDir: workspace });
    expect(restored.json).toEqual({ status: 'ok', restored: 0 });
  });
});

// ---------------------------------------------------------------------------
// 工作区登记表自查恢复:agent server 自己保证 GET /sessions 前库已打开,
// 代理层 routes.py 不再做 restore-before-list 副作用。
// ---------------------------------------------------------------------------

describe('workspace registry self-restore', () => {
  it('GET /sessions 按登记表自动恢复尚未打开的工作区', async () => {
    const provider = new ScriptedProvider({ script: [[text('检测完成')]] });
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();
    const res = await startChat(sessionId, '检测这个视频');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));
    await agentServer?.close();

    // 登记表指向已落盘的 workspace,新 server 启动时不传 restoreWorkspaceDirs。
    const registryPath = path.join(tmpdir(), `agent-registry-${Date.now()}.json`);
    writeFileSync(registryPath, JSON.stringify([workspace]), 'utf8');

    try {
      await startServer(new ScriptedProvider({ script: [] }), [echoTool()], {
        workspaceRegistryPath: registryPath,
      });

      // GET /sessions 应触发自查恢复,无需代理层先调 /workspaces/restore。
      const list = await getJson('/sessions');
      expect(list.status).toBe(200);
      const sessions = (list.json as { sessions: { id: string }[] }).sessions;
      expect(sessions.map((s) => s.id)).toEqual([sessionId]);
    } finally {
      rmSync(registryPath, { force: true });
    }
  });

  it('登记表损坏/缺失时 GET /sessions 不炸、返回已有会话', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const sessionId = await createSession();

    const registryPath = path.join(tmpdir(), `agent-registry-bad-${Date.now()}.json`);
    writeFileSync(registryPath, '{not json', 'utf8');

    try {
      agentServer?.sessions.restoreFromRegistry();
      const list = await getJson('/sessions');
      expect(list.status).toBe(200);
      const sessions = (list.json as { sessions: { id: string }[] }).sessions;
      expect(sessions.map((s) => s.id)).toEqual([sessionId]);
    } finally {
      rmSync(registryPath, { force: true });
    }
  });

  it('已打开的工作区在登记表中只恢复一次(幂等)', async () => {
    const provider = new ScriptedProvider({ script: [[text('检测完成')]] });
    await startServer(provider, [echoTool()]);
    const sessionId = await createSession();
    const res = await startChat(sessionId, '检测这个视频');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const registryPath = path.join(tmpdir(), `agent-registry-dup-${Date.now()}.json`);
    writeFileSync(registryPath, JSON.stringify([workspace]), 'utf8');

    try {
      // workspace 已在启动时打开,restoreFromRegistry 应幂等。
      agentServer?.sessions.restoreFromRegistry();
      agentServer?.sessions.restoreFromRegistry();
      const list = await getJson('/sessions');
      expect(list.status).toBe(200);
      const sessions = (list.json as { sessions: { id: string }[] }).sessions;
      expect(sessions).toHaveLength(1);
    } finally {
      rmSync(registryPath, { force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 工具输出/检测载荷图片的媒体引用转换(SSE 与落盘条目不再内联 dataURL)
// ---------------------------------------------------------------------------

describe('媒体引用转换(media)', () => {
  const JPEG_BYTES = Buffer.from([0xff, 0xd8, 0xff, 0xdb, 0x01, 0x02]);
  const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x01]);
  const jpegDataUrl = `data:image/jpeg;base64,${JPEG_BYTES.toString('base64')}`;
  const pngDataUrl = `data:image/png;base64,${PNG_BYTES.toString('base64')}`;
  const jpegName = `${createHash('sha256').update(JPEG_BYTES).digest('hex')}.jpg`;

  /** 输出给定 ContentParts 的假工具(图片/视频 part 的载具)。 */
  function partsTool(output: ExecutableToolResult['output']): ExecutableTool {
    return {
      name: 'emit_parts',
      description: 'fake tool emitting image/video parts',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'emit_parts()',
        execute: () => Promise.resolve({ output }),
      }),
    };
  }

  it('image part dataURL → /sessions/{id}/media/{hash}.jpg 引用:SSE、history 一致,文件落盘,模型 messages 仍持原始 dataURL', async () => {
    const provider = new ScriptedProvider({ script: [
      [toolCall('c1', 'emit_parts', {})],
      [text('看完图了')],
    ] });
    await startServer(provider, [partsTool([
      { type: 'text', text: '帧图:' },
      { type: 'image_url', imageUrl: { url: jpegDataUrl } },
    ])]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '抽帧看看');
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));
    const toolResult = events.find((e) => e.type === 'tool_result');
    expect(toolResult).toBeDefined();
    const output = (toolResult!.result as { output: unknown }).output;
    expect(Array.isArray(output)).toBe(true);
    const imagePart = (output as Array<{ type: string; imageUrl?: { url: string } }>).find(
      (p) => p.type === 'image_url',
    );
    expect(imagePart?.imageUrl?.url).toBe(`/sessions/${sessionId}/media/${jpegName}`);
    expect((output as Array<{ text?: string }>).some((p) => p.text === '帧图:')).toBe(true);

    // 落盘条目与 SSE 一致;media 文件按内容寻址写入 workspace。
    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const toolEntry = entries.find((e) => e.kind === 'tool');
    if (toolEntry?.kind !== 'tool') throw new Error('expected tool entry');
    const entryImage = (toolEntry.output as Array<{ type: string; imageUrl?: { url: string } }>).find(
      (p) => p.type === 'image_url',
    );
    expect(entryImage?.imageUrl?.url).toBe(`/sessions/${sessionId}/media/${jpegName}`);
    const mediaFile = path.join(workspace, '.agent', 'media', jpegName);
    expect(existsSync(mediaFile)).toBe(true);

    // 模型侧 messages 不受影响:tool 消息里仍是原始 dataURL(第二次 generate
    // 的历史才含 tool 结果消息,见 ScriptedProvider.histories 按 generate 记录)。
    const toolMessage = provider.histories.flatMap((h) => h).find((m) => m.role === 'tool');
    expect(toolMessage).toBeDefined();
    const modelImage = toolMessage!.content.find((p) => p.type === 'image_url');
    expect(modelImage).toEqual({ type: 'image_url', imageUrl: { url: jpegDataUrl } });
  });

  it('同字节图片 → 同 hash 同 URL(重复落盘幂等);不同字节各自寻址', async () => {
    const provider = new ScriptedProvider({ script: [[toolCall('c1', 'emit_parts', {})]] });
    await startServer(provider, [partsTool([
      { type: 'image_url', imageUrl: { url: jpegDataUrl } },
      { type: 'image_url', imageUrl: { url: jpegDataUrl } },
      { type: 'image_url', imageUrl: { url: pngDataUrl } },
    ])]);
    const sessionId = await createSession('yolo');
    const res = await startChat(sessionId, '三张图');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const toolEntry = entries.find((e) => e.kind === 'tool');
    if (toolEntry?.kind !== 'tool') throw new Error('expected tool entry');
    const urls = (toolEntry.output as Array<{ type: string; imageUrl?: { url: string } }>)
      .filter((p) => p.type === 'image_url')
      .map((p) => p.imageUrl?.url);
    const pngName = `${createHash('sha256').update(PNG_BYTES).digest('hex')}.png`;
    expect(urls).toEqual([
      `/sessions/${sessionId}/media/${jpegName}`,
      `/sessions/${sessionId}/media/${jpegName}`,
      `/sessions/${sessionId}/media/${pngName}`,
    ]);
  });

  it('video part 占位文本不变;非 dataURL 的 image part 原样保留(旧 ref 兼容)', async () => {
    const provider = new ScriptedProvider({ script: [[toolCall('c1', 'emit_parts', {})]] });
    await startServer(provider, [partsTool([
      { type: 'video_url', videoUrl: { url: 'data:video/mp4;base64,AAAA' } },
      { type: 'image_url', imageUrl: { url: '/sessions/other/media/alreadyref.jpg' } },
    ])]);
    const sessionId = await createSession('yolo');
    const res = await startChat(sessionId, '看视频');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const toolEntry = entries.find((e) => e.kind === 'tool');
    if (toolEntry?.kind !== 'tool') throw new Error('expected tool entry');
    const parts = toolEntry.output as Array<{ type: string; text?: string; imageUrl?: { url: string } }>;
    expect(parts).toEqual([
      { type: 'text', text: '[完整视频已发送给模型,不在此展示]' },
      { type: 'image_url', imageUrl: { url: '/sessions/other/media/alreadyref.jpg' } },
    ]);
  });

  it('GET /sessions/{id}/media/{name}:200 原字节 + Content-Type + 长缓存;未知 session/文件缺失 → 404', async () => {
    const provider = new ScriptedProvider({ script: [[toolCall('c1', 'emit_parts', {})]] });
    await startServer(provider, [partsTool([
      { type: 'image_url', imageUrl: { url: jpegDataUrl } },
    ])]);
    const sessionId = await createSession('yolo');
    const res = await startChat(sessionId, '抽帧');
    if (res.body === null) throw new Error('no body');
    await readUntilDone(sseReader(res.body));

    const ok = await fetch(`${baseUrl}/sessions/${sessionId}/media/${jpegName}`);
    expect(ok.status).toBe(200);
    expect(ok.headers.get('content-type')).toBe('image/jpeg');
    expect(ok.headers.get('cache-control')).toContain('immutable');
    expect(Buffer.from(await ok.arrayBuffer()).equals(JPEG_BYTES)).toBe(true);

    const missingFile = await fetch(
      `${baseUrl}/sessions/${sessionId}/media/${'0'.repeat(64)}.jpg`,
    );
    expect(missingFile.status).toBe(404);

    const unknownSession = await fetch(`${baseUrl}/sessions/ghost/media/${jpegName}`);
    expect(unknownSession.status).toBe(404);

    // 非白名单文件名路由不匹配 → 404(防路径穿越的第一道)。
    const badName = await fetch(`${baseUrl}/sessions/${sessionId}/media/..%2Fsecret.jpg`);
    expect(badName.status).toBe(404);
  });

  it('detection 载荷逐事件 annotated_image → 引用:tool 条目 payload、detection 事件与条目一致,文件落盘', async () => {
    const payload = {
      video_path: 'demo.mp4',
      binary_encoding: '0_0_0_0_0_0_0_1_0_0_0',
      normal: false,
      events: [{
        event_id: 7,
        detected: true,
        confidence: 0.9,
        instances: [],
        reasoning: 'r',
        evidence_frames: [1.0],
        annotated_image: jpegDataUrl,
      }],
      report_markdown: '# 报告',
    };
    const provider = new ScriptedProvider({ script: [[toolCall('c1', 'submit_detection', {})]] });
    await startServer(provider, [submitTool(payload)]);
    const sessionId = await createSession('yolo');

    const res = await startChat(sessionId, '提交检测');
    if (res.body === null) throw new Error('no body');
    const events = await readUntilDone(sseReader(res.body));
    const detection = events.find((e) => e.type === 'detection');
    expect(detection).toBeDefined();

    const expectedUrl = `/sessions/${sessionId}/media/${jpegName}`;
    const detectionEvent = (detection!.data as { events: Array<{ annotated_image?: string }> }).events[0];
    expect(detectionEvent?.annotated_image).toBe(expectedUrl);
    const toolResult = events.find((e) => e.type === 'tool_result');
    const toolResultPayload = (toolResult!.result as { payload: { events: Array<{ annotated_image?: string }> } }).payload;
    expect(toolResultPayload.events[0]?.annotated_image).toBe(expectedUrl);

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const toolEntry = entries.find((e) => e.kind === 'tool');
    const detectionEntry = entries.find((e) => e.kind === 'detection');
    if (toolEntry?.kind !== 'tool' || detectionEntry?.kind !== 'detection') {
      throw new Error('expected tool + detection entries');
    }
    expect((toolEntry.payload as typeof payload).events[0]?.annotated_image).toBe(expectedUrl);
    expect((detectionEntry.data as typeof payload).events[0]?.annotated_image).toBe(expectedUrl);
    expect(existsSync(path.join(workspace, '.agent', 'media', jpegName))).toBe(true);
  });

  it('旧 dataURL 条目直读不受影响:history/events 原样返回,不做二次转换', async () => {
    await startServer(new ScriptedProvider({ script: [] }), [echoTool()]);
    const sessionId = await createSession();
    const legacyEntry: TimelineEntry = {
      kind: 'tool',
      toolCallId: 'legacy-1',
      name: 'extract_frames',
      arguments: '{}',
      output: [{ type: 'image_url', imageUrl: { url: jpegDataUrl } }],
      isError: false,
      at: Date.now(),
    };
    agentServer?.sessions.appendEntries(sessionId, [legacyEntry]);

    const history = await getJson(`/sessions/${sessionId}/history`);
    const entries = (history.json as { entries: TimelineEntry[] }).entries;
    const toolEntry = entries.find((e) => e.kind === 'tool');
    if (toolEntry?.kind !== 'tool') throw new Error('expected tool entry');
    expect(toolEntry.output).toEqual([{ type: 'image_url', imageUrl: { url: jpegDataUrl } }]);

    const events = await getJson(`/sessions/${sessionId}/events?fromSeq=0`);
    const batch = (events.json as { events: Array<{ entry: TimelineEntry }> }).events;
    const legacyFromEvents = batch.map((e) => e.entry).find((e) => e.kind === 'tool');
    expect(legacyFromEvents).toEqual(legacyEntry);
  });
});
