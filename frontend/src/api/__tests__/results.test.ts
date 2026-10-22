// SFT 标注导出 API 封装测试(契约见 web/evidence/sft_export.py):
// GET /api/results/{stem}/sft/export 返回单行 JSONL 文本附件,
// 封装只取文本并透传 ApiError(网络错误 status=0、后端 detail 文案)。
import { describe, it, expect, vi, afterEach } from 'vitest';
import { exportSftBatch, fetchSftJsonl } from '../results';
import { ApiError } from '../client';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SFT 导出端点封装', () => {
  it('GET /api/results/{stem}/sft/export,stem 经编码,返回响应文本', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        calls.push(String(input));
        return new Response('{"chunk":"chunk #1"}\n', {
          status: 200,
          headers: { 'Content-Type': 'application/x-ndjson' },
        });
      }),
    );
    const text = await fetchSftJsonl('a b');
    expect(calls).toEqual(['/api/results/a%20b/sft/export']);
    expect(text).toBe('{"chunk":"chunk #1"}\n');
  });

  it('无标注(404)抛 ApiError 并带 detail 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: 'SFT file not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    await expect(fetchSftJsonl('v1')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'SFT file not found',
    } satisfies Partial<ApiError>);
  });

  it('网络层失败抛 ApiError(status=0)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network down');
      }),
    );
    await expect(fetchSftJsonl('v1')).rejects.toMatchObject({ status: 0 });
  });
});

// 批量导出(POST /api/workspace/sft/export):合并 JSONL + 计数响应头
describe('批量导出 SFT 标注封装', () => {
  it('POST stems,返回文本与 X-Export-Total/X-Export-Skipped 计数', async () => {
    const calls: Array<{ url: string; method?: string; body?: string }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: RequestInit) => {
        calls.push({ url: String(input), method: init?.method, body: init?.body as string });
        return new Response('{"chunk":"chunk #1"}\n{"chunk":"chunk #1"}\n', {
          status: 200,
          headers: {
            'Content-Type': 'application/x-ndjson',
            'X-Export-Total': '2',
            'X-Export-Skipped': '1',
          },
        });
      }),
    );
    const r = await exportSftBatch(['v1', 'v2', 'bad']);
    expect(calls).toEqual([
      {
        url: '/api/workspace/sft/export',
        method: 'POST',
        body: JSON.stringify({ stems: ['v1', 'v2', 'bad'] }),
      },
    ]);
    expect(r.total).toBe(2);
    expect(r.skipped).toBe(1);
    expect(r.text.split('\n').filter(Boolean).length).toBe(2);
  });

  it('超上限 400 抛 ApiError 并带 detail 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: 'too many stems: 1001 > 1000' }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    );
    await expect(exportSftBatch(Array.from({ length: 1001 }, () => 'v'))).rejects.toMatchObject(
      { status: 400, message: 'too many stems: 1001 > 1000' } satisfies Partial<ApiError>,
    );
  });
});
