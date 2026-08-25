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
