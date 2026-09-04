// RAG 语义检索 API 封装测试(契约 POST /api/rag/search):
// - 请求体原样序列化(mode/query/k 等),路径固定 /api/rag/search;
// - 库未建(404,detail 为引导文案)抛 ApiError(404, detail 原文),供前端引导态展示;
// - stemOfVideoPath/fmtTs 结果映射辅助函数口径(basename 去扩展名 / unix 秒→本地时间)。
// 建向量库端点(契约 POST /api/rag/build、GET /api/rag/build/status、POST /api/rag/build/cancel):
// - 方法与路径固定;409(已在跑)抛 ApiError(409, detail),供调用方兜底为直接轮询。
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  cancelRagBuild,
  fmtTs,
  getRagBuildStatus,
  searchRag,
  startRagBuild,
  stemOfVideoPath,
} from '../rag';
import { ApiError } from '../client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('searchRag 端点封装', () => {
  it('POST /api/rag/search,body 原样序列化请求字段', async () => {
    let body: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: unknown, init?: { body?: unknown }) => {
        body = init?.body;
        return jsonResponse({ results: [], mode: 'text', elapsed_ms: 3 });
      }),
    );
    const r = await searchRag({ mode: 'text', query: '应急车道 养护车', k: 10, alpha: 0.6 });
    expect(JSON.parse(String(body))).toEqual({
      mode: 'text',
      query: '应急车道 养护车',
      k: 10,
      alpha: 0.6,
    });
    expect(r).toEqual({ results: [], mode: 'text', elapsed_ms: 3 });
  });

  it('related 模式携带 video 字段', async () => {
    let body: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: unknown, init?: { body?: unknown }) => {
        body = init?.body;
        return jsonResponse({ results: [], mode: 'related', elapsed_ms: 1 });
      }),
    );
    await searchRag({ mode: 'related', video: '02-08_x.mp4', k: 5 });
    expect(JSON.parse(String(body))).toEqual({ mode: 'related', video: '02-08_x.mp4', k: 5 });
  });

  it('库未建(404)抛 ApiError 并带引导文案 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: '检索库不存在,请先运行建库脚本' }, 404)),
    );
    await expect(searchRag({ mode: 'text', query: 'x' })).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: '检索库不存在,请先运行建库脚本',
    } satisfies Partial<ApiError>);
  });
});

describe('建向量库端点封装', () => {
  it('startRagBuild:POST /api/rag/build,返回 started/total', async () => {
    let url = '';
    let method = '';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        url = String(input);
        method = init?.method ?? 'GET';
        return jsonResponse({ started: true, total: 440 });
      }),
    );
    const r = await startRagBuild();
    expect(url).toBe('/api/rag/build');
    expect(method).toBe('POST');
    expect(r).toEqual({ started: true, total: 440 });
  });

  it('startRagBuild:已在跑(409)抛 ApiError(409, detail 原文)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'build already running' }, 409)),
    );
    await expect(startRagBuild()).rejects.toMatchObject({
      name: 'ApiError',
      status: 409,
      message: 'build already running',
    } satisfies Partial<ApiError>);
  });

  it('getRagBuildStatus:GET /api/rag/build/status,原样返回进度与 library 概况', async () => {
    let url = '';
    let method = '';
    const body = {
      running: true,
      done: 120,
      total: 440,
      failed: 2,
      started_at: 1756800000,
      finished_at: null,
      last_error: null,
      partial: false,
      library: { exists: true, count: 1180, built_at: 1756700000 },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        url = String(input);
        method = init?.method ?? 'GET';
        return jsonResponse(body);
      }),
    );
    const r = await getRagBuildStatus();
    expect(url).toBe('/api/rag/build/status');
    expect(method).toBe('GET');
    expect(r).toEqual(body);
  });

  it('cancelRagBuild:POST /api/rag/build/cancel,返回 cancelling', async () => {
    let url = '';
    let method = '';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        url = String(input);
        method = init?.method ?? 'GET';
        return jsonResponse({ cancelling: true });
      }),
    );
    const r = await cancelRagBuild();
    expect(url).toBe('/api/rag/build/cancel');
    expect(method).toBe('POST');
    expect(r).toEqual({ cancelling: true });
  });
});

describe('结果映射辅助', () => {
  it('stemOfVideoPath:相对路径 basename 去扩展名', () => {
    expect(stemOfVideoPath('02-08_Event_257_1754288341555_1.mp4')).toBe(
      '02-08_Event_257_1754288341555_1',
    );
    expect(stemOfVideoPath('sub/dir/a b.mkv')).toBe('a b');
    expect(stemOfVideoPath('plain')).toBe('plain');
  });

  it('fmtTs:unix 秒 → 本地时间串,0/空兜底为 -', () => {
    expect(fmtTs(0)).toBe('-');
    expect(fmtTs(1754288341.555)).toBe(
      new Date(1754288341.555 * 1000).toLocaleString('zh-CN'),
    );
  });
});
