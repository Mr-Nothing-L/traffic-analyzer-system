// buildPutPayload 行为锁定测试：与 legacy sft.js saveSft 的 PUT body 逐字段对照
// legacy: payload = Object.assign({}, state.results.sft_label, buildSftRevision())
//         if (baseSig) payload.base_sig = baseSig  (GET 响应的 file_sig 作 base_sig 上送)
import { describe, it, expect } from 'vitest';
import { initDraft, buildRevision, buildPutPayload } from '../model';
import { EVENTS, makeSft } from './fixtures';

describe('buildPutPayload:保存载荷构造', () => {
  it('只覆盖 description/action/event_attributes/attr_mentions,其余字段原样带回', () => {
    const sft = makeSft();
    const { draft } = initDraft(EVENTS, sft);
    draft.texts[1] = '改过的文本。';
    draft.checks[1] = false;
    const payload = buildPutPayload(draft, EVENTS, sft);
    expect(payload.chunk).toBe(sft.chunk);
    expect(payload.idx).toBe(sft.idx);
    expect(payload.start_timestamp).toBe(sft.start_timestamp);
    expect(payload.end_timestamp).toBe(sft.end_timestamp);
    expect(payload.chunk_name).toBe(sft.chunk_name);
    expect(payload.description).toBe(buildRevision(draft, EVENTS).description);
    expect(payload.action).toEqual([]);
    expect(payload.event_attributes).toBeNull();
    expect(payload.attr_mentions).toBeNull();
  });

  it('携带 base_sig 乐观锁字段;无 baseSig 时不带该字段', () => {
    const sft = makeSft();
    const { draft } = initDraft(EVENTS, sft);
    expect(buildPutPayload(draft, EVENTS, sft, 'sig-abc').base_sig).toBe('sig-abc');
    expect('base_sig' in buildPutPayload(draft, EVENTS, sft)).toBe(false);
  });

  it('字段集与 legacy Object.assign 结果一致(含 revision 补出的 null 字段)', () => {
    const sft = makeSft();
    const { draft } = initDraft(EVENTS, sft);
    const payload = buildPutPayload(draft, EVENTS, sft, 'sig-abc');
    expect(Object.keys(payload).sort()).toEqual(
      [...Object.keys(sft), 'event_attributes', 'attr_mentions', 'base_sig'].sort(),
    );
  });

  it('不改动入参 sft_label(纯函数,数据进数据出)', () => {
    const sft = makeSft();
    const snapshot = JSON.stringify(sft);
    const { draft } = initDraft(EVENTS, sft);
    draft.checks[7] = true;
    buildPutPayload(draft, EVENTS, sft);
    expect(JSON.stringify(sft)).toBe(snapshot);
  });
});
