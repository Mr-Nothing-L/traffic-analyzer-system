// workspace store 分析报告删除逻辑测试(乐观清 has_results,失败回滚):
// - 单删成功:root/children 各层/videos 三处 has_results 同步翻转(浅响应式
//   约定下整体替换),刷新树静默重拉;
// - 单删失败:回滚为 true 并把异常抛给调用方提示;
// - 批量部分失败:ok=false 条目按 stem 映射回 rel 回滚,其余保持已删;
// - 批量网络级失败:全部回滚并抛错。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import type { TreeEntry } from '../workspace';
import type { VideoInfo } from '../workspace';

beforeEach(() => {
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => void storage.set(k, v),
    removeItem: (k: string) => void storage.delete(k),
    clear: () => storage.clear(),
  });
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function load() {
  const { useWorkspaceStore } = await import('../workspace');
  return useWorkspaceStore();
}

function video(rel: string): VideoInfo {
  const name = rel.split('/').pop() ?? rel;
  return { name, stem: name.replace(/\.[^.]+$/, ''), rel, size: 1, mtime: 1, has_results: true };
}

function entry(rel: string): TreeEntry {
  const name = rel.split('/').pop() ?? rel;
  return { name, rel, type: 'file', is_video: true,
    stem: name.replace(/\.[^.]+$/, ''), has_results: true };
}

interface Fixture {
  v1: VideoInfo;
  nested: VideoInfo;
  e1: TreeEntry;
  en: TreeEntry;
}

/** root 一层视频 v1.mp4 + 目录 sub(children['sub'] 内嵌套视频)。 */
async function seededStore(): Promise<{ ws: ReturnType<typeof load> extends Promise<infer T> ? T : never; fx: Fixture }> {
  const ws = await load();
  const fx: Fixture = {
    v1: video('v1.mp4'),
    nested: video('sub/nested.mp4'),
    e1: entry('v1.mp4'),
    en: entry('sub/nested.mp4'),
  };
  ws.root = [fx.e1, { name: 'sub', rel: 'sub', type: 'dir' }];
  ws.children['sub'] = [fx.en];
  ws.videos = [fx.v1, fx.nested];
  ws.loaded = true; // 树已加载态(refreshTree 才会静默重拉)
  return { ws, fx } as { ws: typeof ws; fx: Fixture };
}

type Ws = Awaited<ReturnType<typeof load>>;

function stubDelete(
  results: Array<{ stem: string; ok: boolean; existed: boolean; error?: string }>,
  calls: Array<{ url: string; method: string; body?: unknown }> = [],
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: unknown, init?: { method?: string; body?: unknown }) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: init?.body });
      if (method === 'DELETE' && url.startsWith('/api/workspace/analysis/')) {
        const stem = decodeURIComponent(url.replace('/api/workspace/analysis/', ''));
        const item = results.find((r) => r.stem === stem);
        return new Response(JSON.stringify(item ?? { stem, ok: true, existed: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/workspace/analysis/delete')) {
        return new Response(JSON.stringify(results), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      // refreshTree 的静默重拉(tree/videos/children)
      return new Response(JSON.stringify({ entries: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
}

describe('单删报告 deleteReport', () => {
  it('成功:root/videos 的 has_results 翻为 false,DELETE 打到正确端点,随后静默刷树', async () => {
    const { ws, fx }: { ws: Ws; fx: Fixture } = await seededStore();
    const calls: Array<{ url: string; method: string }> = [];
    stubDelete([{ stem: 'v1', ok: true, existed: true }], calls);

    await ws.deleteReport('v1.mp4');

    expect(calls.some((c) => c.url === '/api/workspace/analysis/v1' && c.method === 'DELETE')).toBe(true);
    expect(ws.root[0]!.has_results).toBe(false); // 整体替换后的新条目(fx.e1 为旧对象)
    expect(ws.root[0]).not.toBe(fx.e1);
    expect(ws.videos[0]!.has_results).toBe(false);
    await vi.waitFor(() => {
      // 静默刷新落地(root 被 [] 整体替换即证明 refreshTree 跑过)
      expect(ws.root).toEqual([]);
    });
  });

  it('嵌套子层条目同样乐观更新', async () => {
    const { ws, fx }: { ws: Ws; fx: Fixture } = await seededStore();
    stubDelete([{ stem: 'nested', ok: true, existed: true }]);
    await ws.deleteReport('sub/nested.mp4');
    expect(ws.children['sub']![0]!.has_results).toBe(false);
  });

  it('失败:has_results 回滚为 true,异常抛给调用方', async () => {
    const { ws }: { ws: Ws; fx: Fixture } = await seededStore();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: 'boom' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    await expect(ws.deleteReport('v1.mp4')).rejects.toThrow('boom');
    expect(ws.root[0]!.has_results).toBe(true); // 回滚(整体替换回原值)
    expect(ws.videos[0]!.has_results).toBe(true);
  });
});

describe('批量删报告 deleteReports', () => {
  it('部分失败:ok=false 条目回滚徽标,ok=true 保持已删;逐项结果透传', async () => {
    const { ws, fx }: { ws: Ws; fx: Fixture } = await seededStore();
    stubDelete([
      { stem: 'v1', ok: true, existed: true },
      { stem: 'nested', ok: false, existed: true, error: 'permission denied' },
    ]);
    const r = await ws.deleteReports(['v1.mp4', 'sub/nested.mp4']);
    expect(r).toHaveLength(2);
    expect(ws.videos.map((v) => v.has_results)).toEqual([false, true]); // v1 已删,nested 回滚
    expect(ws.children['sub']![0]!.has_results).toBe(true); // 失败项的树条目同步回滚
  });

  it('网络级失败:全部回滚并抛错', async () => {
    const { ws, fx }: { ws: Ws; fx: Fixture } = await seededStore();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        // 批量请求网络断连,其余(refreshTree 不应触达——已抛错)放行空响应
        if (String(input).endsWith('/api/workspace/analysis/delete')) throw new TypeError('down');
        return new Response(JSON.stringify({ entries: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }),
    );
    await expect(ws.deleteReports(['v1.mp4', 'sub/nested.mp4'])).rejects.toThrow();
    expect(ws.videos.map((v) => v.has_results)).toEqual([true, true]); // 整体回滚
  });

  it('相同 stem 只发一次批量项(stem 去重,共享同一报告目录)', async () => {
    const ws = await load();
    ws.videos = [video('a.mp4'), video('sub/a.mp4')];
    ws.root = [entry('a.mp4'), { name: 'sub', rel: 'sub', type: 'dir' }];
    ws.children['sub'] = [entry('sub/a.mp4')];
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    stubDelete([{ stem: 'a', ok: true, existed: true }], calls);
    await ws.deleteReports(['a.mp4', 'sub/a.mp4']);
    const batchCall = calls.find((c) => c.url.endsWith('/api/workspace/analysis/delete'));
    expect(JSON.parse(String(batchCall!.body))).toEqual({ stems: ['a'] });
  });

  it('空列表:不发批量请求,状态不变', async () => {
    const { ws, fx }: { ws: Ws; fx: Fixture } = await seededStore();
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        calls.push({ url: String(input), method: init?.method ?? 'GET' });
        return new Response(JSON.stringify({ entries: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }),
    );
    await ws.deleteReports([]);
    expect(calls.some((c) => c.url.endsWith('/api/workspace/analysis/delete'))).toBe(false);
    expect(ws.videos.map((v) => v.has_results)).toEqual([true, true]); // 未受影响
    expect(fx.e1.has_results).toBe(true);
  });
});
