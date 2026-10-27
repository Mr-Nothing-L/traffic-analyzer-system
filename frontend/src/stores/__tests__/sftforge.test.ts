// SFT Forge 任务状态机测试(stores/sftforge.ts,契约见 api/sftforge.ts):
// - start:创建成功 → running,视频列表按创建响应占位,SSE 连接逐任务地址;
// - SSE 事件落地:round_started 占位 running 卡,round_completed 回填 trace
//   转 done;video_completed 落定摘要;事件幂等(重连回放不重复建卡);
// - 终态:job_completed → done 并拉 /result;job_failed → failed 带错误文案;
// - onerror 兜底:readyState=CLOSED 置 failed(不假死),CONNECTING 不动;
//   事件 data 非 JSON 跳条不中断流;
// - 事件名映射复用 /api/config/events,targetNamesOf 输出纠错目标中文名集合;
// - 创建失败(400)入 error,不进入 running。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useSftForgeStore } from '../sftforge';
import type { ForgeResultPayload, ForgeStoredResult, RoundTrace } from '../../api/sftforge';

/* ---- fake EventSource:捕获监听,测试手动派发命名事件 ---- */
type Listener = (ev: unknown) => void;
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static CLOSED = 2; // 与浏览器 EventSource.CLOSED 常量一致
  url: string;
  listeners = new Map<string, Listener[]>();
  closed = false;
  readyState = 1; // OPEN
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: Listener) {
    const arr = this.listeners.get(type) || [];
    arr.push(fn);
    this.listeners.set(type, arr);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown) {
    for (const fn of this.listeners.get(type) || []) {
      fn({ data: JSON.stringify(data) });
    }
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function trace(over: Partial<RoundTrace> = {}): RoundTrace {
  return {
    round_no: 1,
    kind: 'correct_missing',
    event_ids: [6],
    prompt: '请重新观察…',
    response: '<think>\n交通拥堵：多车道出现拥堵。\n</think>',
    patched: true,
    ...over,
  };
}

function resultPayload(over: Partial<ForgeResultPayload> = {}): ForgeResultPayload {
  return {
    job_id: 7,
    status: 'done',
    error: null,
    stats: { match: 1, quarantine: 0, skipped: 0 },
    videos: [
      { video: '/data/a.mp4', match: true, skipped: false, reason: null, gt: [1, 6], rounds: 2 },
    ],
    jsonl: [{ index: 0, video: '/data/a.mp4', output: '<think></think>' }],
    quarantine: [],
    ...over,
  };
}

/** 按 URL 路由的 fake fetch:config/events + jobs 创建 + result + store 读/删。 */
function stubFetch(
  resultBody: ForgeResultPayload = resultPayload(),
  storedBody?: ForgeStoredResult,
) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: unknown, init?: { method?: string; body?: unknown }) => {
      const url = String(input);
      calls.push(url + ' ' + (init?.method || 'GET'));
      if (url === '/api/config/events') {
        return jsonResponse([
          { event_id: 1, name_zh: '违法停车' },
          { event_id: 6, name_zh: '交通拥堵' },
        ]);
      }
      if (url === '/api/sftforge/jobs' && init?.method === 'POST') {
        return jsonResponse({ job_id: 7, videos: ['/data/a.mp4', '/data/b.mp4'] });
      }
      if (url === '/api/sftforge/jobs/7/result') return jsonResponse(resultBody);
      if (url === '/api/sftforge/store' && init?.method === 'DELETE') {
        return jsonResponse({ deleted: true });
      }
      if (url === '/api/sftforge/store') {
        return storedBody
          ? jsonResponse(storedBody)
          : jsonResponse({ detail: 'No stored sftforge result' }, 404);
      }
      throw new Error('unexpected url: ' + url);
    }),
  );
  return calls;
}

beforeEach(() => {
  setActivePinia(createPinia());
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('创建任务与 SSE 连接', () => {
  it('创建成功:running 态,视频按响应占位,SSE 连逐任务地址', async () => {
    stubFetch();
    const store = useSftForgeStore();
    const ok = await store.start({ dir: '/data', max_rounds: 2 });
    expect(ok).toBe(true);
    expect(store.status).toBe('running');
    expect(store.jobId).toBe(7);
    expect(store.videos.map((v) => v.video)).toEqual(['/data/a.mp4', '/data/b.mp4']);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/api/sftforge/jobs/7/events');
    // 事件名映射已加载
    expect(store.eventNames[6]).toBe('交通拥堵');
  });

  it('创建失败(后端 400):error 带 detail 文案,不进 running', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        const url = String(input);
        if (url === '/api/config/events') return jsonResponse([]);
        return jsonResponse({ detail: '目录不存在: /nope' }, 400);
      }),
    );
    const store = useSftForgeStore();
    const ok = await store.start({ dir: '/nope' });
    expect(ok).toBe(false);
    expect(store.status).toBe('idle');
    expect(store.error).toBe('目录不存在: /nope');
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});

describe('SSE 事件落地(幂等)', () => {
  it('round_started 占位 → round_completed 回填 trace 转 done', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];

    es.emit('round_started', { video: '/data/a.mp4', round_no: 1, kind: 'correct_missing', event_ids: [6] });
    let v = store.videos[0];
    expect(v.rounds).toHaveLength(1);
    expect(v.rounds[0].state).toBe('running');
    expect(v.rounds[0].trace).toBeNull();

    es.emit('round_completed', { video: '/data/a.mp4', trace: trace() });
    v = store.videos[0];
    expect(v.rounds).toHaveLength(1);
    expect(v.rounds[0].state).toBe('done');
    expect(v.rounds[0].trace?.patched).toBe(true);
    expect(v.rounds[0].trace?.prompt).toContain('重新观察');
  });

  it('重连回放重复事件不产生重复卡片', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];
    const started = { video: '/data/a.mp4', round_no: 0, kind: 'forward', event_ids: [] };
    es.emit('round_started', started);
    es.emit('round_started', started); // 回放重复
    es.emit('round_completed', { video: '/data/a.mp4', trace: trace({ round_no: 0, kind: 'forward', event_ids: [] }) });
    es.emit('round_completed', { video: '/data/a.mp4', trace: trace({ round_no: 0, kind: 'forward', event_ids: [] }) });
    expect(store.videos[0].rounds).toHaveLength(1);
  });

  it('round_delta 流式增量:追加 → reset 清空 → completed 回填后清缓冲', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];

    es.emit('round_started', { video: '/data/a.mp4', round_no: 0, kind: 'forward', event_ids: [] });
    es.emit('round_delta', { video: '/data/a.mp4', round_no: 0, kind: 'forward', field: 'reasoning', text: '思1' });
    es.emit('round_delta', { video: '/data/a.mp4', round_no: 0, kind: 'forward', field: 'reasoning', text: '思2' });
    es.emit('round_delta', { video: '/data/a.mp4', round_no: 0, kind: 'forward', field: 'response', text: '答1' });
    let r = store.videos[0].rounds[0];
    expect(r.streamReasoning).toBe('思1思2');
    expect(r.streamResponse).toBe('答1');

    // reset(重试/failover 作废):两个缓冲都清空
    es.emit('round_delta', { video: '/data/a.mp4', round_no: 0, kind: 'forward', field: 'reset', text: '' });
    r = store.videos[0].rounds[0];
    expect(r.streamReasoning).toBe('');
    expect(r.streamResponse).toBe('');

    // 重建增量,completed 回填 trace 后清缓冲转 done
    es.emit('round_delta', { video: '/data/a.mp4', round_no: 0, kind: 'forward', field: 'response', text: '答' });
    es.emit('round_completed', { video: '/data/a.mp4', trace: trace({ round_no: 0, kind: 'forward', event_ids: [] }) });
    r = store.videos[0].rounds[0];
    expect(r.state).toBe('done');
    expect(r.streamReasoning).toBe('');
    expect(r.streamResponse).toBe('');
  });

  it('round_delta 先于 round_started 到达(乱序)也就地占位', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    store.onRoundDelta({ video: '/data/a.mp4', round_no: 1, kind: 'correct_missing', field: 'reasoning', text: '思' });
    const v = store.videos[0];
    expect(v.rounds).toHaveLength(1);
    expect(v.rounds[0].state).toBe('running');
    expect(v.rounds[0].streamReasoning).toBe('思');
  });

  it('draft_step 初稿步骤:追加并按内容去重(回放不产生重复行)', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];
    const step = { video: '/data/a.mp4', type: 'step', step: 1, total: 4, name: '视频预处理' };
    es.emit('draft_step', step);
    es.emit('draft_step', step); // 回放重复
    es.emit('draft_step', { video: '/data/a.mp4', type: 'lane_done', lane: '违法停车', result: 'detected' });
    const v = store.videos[0];
    expect(v.draftSteps).toHaveLength(2);
    expect(v.draftSteps[1].lane).toBe('违法停车');
  });

  it('ensureConnected:已连接时不重复建连(路由返回无感续上)', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    expect(FakeEventSource.instances).toHaveLength(1);
    store.ensureConnected();
    expect(FakeEventSource.instances).toHaveLength(1);
    // 任务数据仍在(不重置):轮次事件落地后路由往返不丢失
    FakeEventSource.instances[0].emit('round_started', { video: '/data/a.mp4', round_no: 0, kind: 'forward', event_ids: [] });
    store.ensureConnected();
    expect(store.videos[0].rounds).toHaveLength(1);
  });

  it('video_completed 落定摘要;job_completed 终态关流并拉 result', async () => {
    const calls = stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];

    const summary = { video: '/data/a.mp4', match: true, skipped: false, reason: null, gt: [1, 6], rounds: 2 };
    es.emit('video_completed', summary);
    es.emit('video_completed', summary); // 回放重复:覆盖而非异常
    expect(store.videos[0].summary?.match).toBe(true);
    expect(store.completedCount).toBe(1);

    es.emit('job_completed', { stats: { match: 1, quarantine: 0, skipped: 0 } });
    await vi.waitFor(() => expect(store.result).not.toBeNull());
    expect(store.status).toBe('done');
    expect(es.closed).toBe(true);
    expect(calls).toContain('/api/sftforge/jobs/7/result GET');
    expect(store.result?.stats.match).toBe(1);
    expect(store.result?.jsonl).toHaveLength(1);
  });

  it('新字段透传:summary.r0_match 落定;result stats.corrected 保留', async () => {
    const payload = resultPayload({
      stats: { match: 1, quarantine: 0, skipped: 0, corrected: 1 },
      videos: [
        { video: '/data/a.mp4', match: true, skipped: false, reason: null, gt: [1, 6], rounds: 2, r0_match: false },
      ],
    });
    stubFetch(payload);
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];

    es.emit('video_completed', { video: '/data/a.mp4', match: true, skipped: false, reason: null, gt: [1, 6], rounds: 2, r0_match: false });
    expect(store.videos[0].summary?.r0_match).toBe(false);

    es.emit('job_completed', { stats: { match: 1, quarantine: 0, skipped: 0, corrected: 1 } });
    await vi.waitFor(() => expect(store.result).not.toBeNull());
    expect(store.result?.stats.corrected).toBe(1);
    expect(store.result?.videos[0].r0_match).toBe(false);
  });

  it('job_failed:failed 态带错误文案,仍尝试拉 result 取服务端 error', async () => {
    stubFetch(resultPayload({ status: 'failed', error: 'api_unavailable: boom' }));
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];
    es.emit('job_failed', { error: 'api_unavailable: boom' });
    await vi.waitFor(() => expect(store.status).toBe('failed'));
    expect(store.error).toBe('api_unavailable: boom');
    expect(es.closed).toBe(true);
  });

  it('onerror 且 readyState=CLOSED:failed 态提示断连(不假死),仍拉 result 对齐', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];
    es.readyState = FakeEventSource.CLOSED;
    es.onerror!();
    await vi.waitFor(() => expect(store.status).toBe('failed'));
    expect(store.error).toContain('SSE 连接已断开');
    expect(store.error).toContain('结果可能仍在服务端生成');
    expect(es.closed).toBe(true);
  });

  it('onerror 但 readyState 非 CLOSED(浏览器自动重连中):不改变状态', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];
    es.readyState = 0; // CONNECTING
    es.onerror!();
    expect(store.status).toBe('running');
  });

  it('事件 data 非 JSON 时跳过该条,不中断流', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const es = FakeEventSource.instances[0];
    for (const fn of es.listeners.get('round_started') || []) fn({ data: 'not-json{{' });
    expect(store.videos[0].rounds).toHaveLength(0);
    es.emit('round_started', { video: '/data/a.mp4', round_no: 0, kind: 'forward', event_ids: [] });
    expect(store.videos[0].rounds).toHaveLength(1);
  });
});

describe('纠错目标事件名集合', () => {
  it('targetNamesOf 输出 event_ids 对应中文名(段落高亮命中集合)', async () => {
    stubFetch();
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    const names = store.targetNamesOf([1, 6, 99]);
    expect([...names].sort()).toEqual(['交通拥堵', '违法停车']);
  });
});

describe('工作区持久化恢复(restore/clearStored)', () => {
  function storedPayload(over: Partial<ForgeStoredResult> = {}): ForgeStoredResult {
    return {
      ...resultPayload(),
      saved_at: '2026-10-22T10:00',
      rounds: {
        '/data/a.mp4': [
          { round_no: 0, kind: 'forward', event_ids: [], patched: false },
          { round_no: 1, kind: 'correct_missing', event_ids: [6], patched: true },
        ],
      },
      ...over,
    };
  }

  it('idle 空视图:GET store 恢复视频列表(折叠卡)+ 结果面板数据', async () => {
    stubFetch(resultPayload(), storedPayload());
    const store = useSftForgeStore();
    const ok = await store.restore();
    expect(ok).toBe(true);
    expect(store.status).toBe('done');
    expect(store.result?.stats.match).toBe(1);
    expect(store.result?.jsonl).toHaveLength(1);
    // 视频列表:摘要落定(徽标数据源),轮次以最小 trace 回填(done 态折叠卡)
    expect(store.videos).toHaveLength(1);
    const v = store.videos[0];
    expect(v.summary?.match).toBe(true);
    expect(v.rounds).toHaveLength(2);
    expect(v.rounds[0].state).toBe('done');
    // 「第 N 轮修正」标注数据源:patched/event_ids 经回填 trace 可读
    expect(v.rounds[1].trace?.patched).toBe(true);
    expect([...store.targetNamesOf(v.rounds[1].event_ids)]).toEqual(['交通拥堵']);
    // 事件名已加载(结果面板真值/标签展示)
    expect(store.eventNames[6]).toBe('交通拥堵');
  });

  it('缓存含 r0_match 新字段:恢复后摘要与结果透传(徽标数据源)', async () => {
    const stored = storedPayload();
    stored.videos = [{ ...stored.videos[0], r0_match: false }];
    stored.stats = { ...stored.stats, corrected: 1 };
    stubFetch(resultPayload(), stored);
    const store = useSftForgeStore();
    expect(await store.restore()).toBe(true);
    expect(store.videos[0].summary?.r0_match).toBe(false);
    expect(store.result?.videos[0].r0_match).toBe(false);
    expect(store.result?.stats.corrected).toBe(1);
  });

  it('历史缓存缺 r0_match:恢复后字段为空,rounds patched 可供视图回退判定', async () => {
    stubFetch(resultPayload(), storedPayload());
    const store = useSftForgeStore();
    expect(await store.restore()).toBe(true);
    const v = store.videos[0];
    expect(v.summary?.r0_match).toBeUndefined();
    // 回退判定数据源:round_no>0 且 patched 的轮次在回填 trace 中可读
    expect(v.rounds.some((r) => r.round_no > 0 && r.trace?.patched)).toBe(true);
  });

  it('failed 终态缓存:恢复为 failed 并带错误文案', async () => {    stubFetch(
      resultPayload(),
      storedPayload({ status: 'failed', error: 'api_unavailable: boom' }),
    );
    const store = useSftForgeStore();
    expect(await store.restore()).toBe(true);
    expect(store.status).toBe('failed');
    expect(store.error).toBe('api_unavailable: boom');
  });

  it('无缓存(404):静默 no-op,保持 idle', async () => {
    stubFetch();
    const store = useSftForgeStore();
    expect(await store.restore()).toBe(false);
    expect(store.status).toBe('idle');
    expect(store.videos).toHaveLength(0);
    expect(store.result).toBeNull();
  });

  it('任务在跑/已有视频:restore 不覆盖现有状态', async () => {
    const calls = stubFetch(resultPayload(), storedPayload());
    const store = useSftForgeStore();
    await store.start({ dir: '/data' });
    expect(await store.restore()).toBe(false);
    expect(store.status).toBe('running');
    expect(store.videos.map((v) => v.video)).toEqual(['/data/a.mp4', '/data/b.mp4']);
    expect(calls).not.toContain('/api/sftforge/store GET');
  });

  it('clearStored:DELETE 后清空视图回 idle', async () => {
    const calls = stubFetch(resultPayload(), storedPayload());
    const store = useSftForgeStore();
    await store.restore();
    expect(store.status).toBe('done');
    await store.clearStored();
    expect(calls).toContain('/api/sftforge/store DELETE');
    expect(store.status).toBe('idle');
    expect(store.videos).toHaveLength(0);
    expect(store.result).toBeNull();
  });
});
