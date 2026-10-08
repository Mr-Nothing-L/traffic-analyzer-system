// SFT 标注导出 API 封装测试(契约见 web/evidence/sft_export.py):
// GET /api/results/{stem}/sft/export 返回单行 JSONL 文本附件,
// 封装只取文本并透传 ApiError(网络错误 status=0、后端 detail 文案)。
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchSftJsonl } from '../results';
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
