// 工作区切换重置对话会话回归测试:
// agent 会话由后端注入 workspaceDir,切换工作区后旧会话仍绑旧工作区,
// 新工作区相对路径送入旧会话会解析失败。期望:工作区路径变化清空当前会话态
// (sessionId/entries/pendingVideo,不删后端历史会话列表),首次加载不触发。
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { nextTick } from 'vue';
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
});
