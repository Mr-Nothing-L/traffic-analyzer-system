// SFT Forge API 封装测试(契约见 traffic_analyzer/web/sftforge/):
// - 创建任务 POST /api/sftforge/jobs,body 透传 dir/videos/max_rounds;
// - 结果 GET /api/sftforge/jobs/{id}/result;
// - SSE 地址带 /api 前缀;错误透传 ApiError(detail 文案)。
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  createForgeJob,
  fetchForgeResult,
  jobEventsUrl,
} from '../sftforge';
import type { ForgeResultPayload } from '../sftforge';
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

describe('创建 SFT Forge 任务', () => {
  it('POST /api/sftforge/jobs,body 序列化 dir + max_rounds', async () => {
    let call: { url: string; method?: string; body?: unknown } | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown, init?: { method?: string; body?: unknown }) => {
        call = { url: String(input), method: init?.method, body: init?.body };
        return jsonResponse({ job_id: 7, videos: ['/data/a.mp4'] });
      }),
    );
    const r = await createForgeJob({ dir: '/data', max_rounds: 3 });
    expect(call!.url).toBe('/api/sftforge/jobs');
    expect(call!.method).toBe('POST');
    expect(JSON.parse(String(call!.body))).toEqual({ dir: '/data', max_rounds: 3 });
    expect(r).toEqual({ job_id: 7, videos: ['/data/a.mp4'] });
  });

  it('videos 列表模式原样透传', async () => {
    let body: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: unknown, init?: { body?: unknown }) => {
        body = init?.body;
        return jsonResponse({ job_id: 8, videos: ['/data/a.mp4', '/data/b.mp4'] });
      }),
    );
    await createForgeJob({ videos: ['/data/a.mp4', '/data/b.mp4'] });
    expect(JSON.parse(String(body))).toEqual({ videos: ['/data/a.mp4', '/data/b.mp4'] });
  });

  it('后端 400(目录不存在)抛 ApiError 并带 detail 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: '目录不存在: /nope' }, 400)),
    );
    await expect(createForgeJob({ dir: '/nope' })).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      message: '目录不存在: /nope',
    } satisfies Partial<ApiError>);
  });
});

describe('任务结果与 SSE 地址', () => {
  it('GET /api/sftforge/jobs/{id}/result 返回完整结果载荷', async () => {
    const payload: ForgeResultPayload = {
      job_id: 7,
      status: 'done',
      error: null,
      stats: { match: 1, quarantine: 1, skipped: 0 },
      videos: [
        { video: '/data/a.mp4', match: true, skipped: false, reason: null, gt: [1, 2], rounds: 2 },
      ],
      jsonl: [{ index: 0, video: '/data/a.mp4', output: '<think></think>' }],
      quarantine: [{ video: '/data/b.mp4', reason: 'max_rounds_exhausted', gt: [4] }],
    };
    let url = '';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: unknown) => {
        url = String(input);
        return jsonResponse(payload);
      }),
    );
    const r = await fetchForgeResult(7);
    expect(url).toBe('/api/sftforge/jobs/7/result');
    expect(r).toEqual(payload);
  });

  it('未知任务 404 抛 ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'Unknown sftforge job id: 99' }, 404)),
    );
    await expect(fetchForgeResult(99)).rejects.toMatchObject({ status: 404 });
  });

  it('SSE 地址带 /api 前缀(EventSource 直连)', () => {
    expect(jobEventsUrl(7)).toBe('/api/sftforge/jobs/7/events');
  });
});
