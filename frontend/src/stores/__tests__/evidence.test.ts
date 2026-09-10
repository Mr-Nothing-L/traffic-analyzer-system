// evidence.load 过期响应守卫回归测试(P1-8):
// 快速切换视频时两个 GET 并发在途,旧视频的响应晚到不得覆盖新视频的
// results/draft/乐观锁指纹。
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import type { ResultsResponse } from '../../api/results';

const getResults = vi.hoisted(() => vi.fn());

vi.mock('../../api/results', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/results')>();
  return { ...actual, getResults };
});

function resp(tag: string): ResultsResponse {
  return {
    report_md: `# ${tag}`,
    sft_label: null,
    evidence: null,
    raw_action: null,
    file_sig: null,
    evidence_sig: `sig-${tag}`,
  };
}

beforeEach(() => {
  getResults.mockReset();
  setActivePinia(createPinia());
});

describe('evidence.load 过期响应守卫', () => {
  it('旧请求晚到被丢弃:results/draft 保持新视频的数据', async () => {
    getResults.mockImplementation((stem: string) =>
      stem === 'a' ? new Promise((r) => setTimeout(() => r(resp('a')), 40)) : Promise.resolve(resp('b')),
    );
    const { useEvidenceStore } = await import('../evidence');
    const ev = useEvidenceStore();
    const p1 = ev.load('a');
    const p2 = ev.load('b');
    expect(await p2).toBe(true);
    expect(await p1).toBe(false); // 过期响应被丢弃
    expect(ev.results?.report_md).toBe('# b');
    expect(ev.evidenceSig).toBe('sig-b');
  });

  it('clear() 使在途请求过期:回来后不恢复任何状态', async () => {
    let resolveA!: (v: ResultsResponse) => void;
    getResults.mockImplementation(
      () => new Promise((r) => { resolveA = r; }),
    );
    const { useEvidenceStore } = await import('../evidence');
    const ev = useEvidenceStore();
    const p = ev.load('a');
    ev.clear();
    resolveA(resp('a'));
    expect(await p).toBe(false);
    expect(ev.results).toBeNull();
    expect(ev.evidenceSig).toBeNull();
  });
});
