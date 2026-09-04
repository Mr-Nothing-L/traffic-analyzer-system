// 侧栏语义检索状态机测试(stores/rag.ts,契约见 api/rag.ts):
// - search 成功:done/empty 两态,结果整体替换;检索词 trim;
// - 库未建(404)→ missing(引导文案原样入 error),其余错误 → error;
// - 竞态:连续两次检索,先发出的请求晚返回,其过期响应被丢弃(seq 判定);
// - clear/setMode:清空查询、作废在途响应,active 回落,侧栏恢复文件树。
// 建向量库状态机(idle/running/done/error + 2s 轮询,契约 POST /rag/build + GET /rag/build/status):
// - 空闲拉 status 只更新 library 概况,不进终态;running 轮询至终态(last_error→error,partial 保留);
// - 启动遇 409(已在跑)兜底为直接轮询;resetBuild 重置并按新工作区重拉;轮询防多实例并发。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useRagStore } from '../rag';
import type { RagBuildStatus, RagResult, RagSearchResponse } from '../../api/rag';

function result(video_path: string, score = 0.61): RagResult {
  return {
    video_path,
    score,
    events: [2, 8],
    site: 'G3京台高速|K18+470|进京|3',
    start_ts: 1754288341.555,
    duration_s: 6.43,
    has_annotation: true,
    human_edited: false,
    review_status: 'confirmed',
  };
}

function resp(results: RagResult[]): RagSearchResponse {
  return { results, mode: 'text', elapsed_ms: 12 };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 建库 status 响应体工厂(契约 GET /api/rag/build/status)。 */
function buildStatus(over: Partial<RagBuildStatus> = {}): RagBuildStatus {
  return {
    running: false,
    done: 0,
    total: 0,
    failed: 0,
    started_at: null,
    finished_at: null,
    last_error: null,
    partial: false,
    library: null,
    ...over,
  };
}

/** 建库端点 fetch 路由:statusResponses 依次作为 /rag/build/status 的响应;build 固定成功。 */
function stubBuildFetch(
  statusResponses: Array<() => Response>,
  buildResponse: () => Response = () => jsonResponse({ started: true, total: 10 }),
) {
  let statusCalls = 0;
  const fetchMock = vi.fn(async (input: unknown, init?: { method?: string }) => {
    const url = String(input);
    if (url.endsWith('/rag/build/status')) {
      const resp = statusResponses[Math.min(statusCalls, statusResponses.length - 1)];
      statusCalls += 1;
      return resp();
    }
    if (url.endsWith('/rag/build/cancel')) return jsonResponse({ cancelling: true });
    if (url.endsWith('/rag/build') && init?.method === 'POST') return buildResponse();
    throw new Error(`unexpected fetch: ${init?.method ?? 'GET'} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, statusCalls: () => statusCalls };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('rag store 检索状态机', () => {
  it('默认文件名模式,active=false(显示文件树)', () => {
    const rag = useRagStore();
    expect(rag.mode).toBe('name');
    expect(rag.active).toBe(false);
    expect(rag.status).toBe('idle');
  });

  it('检索成功:loading → done,结果整体替换,active=true', async () => {
    const rag = useRagStore();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(resp([result('a.mp4', 0.61), result('b.mp4', 0.42)]))),
    );
    rag.setMode('semantic');
    const p = rag.search('  应急车道 养护车  ');
    expect(rag.status).toBe('loading');
    await p;
    expect(rag.status).toBe('done');
    expect(rag.query).toBe('应急车道 养护车'); // trim
    expect(rag.active).toBe(true);
    expect(rag.results.map((r) => r.video_path)).toEqual(['a.mp4', 'b.mp4']);
  });

  it('空结果集 → empty 态', async () => {
    const rag = useRagStore();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(resp([]))));
    rag.setMode('semantic');
    await rag.search('无此场景');
    expect(rag.status).toBe('empty');
    expect(rag.results).toEqual([]);
  });

  it('库未建(404)→ missing 态,error 为后端引导文案', async () => {
    const rag = useRagStore();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: '检索库不存在,请先运行建库脚本' }, 404)),
    );
    rag.setMode('semantic');
    await rag.search('x');
    expect(rag.status).toBe('missing');
    expect(rag.error).toBe('检索库不存在,请先运行建库脚本');
    expect(rag.results).toEqual([]);
  });

  it('其他后端错误 → error 态,error 带 detail 文案', async () => {
    const rag = useRagStore();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'embedding 服务不可用' }, 500)),
    );
    rag.setMode('semantic');
    await rag.search('x');
    expect(rag.status).toBe('error');
    expect(rag.error).toBe('embedding 服务不可用');
  });

  it('竞态:先发请求晚返回被丢弃,只保留最新查询的结果', async () => {
    const rag = useRagStore();
    const gate: Array<(r: Response) => void> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            gate.push(resolve);
          }),
      ),
    );
    rag.setMode('semantic');
    const p1 = rag.search('第一次');
    const p2 = rag.search('第二次');
    // 第二次先返回:正常生效
    gate[1](jsonResponse(resp([result('new.mp4')])));
    await p2;
    expect(rag.status).toBe('done');
    expect(rag.query).toBe('第二次');
    expect(rag.results.map((r) => r.video_path)).toEqual(['new.mp4']);
    // 第一次晚返回:过期响应丢弃,状态不被覆盖
    gate[0](jsonResponse(resp([result('stale.mp4')])));
    await p1;
    expect(rag.query).toBe('第二次');
    expect(rag.results.map((r) => r.video_path)).toEqual(['new.mp4']);
  });

  it('空词检索等同清空:idle + active=false(恢复文件树)', async () => {
    const rag = useRagStore();
    const fetchMock = vi.fn(async () => jsonResponse(resp([result('a.mp4')])));
    vi.stubGlobal('fetch', fetchMock);
    rag.setMode('semantic');
    await rag.search('   ');
    expect(fetchMock).not.toHaveBeenCalled(); // 空词不发请求
    expect(rag.status).toBe('idle');
    expect(rag.active).toBe(false);
  });

  it('clear 作废在途响应:晚到的结果不再生效', async () => {
    const rag = useRagStore();
    const gate: Array<(r: Response) => void> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            gate.push(resolve);
          }),
      ),
    );
    rag.setMode('semantic');
    const p = rag.search('x');
    rag.clear();
    gate[0](jsonResponse(resp([result('late.mp4')])));
    await p;
    expect(rag.status).toBe('idle');
    expect(rag.results).toEqual([]);
    expect(rag.active).toBe(false);
  });

  it('切模式清空语义状态;切回文件名模式不残留结果', async () => {
    const rag = useRagStore();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(resp([result('a.mp4')]))),
    );
    rag.setMode('semantic');
    await rag.search('x');
    expect(rag.active).toBe(true);
    rag.setMode('name');
    expect(rag.status).toBe('idle');
    expect(rag.results).toEqual([]);
    expect(rag.query).toBe('');
    expect(rag.active).toBe(false);
    rag.setMode('semantic');
    expect(rag.active).toBe(false); // 重新进入需重新提交查询
  });
});

describe('建向量库状态机', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('初始 idle;空闲拉 status 只更新 library 概况,不进终态', async () => {
    const rag = useRagStore();
    expect(rag.buildState).toBe('idle');
    expect(rag.library).toBeNull();
    stubBuildFetch([
      () =>
        jsonResponse(
          buildStatus({ library: { exists: true, count: 1180, built_at: 1756700000 } }),
        ),
    ]);
    await rag.refreshBuildStatus();
    expect(rag.buildState).toBe('idle');
    expect(rag.library).toEqual({ exists: true, count: 1180, built_at: 1756700000 });
  });

  it('status 查询失败静默:状态不变,不抛错', async () => {
    const rag = useRagStore();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'x' }, 500)));
    await rag.refreshBuildStatus();
    expect(rag.buildState).toBe('idle');
    expect(rag.library).toBeNull();
  });

  it('拉到他端在跑的构建:进入 running 并轮询至终态 done', async () => {
    vi.useFakeTimers();
    const rag = useRagStore();
    const lib = { exists: true, count: 42, built_at: 2 };
    stubBuildFetch([
      () => jsonResponse(buildStatus({ running: true, done: 3, total: 10 })),
      () => jsonResponse(buildStatus({ done: 10, total: 10, failed: 1, finished_at: 2, library: lib })),
    ]);
    await rag.refreshBuildStatus();
    expect(rag.buildState).toBe('running');
    expect(rag.buildDone).toBe(3);
    expect(rag.buildTotal).toBe(10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(rag.buildState).toBe('done');
    expect(rag.buildDone).toBe(10);
    expect(rag.buildFailed).toBe(1);
    expect(rag.library).toEqual(lib);
  });

  it('startBuild:POST 启动成功 → running;409(已在跑)兜底为直接轮询', async () => {
    vi.useFakeTimers(); // 防轮询 setTimeout 泄漏到后续测试
    const rag = useRagStore();
    stubBuildFetch([() => jsonResponse(buildStatus({ running: true, done: 0, total: 10 }))]);
    await rag.startBuild();
    expect(rag.buildState).toBe('running');
    rag.stopBuild(); // 停轮询

    stubBuildFetch(
      [() => jsonResponse(buildStatus({ running: true, done: 5, total: 10 }))],
      () => jsonResponse({ detail: 'build already running' }, 409),
    );
    await rag.startBuild(); // 409 不抛错
    expect(rag.buildState).toBe('running');
    expect(rag.buildDone).toBe(5);
    rag.stopBuild();
  });

  it('startBuild:非 409 错误原样抛出(由调用方 message 提示)', async () => {
    const rag = useRagStore();
    stubBuildFetch([], () => jsonResponse({ detail: 'GPU 不可用' }, 500));
    await expect(rag.startBuild()).rejects.toMatchObject({ status: 500, message: 'GPU 不可用' });
    expect(rag.buildState).toBe('idle');
  });

  it('终态带 last_error → error 态,buildError 为原文', async () => {
    vi.useFakeTimers();
    const rag = useRagStore();
    stubBuildFetch([
      () => jsonResponse(buildStatus({ running: true, done: 2, total: 10 })),
      () =>
        jsonResponse(
          buildStatus({ done: 2, total: 10, failed: 1, finished_at: 2, last_error: 'embedding 服务不可用' }),
        ),
    ]);
    await rag.refreshBuildStatus();
    expect(rag.buildState).toBe('running');
    await vi.advanceTimersByTimeAsync(2000);
    expect(rag.buildState).toBe('error');
    expect(rag.buildError).toBe('embedding 服务不可用');
  });

  it('取消后终态 partial:done 态且 buildPartial=true', async () => {
    vi.useFakeTimers();
    const rag = useRagStore();
    stubBuildFetch([
      () => jsonResponse(buildStatus({ running: true, done: 4, total: 10 })),
      () => jsonResponse(buildStatus({ done: 5, total: 10, failed: 0, finished_at: 2, partial: true })),
    ]);
    await rag.refreshBuildStatus();
    await rag.cancelBuild(); // POST cancel(不直接改状态,终态由轮询落定)
    await vi.advanceTimersByTimeAsync(2000);
    expect(rag.buildState).toBe('done');
    expect(rag.buildPartial).toBe(true);
    expect(rag.buildDone).toBe(5);
  });

  it('防多实例并发轮询:running 中重复 refresh 不起第二个轮询循环', async () => {
    vi.useFakeTimers();
    const rag = useRagStore();
    const stub = stubBuildFetch([
      () => jsonResponse(buildStatus({ running: true, done: 1, total: 10 })),
      () => jsonResponse(buildStatus({ running: true, done: 2, total: 10 })),
    ]);
    await rag.refreshBuildStatus(); // status 第 1 次,起轮询
    await rag.refreshBuildStatus(); // status 第 2 次;pollBuild 已在跑,直接返回
    await vi.advanceTimersByTimeAsync(2000); // 仅一个轮询循环各拉一次
    expect(stub.statusCalls()).toBe(3);
    rag.stopBuild();
  });

  it('resetBuild:重置状态机并按新工作区重拉 status(running 中切走轮询退出)', async () => {
    vi.useFakeTimers();
    const rag = useRagStore();
    const stub = stubBuildFetch([
      () => jsonResponse(buildStatus({ running: true, done: 3, total: 10 })),
      () => jsonResponse(buildStatus({ library: { exists: false, count: 0, built_at: null } })),
    ]);
    await rag.refreshBuildStatus();
    expect(rag.buildState).toBe('running');
    await rag.resetBuild(); // 切工作区:先重置,再按新工作区拉(空闲)
    expect(rag.buildState).toBe('idle');
    expect(rag.buildDone).toBe(0);
    expect(rag.library).toEqual({ exists: false, count: 0, built_at: null });
    await vi.advanceTimersByTimeAsync(5000); // 旧轮询已退出,不再拉 status
    expect(stub.statusCalls()).toBe(2);
  });
});
