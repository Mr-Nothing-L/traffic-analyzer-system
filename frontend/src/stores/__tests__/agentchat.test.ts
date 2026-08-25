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
});
