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
                    { type: 'image_url', imageUrl: { url: 'data:image/jpeg;base64,BBB' } },
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

// 媒体引用(media ref)渲染支持:服务端把工具输出/检测载荷里的图片 dataURL
// 落盘为内容寻址文件后,条目只携带 /sessions/{id}/media/{hash} 引用 URL;
// 前端在 store 层统一加 /api/agent 代理前缀,旧 dataURL 原样混排。
describe('媒体引用 URL 解析', () => {
  const REF = '/sessions/s1/media/'.concat('a'.repeat(64), '.jpg');
  const REF_URL = `/api/agent${REF}`;

  it('history 的 tool 条目:image_url 引用加 /api/agent 前缀,与 dataURL 混排正常', async () => {
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
                    { type: 'text', text: '帧图:' },
                    { type: 'image_url', imageUrl: { url: REF } },
                    { type: 'image_url', imageUrl: { url: 'data:image/jpeg;base64,AAA' } },
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
    vi.unstubAllGlobals();
    const tool = agent.entries[0];
    if (tool.kind !== 'tool') throw new Error('expected tool entry');
    expect(tool.images).toEqual([REF_URL, 'data:image/jpeg;base64,AAA']);
  });

  it('SSE tool_result 的 image_url 引用同样加前缀', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      { type: 'tool_call_start', call: { id: 'c1', name: 'draw_boxes', arguments: '{}' } },
      {
        type: 'tool_result',
        toolCallId: 'c1',
        name: 'draw_boxes',
        result: {
          output: [
            { type: 'text', text: '标注完成:' },
            { type: 'image_url', imageUrl: { url: REF } },
          ],
        },
        isError: false,
      },
      { type: 'done', reason: 'stop_turn' },
    ]);
    await agent.send('画框');
    vi.unstubAllGlobals();

    const tool = agent.entries[1];
    if (tool.kind !== 'tool') throw new Error('expected tool entry');
    expect(tool.images).toEqual([REF_URL]);
  });

  it('detection 事件/历史条目的 annotated_image 引用加前缀;dataURL 原样', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    const refPayload = {
      events: [{ event_id: 3, detected: true, reasoning: 'r', evidence_frames: [1], annotated_image: REF }],
    };
    stubChatStream([
      { type: 'detection', data: refPayload },
      { type: 'done', reason: 'stop_turn' },
    ]);
    await agent.send('检测');
    vi.unstubAllGlobals();

    const det = agent.entries[1];
    if (det.kind !== 'detection') throw new Error('expected detection entry');
    const events = (det.data as { events: Array<{ annotated_image?: string }> }).events;
    expect(events[0]?.annotated_image).toBe(REF_URL);

    // 历史重载路径(mapHistoryEntry)同一转换;dataURL(旧条目)原样。
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).endsWith('/history')) {
          return new Response(
            JSON.stringify({
              entries: [
                {
                  kind: 'detection',
                  data: {
                    events: [
                      { event_id: 3, annotated_image: REF },
                      { event_id: 5, annotated_image: 'data:image/jpeg;base64,WFg=' },
                    ],
                  },
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
    await agent.selectSession('s2');
    vi.unstubAllGlobals();
    const det2 = agent.entries[0];
    if (det2.kind !== 'detection') throw new Error('expected detection entry');
    const events2 = (det2.data as { events: Array<{ annotated_image?: string }> }).events;
    expect(events2[0]?.annotated_image).toBe(REF_URL);
    expect(events2[1]?.annotated_image).toBe('data:image/jpeg;base64,WFg=');
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

  it('generate_done 把该步 generate 耗时写到当前 assistant 条目;纯工具步丢弃;generate_retry 插本地警示', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    stubChatStream([
      { type: 'think_delta', text: '想一下' },
      { type: 'generate_done', step: 1, generateMs: 12000, serverMs: 11000 },
      { type: 'tool_call_start', call: { id: 'c1', name: 'echo', arguments: '{}' } },
      // 纯工具调用步:无 assistant 条目可写,耗时丢弃(不建空气泡)
      { type: 'generate_done', step: 2, generateMs: 3000 },
      {
        type: 'tool_result',
        toolCallId: 'c1',
        name: 'echo',
        result: { output: 'ok' },
        isError: false,
      },
      { type: 'generate_retry', step: 2, error: '400 Unterminated string' },
      { type: 'text_delta', text: '答' },
      { type: 'generate_done', step: 3, generateMs: 500 },
      { type: 'done', reason: 'completed' },
    ]);
    await agent.send('测试');
    vi.unstubAllGlobals();

    const kinds = agent.entries.map((e) => e.kind);
    expect(kinds).toEqual(['user', 'assistant', 'tool', 'system', 'assistant']);
    const a1 = agent.entries[1];
    const a2 = agent.entries[4];
    if (a1?.kind !== 'assistant' || a2?.kind !== 'assistant') {
      throw new Error('expected assistant entries');
    }
    expect(a1.generateMs).toBe(12000);
    expect(a2.generateMs).toBe(500);
    const warn = agent.entries[3];
    expect(warn).toMatchObject({ kind: 'system', tone: 'warn' });
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
      video_path: 'demo.mp4',
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      events: [
        {
          event_id: 3,
          detected: true,
          confidence: 0.9,
          reasoning: '追尾',
          evidence_frames: [3.5],
          annotated_image: 'data:image/jpeg;base64,WFg=',
        },
        { event_id: 5, detected: true, confidence: 0.8, reasoning: '逆行', evidence_frames: [8.2, 9.1] },
      ],
      meta: { missing_boxes: [5] },
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
    expect(last).toMatchObject({
      kind: 'system',
      text: '输出达到 token 上限被截断,部分内容可能不完整,可继续追问',
      tone: 'warn',
    });
    expect(typeof last.id).toBe('string'); // 条目进入时间线即有前端 id
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

// D 任务回归:恢复链路按 seq 去重——恢复探测与 5s 自动轮询连续拉取/重叠返回
// 同一段落盘 events(水位回退重发、手动刷新与自动轮询并发的历史形态)时,
// 同一条目(detection/收尾 assistant 等)只入时间线一次,「最后结果出现多次」不再发生。
describe('恢复轮询去重(同一段 events 只入时间线一次)', () => {
  it('恢复探测后连续两次轮询返回同一段 events:条目不重复追加', async () => {
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
          // 恶劣情况:后端每次都返回同一段(忽略 fromSeq),全靠前端按 seq 去重
          return jsonResponse({
            events: [
              { seq: 2, entry: { kind: 'assistant', text: '恢复的回答', think: '', at: 2 } },
              { seq: 3, entry: { kind: 'detection', data: { normal: true }, at: 3 } },
            ],
            inProgress: eventCalls < 3, // 第三次拉取才收尾
          });
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    // 第 1 次拉取(恢复探测):补齐 assistant + detection,进入恢复态
    await agent.selectSession('s1');
    expect(agent.entries).toHaveLength(3);
    expect(agent.entries.filter((e) => e.kind === 'detection')).toHaveLength(1);
    expect(agent.recovering).toBe(true);

    // 第 2 次拉取(5s 轮询):同一段重放 → 整体跳过,不重复
    await vi.advanceTimersByTimeAsync(5000);
    expect(agent.entries).toHaveLength(3);
    expect(agent.recovering).toBe(true);

    // 第 3 次拉取:inProgress=false 收尾,条目仍不重复
    await vi.advanceTimersByTimeAsync(5000);
    expect(agent.entries).toHaveLength(3);
    expect(agent.entries.map((e) => e.kind)).toEqual(['user', 'assistant', 'detection']);
    expect(agent.status).toBe('done');
    expect(agent.recovering).toBe(false);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});

// openSession 跨工作区切换测试(会话栏点击入口):
// 会话 workspaceDir 与当前工作区不同时,先 POST /api/workspace 切工作区
// (等它返回——path 变更触发的 watch 在其 resolve 前清空会话态),成功后再
// selectSession;切换失败(400/403)抛错且不选会话。当前工作区会话直接选择。
describe('openSession 跨工作区切换', () => {
  function stubWorkspaceFetch(calls: Array<{ url: string; method: string }>) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        const url = String(input);
        const method = init?.method ?? 'GET';
        calls.push({ url, method });
        if (url === '/api/workspace' && method === 'POST') {
          return jsonResponse({ path: '/ws/b' });
        }
        if (url.endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: 'B 工作区会话', images: [], at: 1 }]);
        }
        if (url.includes('/events')) return jsonResponse({ events: [], inProgress: false });
        if (url.endsWith('/api/agent/sessions')) {
          return jsonResponse({
            sessions: [
              { id: 's1', workspaceDir: '/ws/a' },
              { id: 's2', workspaceDir: '/ws/b', title: 'B 会话' },
            ],
          });
        }
        // loadTree:/workspace/tree、/workspace/videos
        return jsonResponse({ entries: [] });
      }),
    );
  }

  it('点击其他工作区会话:先 POST /api/workspace,成功后再 selectSession', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessionId = 's1';
    agent.entries = [{ kind: 'user', text: 'hi' }];
    agent.sessions = [
      { id: 's1', workspaceDir: '/ws/a' },
      { id: 's2', workspaceDir: '/ws/b', title: 'B 会话' },
    ];
    const calls: Array<{ url: string; method: string }> = [];
    stubWorkspaceFetch(calls);

    await agent.openSession('s2');
    vi.unstubAllGlobals();

    const wsIdx = calls.findIndex((c) => c.url === '/api/workspace' && c.method === 'POST');
    const histIdx = calls.findIndex((c) => c.url === '/api/agent/sessions/s2/history');
    expect(wsIdx).toBeGreaterThanOrEqual(0);
    expect(histIdx).toBeGreaterThanOrEqual(0);
    expect(wsIdx).toBeLessThan(histIdx); // 先切工作区,后选会话
    expect(ws.path).toBe('/ws/b');
    expect(agent.sessionId).toBe('s2');
    expect(agent.entries).toHaveLength(1);
    expect(agent.entries[0]).toMatchObject({ kind: 'user', text: 'B 工作区会话' });
  });

  it('工作区切换失败(400 目录不存在):抛错,不选会话,当前会话态保持', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessionId = 's1';
    agent.entries = [{ kind: 'user', text: 'hi' }];
    agent.sessions = [
      { id: 's1', workspaceDir: '/ws/a' },
      { id: 's2', workspaceDir: '/ws/gone' },
    ];
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? 'GET' });
        if (url === '/api/workspace') return jsonResponse({ detail: '目录不存在' }, 400);
        return jsonResponse({});
      }),
    );

    await expect(agent.openSession('s2')).rejects.toThrow('目录不存在');
    vi.unstubAllGlobals();

    expect(calls.some((c) => c.url.includes('/history'))).toBe(false); // 未发起 selectSession
    expect(ws.path).toBe('/ws/a'); // 工作区未变
    expect(agent.sessionId).toBe('s1'); // 当前会话态保持
    expect(agent.entries).toHaveLength(1);
  });

  it('当前工作区会话:直接 selectSession,不调 workspace API;无 workspaceDir 旧会话同理', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessions = [
      { id: 's1', workspaceDir: '/ws/a' },
      { id: 's9' }, // 旧会话:无 workspaceDir
    ];
    const calls: Array<{ url: string; method: string }> = [];
    stubWorkspaceFetch(calls);

    await agent.openSession('s1');
    expect(calls.some((c) => c.url === '/api/workspace')).toBe(false);
    expect(agent.sessionId).toBe('s1');

    calls.length = 0;
    await agent.openSession('s9');
    vi.unstubAllGlobals();
    expect(calls.some((c) => c.url === '/api/workspace')).toBe(false);
    expect(agent.sessionId).toBe('s9');
    expect(ws.path).toBe('/ws/a');
  });
});

// deleteSession 跨工作区守卫回归测试:
// 删除当前会话后从全部会话按 lastActiveAt 取最近直接 selectSession,若该会话
// 属于其他工作区会绕过 openSession 的 applyWorkspace,时间线加载了别的工作区
// 的会话但 ws.path 仍是旧工作区。期望:优先选当前工作区内最近的会话(不动
// 工作区);只剩其他工作区的会话时走 openSession 先切工作区,ws.path 与新
// 选中会话的工作区一致。
describe('deleteSession 跨工作区守卫', () => {
  function stubDeleteFetch(calls: Array<{ url: string; method: string }>) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        const url = String(input);
        const method = init?.method ?? 'GET';
        calls.push({ url, method });
        if (url === '/api/workspace' && method === 'POST') {
          return jsonResponse({ path: '/ws/b' });
        }
        if (url.endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: '历史会话', images: [], at: 1 }]);
        }
        if (url.includes('/events')) return jsonResponse({ events: [], inProgress: false });
        if (url.endsWith('/api/agent/sessions')) {
          return jsonResponse({
            sessions: [
              { id: 's2', workspaceDir: '/ws/b', lastActiveAt: 200 },
              { id: 's3', workspaceDir: '/ws/a', lastActiveAt: 100 },
            ],
          });
        }
        // loadTree:/workspace/tree、/workspace/videos
        return jsonResponse({ entries: [] });
      }),
    );
  }

  it('删除当前会话后只剩其他工作区会话:走 openSession 先切工作区,ws.path 与新会话工作区一致', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessionId = 's1';
    agent.sessions = [
      { id: 's1', workspaceDir: '/ws/a', lastActiveAt: 300 },
      { id: 's2', workspaceDir: '/ws/b', lastActiveAt: 200 },
    ];
    const calls: Array<{ url: string; method: string }> = [];
    stubDeleteFetch(calls);

    await agent.deleteSession('s1');
    vi.unstubAllGlobals();

    const wsIdx = calls.findIndex((c) => c.url === '/api/workspace' && c.method === 'POST');
    const histIdx = calls.findIndex((c) => c.url === '/api/agent/sessions/s2/history');
    expect(wsIdx).toBeGreaterThanOrEqual(0);
    expect(histIdx).toBeGreaterThanOrEqual(0);
    expect(wsIdx).toBeLessThan(histIdx); // 先切工作区,后选会话
    expect(ws.path).toBe('/ws/b'); // 与新选中会话的 workspaceDir 一致
    expect(agent.sessionId).toBe('s2');
  });

  it('当前工作区还有会话(即使更旧):留在当前工作区,不调 workspace API', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessionId = 's1';
    agent.sessions = [
      { id: 's1', workspaceDir: '/ws/a', lastActiveAt: 300 },
      { id: 's2', workspaceDir: '/ws/b', lastActiveAt: 200 },
      { id: 's3', workspaceDir: '/ws/a', lastActiveAt: 100 },
    ];
    const calls: Array<{ url: string; method: string }> = [];
    stubDeleteFetch(calls);

    await agent.deleteSession('s1');
    vi.unstubAllGlobals();

    expect(calls.some((c) => c.url === '/api/workspace' && c.method === 'POST')).toBe(false);
    expect(ws.path).toBe('/ws/a');
    expect(agent.sessionId).toBe('s3'); // 当前工作区内最近的(虽然比 s2 旧)
  });
});

// D4 回归:条目 id + 会话实例替换 + 落盘序号 seq。
describe('条目身份(entryId/seq)', () => {
  it('条目进入时间线即赋 id:历史加载与流式条目都有,且互不相同', async () => {
    const { agent } = await load();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        if (String(input).endsWith('/history')) {
          return historyResponse([{ kind: 'user', text: 'hi', images: [], at: 1 }]);
        }
        return jsonResponse({ sessions: [] });
      }),
    );
    await agent.selectSession('s1');
    stubChatStream([
      { type: 'text_delta', text: '答', seq: 2 },
      { type: 'done', reason: 'stop_turn', seq: 2 },
    ]);
    await agent.send('再问');
    vi.unstubAllGlobals();

    const ids = agent.entries.map((e) => e.id);
    expect(ids).toHaveLength(3); // 历史 user + 新 user + assistant
    expect(new Set(ids).size).toBe(3); // 全局唯一
    for (const id of ids) expect(typeof id).toBe('string');
    // 落盘序号:历史条目 = 数组位置+1,流式 user 条目按水位+1 推算
    expect(agent.entries[0]).toMatchObject({ kind: 'user', seq: 1 });
    expect(agent.entries[1]).toMatchObject({ kind: 'user', seq: 2 });
  });

  it('撤回按条目 id 定位:entryIndex 直送落盘序号-1,不再做下标换算', async () => {
    const { agent } = await load();
    const bodies: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { body?: unknown }) => {
        const url = String(input);
        if (url.endsWith('/history')) {
          // 中间夹一条本地 system 条目(截断警示)验证:撤回定位不再受其干扰
          return historyResponse([
            { kind: 'user', text: '第一问', images: [], at: 1 },
            { kind: 'assistant', text: '答一', think: '', at: 2 },
          ]);
        }
        if (url.endsWith('/recall')) {
          bodies.push(init?.body ? JSON.parse(String(init.body)) : null);
          return jsonResponse({ status: 'ok' });
        }
        return jsonResponse({ sessions: [] });
      }),
    );
    await agent.selectSession('s1');
    const first = agent.entries[0]!;
    await agent.recallFrom(first.id);
    vi.unstubAllGlobals();

    expect(bodies).toEqual([{ entryIndex: 0 }]); // seq 1 → 后端下标 0
    expect(agent.entries).toEqual([]);
  });

  it('撤回流式轮次产生的条目:seq 来自 SSE 事件水位(含前置 system 条目也不换算错)', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    const bodies: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { body?: unknown }) => {
        const url = String(input);
        if (url.endsWith('/recall')) {
          bodies.push(init?.body ? JSON.parse(String(init.body)) : null);
          return jsonResponse({ status: 'ok' });
        }
        if (url.endsWith('/api/agent/chat')) {
          return sseResponse([
            { type: 'compaction', seq: 0 }, // 本地 system 条目:不落盘、不占 seq
            { type: 'text_delta', text: '答', seq: 2 },
            { type: 'done', reason: 'stop_turn', truncated: true, seq: 2 },
          ]);
        }
        return jsonResponse({ sessions: [] });
      }),
    );
    await agent.send('问一');
    // 时间线:user + system(压缩提示)+ assistant + system(截断警示)
    expect(agent.entries.map((e) => e.kind)).toEqual(['user', 'system', 'assistant', 'system']);
    const user = agent.entries[0]!;
    await agent.recallFrom(user.id);
    vi.unstubAllGlobals();

    expect(bodies).toEqual([{ entryIndex: 0 }]); // 落盘序号 1,剔除 system 的换算已删除
    expect(agent.entries).toEqual([]);
  });
});

// D4 回归:同文本两次 steer 不误判——乐观账按条目 id 队列消费,
// 两次「快点」各绑各的,轮询/SSE 补齐不得把两条折叠成一条。
describe('同文本多次 steer', () => {
  it('SSE steer 事件按 id 队列出队:同文本两次插话各保留一条,seq 各自绑定', async () => {
    const { agent } = await load();
    agent.sessionId = 's1';
    // 可控 SSE 流:先发一轮 /chat 挂起,插话两次后事件才到达(真实时序)
    const enc = new TextEncoder();
    let pushEvent: (e: unknown) => void = () => {};
    let closeStream: () => void = () => {};
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        pushEvent = (e) => c.enqueue(enc.encode(`data: ${JSON.stringify(e)}\n\n`));
        closeStream = () => c.close();
      },
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/steer')) return jsonResponse({ status: 'ok' });
        if (url.endsWith('/api/agent/chat')) {
          return new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          });
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    const turn = agent.send('首轮'); // 进入 running(流挂起,未发事件)
    await agent.send('快点'); // 第一次插话(乐观插入)
    await agent.send('快点'); // 第二次同文本插话
    // 后端契约:同批 steer 条目整体 flush 后逐条 emit,事件 seq 同为批末水位 3
    pushEvent({ type: 'steer', text: '快点', images: [], seq: 3 });
    pushEvent({ type: 'steer', text: '快点', images: [], seq: 3 });
    pushEvent({ type: 'text_delta', text: '好的', seq: 4 });
    pushEvent({ type: 'done', reason: 'stop_turn', seq: 4 });
    closeStream();
    await turn;
    vi.unstubAllGlobals();

    const users = agent.entries.filter((e) => e.kind === 'user');
    expect(users.map((e) => (e.kind === 'user' ? e.text : ''))).toEqual(['首轮', '快点', '快点']);
    expect(users.map((e) => (e.kind === 'user' ? !!e.steered : false))).toEqual([false, true, true]);
    // 两次插话各自绑到落盘序号 2/3(而非都算到一条头上)
    expect(users.map((e) => (e.kind === 'user' ? e.seq : undefined))).toEqual([1, 2, 3]);
  });

  it('轮询补齐的同文本 steer:user 落盘条目按 id 队列消费,不重复插入', async () => {
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
          return jsonResponse({
            events: [
              { seq: 2, entry: { kind: 'user', text: '快点', images: [], at: 2 } },
              { seq: 3, entry: { kind: 'user', text: '快点', images: [], at: 3 } },
              { seq: 4, entry: { kind: 'assistant', text: '好的', think: '', at: 4 } },
            ],
            inProgress: false,
          });
        }
        return jsonResponse({ sessions: [] });
      }),
    );

    await agent.selectSession('s1'); // 进入恢复态(running)
    await agent.send('快点'); // 第一次插话
    await agent.send('快点'); // 第二次同文本插话
    expect(agent.entries).toHaveLength(3);

    await vi.advanceTimersByTimeAsync(5000);
    vi.unstubAllGlobals();
    vi.useRealTimers();

    // 两条落盘的「快点」各消费一条乐观账;只补 assistant,不再重复插 user
    expect(agent.entries.map((e) => (e.kind === 'user' ? e.text : e.kind))).toEqual([
      '首轮',
      '快点',
      '快点',
      'assistant',
    ]);
    const steered = agent.entries.filter((e) => e.kind === 'user' && e.steered);
    expect(steered.map((e) => (e as { seq?: number }).seq)).toEqual([2, 3]); // seq 绑回乐观条目
  });
});

// D4 回归:快速连续切换两个不同工作区的会话,时间线必须属于最后选中的会话
// (旧实现靠 selectSeq 手工守卫;新实现 = 实例替换后晚到响应整体丢弃)。
describe('快速跨工作区切换会话', () => {
  it('s2@wsB 在途时点 s3@wsA:晚到的 s2 历史不写回,时间线属于 s3', async () => {
    const { ws, agent } = await load();
    ws.path = '/ws/a';
    await nextTick();
    agent.sessions = [
      { id: 's2', workspaceDir: '/ws/b' },
      { id: 's3', workspaceDir: '/ws/a' },
    ];
    // POST /api/workspace 与 /history 都可控:精确交错两次跨工作区点击
    const wsPosts: Array<(r: Response) => void> = [];
    const releases = new Map<string, (r: Response) => void>();
    const sessionList = [
      { id: 's2', workspaceDir: '/ws/b' },
      { id: 's3', workspaceDir: '/ws/a' },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn((input: unknown, init?: { method?: string }) => {
        const url = String(input);
        const method = init?.method ?? 'GET';
        if (url === '/api/workspace' && method === 'POST') {
          const dir = wsPosts.length === 0 ? '/ws/b' : '/ws/a';
          return new Promise<Response>((resolve) => wsPosts.push(() => resolve(jsonResponse({ path: dir }))));
        }
        if (url.endsWith('/history')) {
          return new Promise<Response>((resolve) => releases.set(url, resolve));
        }
        if (url === '/api/agent/sessions') return Promise.resolve(jsonResponse({ sessions: sessionList }));
        return Promise.resolve(jsonResponse({ entries: [] }));
      }),
    );

    // 第一次点击 s2@wsB:切工作区(完成)+ history 在途
    const p2 = agent.openSession('s2');
    wsPosts[0]!(() => undefined);
    await vi.waitFor(() => {
      expect(agent.status).toBe('connecting'); // selectSession('s2') 已发起
    });
    // 第二次点击 s3@wsA:再切回 wsA(其 watch 会作废 s2 的选择)
    const p3 = agent.openSession('s3');
    await vi.waitFor(() => {
      expect(wsPosts.length).toBe(2); // s3 的 applyWorkspace 已发起
    });
    wsPosts[1]!(() => undefined);
    // s3 的 history 先落地:生效
    await vi.waitFor(() => {
      expect(releases.size).toBe(2);
    });
    releases.get('/api/agent/sessions/s3/history')!(
      historyResponse([{ kind: 'user', text: 'A 会话', images: [], at: 1 }]),
    );
    await p3;
    expect(agent.sessionId).toBe('s3');
    expect(agent.entries).toHaveLength(1);
    // s2 的 history 晚到:实例已被替换,整体丢弃
    releases.get('/api/agent/sessions/s2/history')!(
      historyResponse([{ kind: 'user', text: 'B 会话', images: [], at: 2 }]),
    );
    await p2;
    vi.unstubAllGlobals();

    expect(agent.sessionId).toBe('s3');
    expect(ws.path).toBe('/ws/a');
    expect(agent.entries).toHaveLength(1);
    expect(agent.entries[0]).toMatchObject({ kind: 'user', text: 'A 会话' });
  });
});

// renameSession:POST /sessions/{id}/title 成功后就地更新列表项标题;
// 失败抛错且本地列表不变(调用方负责提示)。
describe('renameSession 自定义标题', () => {
  it('成功:更新列表项标题为后端返回值(trim 后)', async () => {
    const { agent } = await load();
    agent.sessions = [{ id: 's1', workspaceDir: '/ws/a', title: '分析视频', lastActiveAt: 1 }];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url.endsWith('/title')) return jsonResponse({ status: 'ok', title: '倒车复检' });
        return jsonResponse({});
      }),
    );

    await agent.renameSession('s1', '  倒车复检  ');
    vi.unstubAllGlobals();

    expect(agent.sessions[0]?.title).toBe('倒车复检');
  });

  it('失败:抛错且本地标题不变', async () => {
    const { agent } = await load();
    agent.sessions = [{ id: 's1', workspaceDir: '/ws/a', title: '分析视频', lastActiveAt: 1 }];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ error: { message: 'boom' } }), { status: 500 })),
    );

    await expect(agent.renameSession('s1', 'x')).rejects.toThrow();
    vi.unstubAllGlobals();

    expect(agent.sessions[0]?.title).toBe('分析视频');
  });
});
