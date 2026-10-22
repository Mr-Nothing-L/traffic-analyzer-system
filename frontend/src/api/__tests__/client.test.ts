// ApiError 错误分级测试:非应用错误(响应体不是 {detail} JSON,如代理裸文本/HTML)
// 必须带 transport=true;后端应用错误(detail JSON)transport 为假且文案不变。
import { describe, it, expect, vi, afterEach } from 'vitest';
import { apiResponse, ApiError } from '../client';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('apiResponse 错误分级(transport 标志)', () => {
  it('空响应体 400 → transport=true', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 400 })));
    await expect(apiResponse('/results/v1')).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      transport: true,
    } satisfies Partial<ApiError>);
  });

  it('HTML 错误页 502(代理产生)→ transport=true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('<html><body><h1>502 Bad Gateway</h1></body></html>', {
            status: 502,
            headers: { 'Content-Type': 'text/html' },
          }),
      ),
    );
    await expect(apiResponse('/results/v1')).rejects.toMatchObject({
      status: 502,
      transport: true,
    } satisfies Partial<ApiError>);
  });

  it('JSON 但无 detail 字段 → transport=true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: 'Bad Request' }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    );
    await expect(apiResponse('/results/v1')).rejects.toMatchObject({
      status: 400,
      transport: true,
    } satisfies Partial<ApiError>);
  });

  it('应用错误 {detail} 400 → transport 为假且文案不变', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: 'SFT schema 校验失败' }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    );
    await expect(apiResponse('/results/v1')).rejects.toMatchObject({
      status: 400,
      transport: false,
      message: 'SFT schema 校验失败',
    } satisfies Partial<ApiError>);
  });

  it('fetch 抛异常(后端停机)→ status=0 且 transport=true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed');
      }),
    );
    await expect(apiResponse('/results/v1')).rejects.toMatchObject({
      status: 0,
      transport: true,
    } satisfies Partial<ApiError>);
  });
});
