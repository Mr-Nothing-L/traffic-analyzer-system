// 分析报告删除 API 封装测试(契约见 web/workspace/videos.py):
// 单删走 DELETE /api/workspace/analysis/{stem}(stem 需 URL 编码),
// 批量走 POST /api/workspace/analysis/delete,body 为 {stems};两_wrapper 均
// 透传 ApiError(网络错误 status=0、后端 detail 文案)。
import { describe, it, expect, vi, afterEach } from 'vitest';
import { deleteAnalysisReport, deleteAnalysisReports } from '../analysis';
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

describe('单删报告端点封装', () => {
  it('DELETE /api/workspace/analysis/{stem},stem 经 encodeURIComponent 编码', async () => {
    const calls: Array<{ url: string; method?: string }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string }) => {
        calls.push({ url: String(input), method: init?.method });
        return jsonResponse({ stem: 'v1', ok: true, existed: true });
      }),
    );
    const r = await deleteAnalysisReport('a b');
    expect(calls).toEqual([
      { url: '/api/workspace/analysis/a%20b', method: 'DELETE' },
    ]);
    expect(r).toEqual({ stem: 'v1', ok: true, existed: true });
  });

  it('幂等删除(existed=false)与失败条目均原样透传', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ stem: 'gone', ok: false, existed: true, error: 'busy' })),
    );
    expect(await deleteAnalysisReport('gone')).toEqual({
      stem: 'gone',
      ok: false,
      existed: true,
      error: 'busy',
    });
  });

  it('后端错误抛 ApiError 并带 detail 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'Unknown video stem' }, 404)),
    );
    await expect(deleteAnalysisReport('a..b')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'Unknown video stem',
    } satisfies Partial<ApiError>);
  });
});

describe('批量删报告端点封装', () => {
  it('POST /api/workspace/analysis/delete,body 序列化 stems 列表', async () => {
    let body: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: unknown, init?: { body?: unknown }) => {
        body = init?.body;
        return jsonResponse([
          { stem: 'v1', ok: true, existed: true },
          { stem: 'v2', ok: true, existed: false },
        ]);
      }),
    );
    const r = await deleteAnalysisReports(['v1', 'v2']);
    expect(JSON.parse(String(body))).toEqual({ stems: ['v1', 'v2'] });
    expect(r.map((x) => x.existed)).toEqual([true, false]);
  });

  it('批量整体失败(网络层)抛 ApiError(status=0)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network down');
      }),
    );
    await expect(deleteAnalysisReports(['v1'])).rejects.toMatchObject({ status: 0 });
  });
});
