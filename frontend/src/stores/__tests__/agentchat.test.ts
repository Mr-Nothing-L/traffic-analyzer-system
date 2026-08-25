// 工作区切换重置对话会话回归测试:
// agent 会话由后端注入 workspaceDir,切换工作区后旧会话仍绑旧工作区,
// 新工作区相对路径送入旧会话会解析失败。期望:工作区路径变化清空当前会话态
// (sessionId/entries/pendingVideo,不删后端历史会话列表),首次加载不触发。
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { isReactive, nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';

// workspace store 初始化即读 localStorage,node 环境下先垫一层内存实现
beforeEach(() => {
  const store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
  setActivePinia(createPinia());
});

async function load() {
  const { useWorkspaceStore } = await import('../workspace');
  const { useAgentChatStore } = await import('../agentchat');
  return { ws: useWorkspaceStore(), agent: useAgentChatStore() };
}

describe('工作区切换重置 agent 会话', () => {
  it('首次加载(null → 有值)不清空会话态', async () => {
    const { ws, agent } = await load();
    agent.sessionId = 's1';
    agent.entries = [{ kind: 'user', text: 'hi' }];
    agent.setPendingVideo({ path: 'a.mp4', name: 'a.mp4' });
    ws.path = '/ws/a';
    await nextTick();
    expect(agent.sessionId).toBe('s1');
    expect(agent.entries).toHaveLength(1);
    expect(agent.pendingVideo).not.toBeNull();
  });

  it('工作区路径变化:清 sessionId/entries/pendingVideo,保留历史会话列表', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a'; // 先落到初始工作区
    await nextTick();
    agent.sessionId = 's1';
    agent.entries = [{ kind: 'user', text: 'hi' }];
    agent.setPendingVideo({ path: 'a.mp4', name: 'a.mp4' });
    agent.sessions = [{ id: 's1', workspaceDir: '/ws/a' }];
    agent.status = 'done';
    ws.path = '/ws/b'; // 切换工作区
    await nextTick();
    expect(agent.sessionId).toBeNull();
    expect(agent.entries).toEqual([]);
    expect(agent.pendingVideo).toBeNull();
    expect(agent.status).toBe('idle');
    expect(agent.sessions).toEqual([{ id: 's1', workspaceDir: '/ws/a' }]); // 后端历史不删
  });

  it('工作区切换后重拉会话列表,恢复的会话立即可见', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessions = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({ sessions: [{ id: 's9', workspaceDir: '/ws/b', title: '恢复的会话' }] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    ws.path = '/ws/b'; // 切换工作区 → watch 应触发 fetchSessions
    await nextTick();
    // fetchSessions 是异步的,等它落地
    await vi.waitFor(() => {
      expect(agent.sessions).toEqual([{ id: 's9', workspaceDir: '/ws/b', title: '恢复的会话' }]);
    });
    vi.unstubAllGlobals();
  });

  it('selectSession 不动工作区路径,不触发重置', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessionId = 's1';
    agent.entries = [{ kind: 'user', text: 'hi' }];
    // selectSession 会拉 history,这里不真调;直接模拟其结果(换 sessionId/entries)
    agent.sessionId = 's2';
    agent.entries = [{ kind: 'user', text: 'from history' }];
    await nextTick();
    expect(agent.sessionId).toBe('s2');
    expect(agent.entries).toHaveLength(1);
  });
});

// selectSession 竞态回归:history 请求在途期间,工作区切换/新建/后一次选择
// 都会清空或取代本地状态;晚到的 history 响应必须丢弃,否则会把已清空的
// 旧会话写回时间线(用户看到「历史会话内容又冒出来/串台」)。同时验证历史
// 条目 markRaw(大 base64 图片字符串不做深响应式 proxy 化)。
function historyResponse(entries: unknown[]): Response {
  return new Response(JSON.stringify({ entries }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('selectSession 代际守卫', () => {
  it('history 在途期间工作区切换:晚到的响应不得写回旧会话', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();

    let release: (r: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn((input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        }
        // 工作区 watch 触发的 fetchSessions 等
        return Promise.resolve(
          new Response(JSON.stringify({ sessions: [] }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }),
    );

    const pending = agent.selectSession('s-old');
    await nextTick();
    expect(agent.status).toBe('connecting');

    ws.path = '/ws/b'; // 工作区切换:清空会话态,使在途 select 失效
    await nextTick();

    release(historyResponse([{ kind: 'user', text: '旧会话内容', images: [], at: 1 }]));
    await pending;

    expect(agent.entries).toEqual([]); // 晚到的旧会话历史不得写回
    expect(agent.sessionId).toBeNull();
    expect(agent.status).toBe('idle');
    vi.unstubAllGlobals();
  });

  it('后一次选择取代前一次:前一次晚到的 history 被丢弃', async () => {
    const { agent } = await load();
    const releases = new Map<string, (r: Response) => void>();
    vi.stubGlobal(
      'fetch',
      vi.fn((input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return new Promise<Response>((resolve) => releases.set(url, resolve));
        }
        return Promise.resolve(
          new Response(JSON.stringify({ sessions: [] }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }),
    );

    const p1 = agent.selectSession('s1');
    const p2 = agent.selectSession('s2');
    // s2 先落地:生效
    releases.get('/api/agent/sessions/s2/history')!(
      historyResponse([{ kind: 'user', text: 'B', images: [], at: 2 }]),
    );
    await p2;
    expect(agent.sessionId).toBe('s2');
    // s1 晚到:被丢弃,不得覆盖 s2
    releases.get('/api/agent/sessions/s1/history')!(
      historyResponse([{ kind: 'user', text: 'A', images: [], at: 1 }]),
    );
    await p1;
    expect(agent.sessionId).toBe('s2');
    expect(agent.entries).toHaveLength(1);
    expect(agent.entries[0]).toMatchObject({ kind: 'user', text: 'B' });
    vi.unstubAllGlobals();
  });

  it('历史条目 markRaw:不进深响应式(大 base64 免 proxy 化),字段仍可读', async () => {
    const { agent } = await load();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return historyResponse([
            { kind: 'user', text: 'hi', images: ['data:image/png;base64,AAA'], at: 1 },
          ]);
        }
        return new Response(JSON.stringify({ sessions: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }),
    );
    await agent.selectSession('s1');
    vi.unstubAllGlobals();

    expect(agent.entries).toHaveLength(1);
    const entry = agent.entries[0];
    expect(isReactive(entry)).toBe(false); // 历史条目不做深响应式代理
    expect(entry).toMatchObject({ kind: 'user', text: 'hi' });
  });
});

// 工具结果图片回归测试:extract_frames/draw_boxes 的 output 是 kosong ContentPart[]
// (text + image_url),历史落盘 JSON 往返后仍是数组;期望 mapHistoryEntry 提取
// 图片 dataURL 进 images,文本仍进 result。
describe('历史工具条目图片提取', () => {
  it('history 的 tool 条目 output(ContentPart[])拆成文本 result + 图片 images', async () => {
    const { agent } = await load();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).endsWith('/history')) {
          return new Response(
            JSON.stringify({
              entries: [
                {
                  kind: 'tool',
                  toolCallId: 'c1',
                  name: 'extract_frames',
                  arguments: '{}',
                  output: [
                    { type: 'text', text: '帧 t=2.0s' },
                    { type: 'image_url', imageUrl: { url: 'data:image/jpeg;base64,AAA' } },
                    { type: 'image_url', image_url: { url: 'data:image/jpeg;base64,BBB' } },
                  ],
                  isError: false,
                  at: 1,
                },
              ],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          );
        }
        return new Response('{}', { status: 200 });
      }),
    );
    await agent.selectSession('s1');
    const tool = agent.entries[0];
    expect(tool.kind).toBe('tool');
    if (tool.kind === 'tool') {
      expect(tool.result).toBe('帧 t=2.0s');
      expect(tool.images).toEqual(['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB']);
      expect(tool.done).toBe(true);
    }
    vi.unstubAllGlobals();
  });

  it('history 的 load_video 条目:video part 只置 hasVideo,不进 images(result 仍为文本)', async () => {
    const { agent } = await load();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).endsWith('/history')) {
          return new Response(
            JSON.stringify({
              entries: [
                {
                  kind: 'tool',
                  toolCallId: 'c9',
                  name: 'load_video',
                  arguments: '{"video_path":"a.mp4"}',
                  output: [
                    { type: 'text', text: '已加载完整视频:时长 10s,fps 2,大小 30.0MB,已降帧/转码' },
                    { type: 'video_url', videoUrl: { url: 'data:video/mp4;base64,QUJD' } },
                  ],
                  isError: false,
                  at: 1,
                },
              ],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          );
        }
        return new Response('{}', { status: 200 });
      }),
    );
    await agent.selectSession('s1');
    const tool = agent.entries[0];
    expect(tool.kind).toBe('tool');
    if (tool.kind === 'tool') {
      expect(tool.result).toContain('已加载完整视频');
      expect(tool.images).toEqual([]); // 40MB 视频 dataURL 绝不当图渲染
      expect(tool.hasVideo).toBe(true);
      expect(tool.children).toEqual([]);
    }
    vi.unstubAllGlobals();
  });
});

// SSE 流式事件测试:fetch 垫一个按 \n\n 分块的 data: 行流,驱动 send 全流程。
function sseResponse(events: unknown[]): Response {
  const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function stubChatStream(events: unknown[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/chat')) return sseResponse(events);
      // fetchSessions 等其余请求
      return new Response(JSON.stringify({ sessions: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
}

describe('SSE 流式事件', () => {
  it('subagent_event 聚合到对应 spawn_subagent 工具条目的 children,不建顶层条目', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      {
        type: 'tool_call_start',
        call: { id: 'c1', name: 'spawn_subagent', arguments: '{"task":"研判整段视频"}' },
      },
      { type: 'subagent_event', toolCallId: 'c1', event: { type: 'think_delta', text: '先想' } },
      { type: 'subagent_event', toolCallId: 'c1', event: { type: 'think_delta', text: '再想' } },
      {
        type: 'subagent_event',
        toolCallId: 'c1',
        event: {
          type: 'tool_call_start',
          call: { id: 't1', name: 'extract_frames', arguments: '{"timestamps":[2]}' },
        },
      },
      {
        type: 'subagent_event',
        toolCallId: 'c1',
        event: { type: 'tool_result', toolCallId: 't1', result: { output: 'ok' }, isError: false },
      },
      { type: 'subagent_event', toolCallId: 'c1', event: { type: 'text_delta', text: '子结论' } },
      {
        type: 'tool_result',
        toolCallId: 'c1',
        name: 'spawn_subagent',
        result: { output: '子代理结论:无事件', note: '{"reason":"completed","steps":3}' },
        isError: false,
      },
      { type: 'done', reason: 'stop_turn' },
    ]);
    await agent.send('研判一下');
    vi.unstubAllGlobals();

    // 顶层只有 user + 一个工具条目,子事件不建独立条目
    expect(agent.entries.map((e) => e.kind)).toEqual(['user', 'tool']);
    const tool = agent.entries[1];
    if (tool.kind !== 'tool') throw new Error('expected tool entry');
    expect(tool.result).toBe('子代理结论:无事件');
    expect(tool.done).toBe(true);
    expect(tool.children).toEqual([
      { kind: 'think', text: '先想再想' }, // 连续 think_delta 聚合
      {
        kind: 'tool',
        id: 't1',
        name: 'extract_frames',
        args: '{"timestamps":[2]}',
        done: true, // tool_result 回填
      },
      { kind: 'text', text: '子结论' },
    ]);
    expect(agent.status).toBe('done');
  });

  it('load_video 工具结果:video part 只置 hasVideo,images 为空,result 取文本部分', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      {
        type: 'tool_call_start',
        call: { id: 'c2', name: 'load_video', arguments: '{"video_path":"a.mp4"}' },
      },
      {
        type: 'tool_result',
        toolCallId: 'c2',
        name: 'load_video',
        result: {
          output: [
            { type: 'text', text: '已加载完整视频:时长 10s。视频内容如下:' },
            { type: 'video_url', videoUrl: { url: 'data:video/mp4;base64,QUJD' } },
          ],
        },
        isError: false,
      },
      { type: 'done', reason: 'stop_turn' },
    ]);
    await agent.send('看看视频');
    vi.unstubAllGlobals();

    const tool = agent.entries[1];
    if (tool.kind !== 'tool') throw new Error('expected tool entry');
    expect(tool.result).toBe('已加载完整视频:时长 10s。视频内容如下:');
    expect(tool.images).toEqual([]);
    expect(tool.hasVideo).toBe(true);
  });

  it('detection 事件 data 原样入条目(含逐事件 annotated_image 与 meta 降级字段)', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    const payload = {
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      events: [
        {
          event_id: 3,
          detected: true,
          reasoning: '追尾',
          evidence_frames: ['f1'],
          annotated_image: 'data:image/jpeg;base64,WFg=',
        },
        { event_id: 5, detected: true, reasoning: '逆行', evidence_frames: ['f2'] },
      ],
      meta: { annotation_not_provided: [5] },
      report_markdown: '## 报告',
    };
    stubChatStream([{ type: 'detection', data: payload }, { type: 'done', reason: 'stop_turn' }]);
    await agent.send('检测');
    vi.unstubAllGlobals();

    const det = agent.entries[1];
    expect(det.kind).toBe('detection');
    if (det.kind === 'detection') expect(det.data).toEqual(payload);
  });

  // 截断警示:done {truncated:true} 时时间线末尾插入 warn 级 system 条目;
  // 不带 truncated(或 reason=error)则不插。
  it('done 带 truncated:true 时,末尾插入警示条目;普通 done 不插', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      { type: 'text_delta', text: '被截断的回答' },
      { type: 'done', reason: 'stop_turn', truncated: true },
    ]);
    await agent.send('问一题');
    vi.unstubAllGlobals();

    expect(agent.status).toBe('done');
    const last = agent.entries[agent.entries.length - 1];
    expect(last).toEqual({
      kind: 'system',
      text: '输出达到 token 上限被截断,部分内容可能不完整,可继续追问',
      tone: 'warn',
    });
  });

  it('done 不带 truncated 时不插警示条目', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      { type: 'text_delta', text: '完整回答' },
      { type: 'done', reason: 'stop_turn' },
    ]);
    await agent.send('问一题');
    vi.unstubAllGlobals();

    expect(agent.status).toBe('done');
    expect(agent.entries.some((e) => e.kind === 'system')).toBe(false);
  });

  it('done reason=error 即使带 truncated 也走失败态,不插警示条目', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([{ type: 'done', reason: 'error', error: 'boom', truncated: true }]);
    await agent.send('问一题');
    vi.unstubAllGlobals();

    expect(agent.status).toBe('failed');
    expect(agent.error).toBe('boom');
    expect(agent.entries.some((e) => e.kind === 'system')).toBe(false);
  });
});

// 断连恢复 + steer + cancel 测试(P1/P2 前端):
// - selectSession 后 GET events 显示 inProgress=true → 进入恢复态(running + recovering),
//   5s 轮询补齐落盘条目,inProgress=false 后收尾并停轮询;
// - 进行中发送走 /steer(乐观插入 steered user 条目),409 no_active_turn 回退 /chat;
// - SSE steer 事件/轮询补齐的 user 条目与本地乐观条目按 text 去重;
// - 停止按钮走 /cancel 显式终止服务端轮次。
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('断连恢复(events 轮询)', () => {
  it('selectSession 后 inProgress=true:进入恢复态,轮询补齐并在结束后收尾停轮询', async () => {
    vi.useFakeTimers();
    const { agent } = await load();
    let eventCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: '首轮', images: [], at: 1 }]);
        }
        if (url.includes('/events')) {
          eventCalls += 1;
          if (eventCalls === 1) return jsonResponse({ events: [], inProgress: true });
          return jsonResponse({
            events: [{ seq: 2, entry: { kind: 'assistant', text: '恢复的回答', think: '', at: 2 } }],
            inProgress: false,
          });
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    await agent.selectSession('s1');
    expect(agent.status).toBe('running'); // 恢复态按运行中呈现
    expect(agent.recovering).toBe(true);
    expect(agent.entries).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(5000); // 第一次轮询:补齐 + 收尾
    expect(agent.entries).toHaveLength(2);
    expect(agent.entries[1]).toMatchObject({ kind: 'assistant', text: '恢复的回答' });
    expect(agent.recovering).toBe(false);
    expect(agent.status).toBe('done');

    const callsAfterDone = eventCalls;
    await vi.advanceTimersByTimeAsync(20000);
    expect(eventCalls).toBe(callsAfterDone); // inProgress=false 后轮询已停
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('工作区切换:恢复轮询停止,恢复态清除', async () => {
    vi.useFakeTimers();
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    let eventCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: '首轮', images: [], at: 1 }]);
        }
        if (url.includes('/events')) {
          eventCalls += 1;
          return jsonResponse({ events: [], inProgress: true }); // 一直在跑
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    await agent.selectSession('s1');
    expect(agent.recovering).toBe(true);

    ws.path = '/ws/b'; // 工作区切换:重置会话态,轮询必须停
    await nextTick();
    expect(agent.recovering).toBe(false);
    expect(agent.status).toBe('idle');

    const calls = eventCalls;
    await vi.advanceTimersByTimeAsync(20000);
    expect(eventCalls).toBe(calls);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('恢复轮询补齐的 steer user 条目与本地乐观条目去重', async () => {
    vi.useFakeTimers();
    const { agent } = await load();
    let eventCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: '首轮', images: [], at: 1 }]);
        }
        if (url.endsWith('/steer')) return jsonResponse({ status: 'ok', queued: true });
        if (url.includes('/events')) {
          eventCalls += 1;
          if (eventCalls === 1) return jsonResponse({ events: [], inProgress: true });
          // 服务端落盘的 steer user 条目(seq 2)与本地乐观条目同文,应去重
          return jsonResponse({
            events: [
              { seq: 2, entry: { kind: 'user', text: '插话', images: [], at: 2 } },
              { seq: 3, entry: { kind: 'assistant', text: '收尾', think: '', at: 3 } },
            ],
            inProgress: false,
          });
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    await agent.selectSession('s1'); // 进入恢复态(running)
    await agent.send('插话'); // 进行中 → /steer,乐观插入
    expect(agent.entries).toHaveLength(2);
    expect(agent.entries[1]).toMatchObject({ kind: 'user', text: '插话', steered: true });

    await vi.advanceTimersByTimeAsync(5000);
    expect(agent.entries).toHaveLength(3); // steer user 条目不重复,只补 assistant
    expect(agent.entries[1]).toMatchObject({ kind: 'user', text: '插话', steered: true });
    expect(agent.entries[2]).toMatchObject({ kind: 'assistant', text: '收尾' });
    expect(agent.recovering).toBe(false);
    expect(agent.status).toBe('done');
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});

describe('steer 插话', () => {
  it('进行中发送走 /steer:乐观插入带 steered 标记的 user 条目,不发 /chat', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    agent.status = 'running';
    const calls: Array<{ url: string; body?: unknown }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { body?: unknown }) => {
        calls.push({ url: String(input), body: init?.body });
        return jsonResponse({ status: 'ok', queued: true });
      }),
    );

    await agent.send('插话一句');
    vi.unstubAllGlobals();

    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe('/api/agent/sessions/s1/steer');
    expect(JSON.parse(String(calls[0]!.body))).toMatchObject({ input: '插话一句' });
    expect(agent.entries).toHaveLength(1);
    expect(agent.entries[0]).toMatchObject({ kind: 'user', text: '插话一句', steered: true });
    expect(agent.status).toBe('running'); // 轮次仍进行中,状态不变
  });

  it('steer 409 no_active_turn:回退正常 /chat 发送(不带 steered 标记)', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    agent.status = 'running';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/steer')) {
          return jsonResponse(
            { error: { code: 'no_active_turn', message: 'no chat turn in progress' } },
            409,
          );
        }
        if (url.endsWith('/chat')) {
          return sseResponse([
            { type: 'text_delta', text: '回答' },
            { type: 'done', reason: 'stop_turn' },
          ]);
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    await agent.send('新问题');
    vi.unstubAllGlobals();

    expect(agent.entries.map((e) => e.kind)).toEqual(['user', 'assistant']);
    expect(agent.entries[0]).toMatchObject({ kind: 'user', text: '新问题' });
    expect('steered' in agent.entries[0]!).toBe(false); // 正常发送不带插话标记
    expect(agent.status).toBe('done');
  });

  it('SSE steer 事件(本地无乐观条目,他端插入):补一条带 steered 标记的 user 条目', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      { type: 'text_delta', text: '先答一半' },
      { type: 'steer', text: '他端插话', images: [] },
      { type: 'done', reason: 'stop_turn' },
    ]);
    await agent.send('首轮');
    vi.unstubAllGlobals();

    expect(agent.entries.map((e) => e.kind)).toEqual(['user', 'assistant', 'user']);
    expect(agent.entries[2]).toMatchObject({ kind: 'user', text: '他端插话', steered: true });
  });
});

describe('cancel 显式终止', () => {
  it('恢复态 cancelTurn:POST /cancel 后拉齐 events 收尾(停轮询)', async () => {
    vi.useFakeTimers();
    const { agent } = await load();
    let cancelCalled = false;
    let eventCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: '首轮', images: [], at: 1 }]);
        }
        if (url.endsWith('/cancel')) {
          cancelCalled = true;
          return jsonResponse({ status: 'ok' });
        }
        if (url.includes('/events')) {
          eventCalls += 1;
          if (eventCalls === 1) return jsonResponse({ events: [], inProgress: true });
          return jsonResponse({
            events: [
              { seq: 2, entry: { kind: 'assistant', text: '被取消前的部分', think: '', at: 2 } },
            ],
            inProgress: false,
          });
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    await agent.selectSession('s1');
    expect(agent.recovering).toBe(true);

    await agent.cancelTurn();
    expect(cancelCalled).toBe(true);
    expect(agent.entries).toHaveLength(2); // 拉齐取消前已落盘的部分
    expect(agent.recovering).toBe(false);
    expect(agent.status).toBe('done');

    const calls = eventCalls;
    await vi.advanceTimersByTimeAsync(20000);
    expect(eventCalls).toBe(calls); // 收尾后轮询已停
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});
