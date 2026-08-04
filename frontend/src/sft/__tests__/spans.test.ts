// spans.ts 行为锁定测试：声明提及分词边界 / span 锚定替换 / 骨架前缀换新 / 别名映射
import { describe, it, expect } from 'vitest';
import {
  groupMentionStrings, computeDeclSpans, spansMatchText, replaceDeclaredSpans,
  shiftSpansForEdits, swapSkeletonPrefix, aliasesOf, mapMentionToOption, tokenizeSpans,
} from '../spans';
import type { DeclSpan } from '../types';

describe('groupMentionStrings:扁平数组与嵌套对象统一展开', () => {
  it('扁平数组原样返回;嵌套对象展开全部提及串;空值回退空数组', () => {
    expect(groupMentionStrings(['a', 'b'])).toEqual(['a', 'b']);
    expect(groupMentionStrings({ 甲: ['a'], 乙: ['b', 'c'] })).toEqual(['a', 'b', 'c']);
    expect(groupMentionStrings(null)).toEqual([]);
  });
});

describe('computeDeclSpans:声明串精确子串定位,背景同形词不动', () => {
  it('中文文本基本定位(start/end 为字符下标)', () => {
    const spans = computeDeclSpans({ vehicle_type: ['小型车'] }, '来向一侧行车道内停有一辆小型车');
    expect(spans).toEqual([{ start: 12, end: 15, group: 'vehicle_type', str: '小型车' }]);
  });

  it('中英文混排与标点边界：按字符精确匹配,标点不影响定位', () => {
    const text = 'abc,小型车;测试"货车"end';
    const spans = computeDeclSpans({ vehicle_type: ['小型车', '货车'] }, text);
    expect(spans).toEqual([
      { start: 4, end: 7, group: 'vehicle_type', str: '小型车' },
      { start: 11, end: 13, group: 'vehicle_type', str: '货车' },
    ]);
  });

  it('出现次数多于声明次数时按声明次数封顶,取文本中前 N 处', () => {
    const spans = computeDeclSpans({ vehicle_type: ['小型车'] }, '小型车违停,另一辆小型车驶过');
    expect(spans).toEqual([{ start: 0, end: 3, group: 'vehicle_type', str: '小型车' }]);
  });

  it('同起点长串优先,重叠 span 贪心去除', () => {
    const spans = computeDeclSpans(
      { a: ['工程作业车'], b: ['黄色工程作业车'] },
      '黄色工程作业车占用车道',
    );
    expect(spans).toEqual([{ start: 0, end: 7, group: 'b', str: '黄色工程作业车' }]);
  });

  it('空声明/空串/声明串不在文本中 → 无 span', () => {
    expect(computeDeclSpans({}, '任意文本')).toEqual([]);
    expect(computeDeclSpans({ g: [''] }, '任意文本')).toEqual([]);
    expect(computeDeclSpans({ g: ['不存在'] }, '任意文本')).toEqual([]);
    expect(computeDeclSpans({ g: ['x'] }, '')).toEqual([]);
  });

  it('链式编辑：出现次数多于旧 span 数时按「与旧 span 最近」取前 N 个', () => {
    const prev: DeclSpan[] = [{ start: 12, end: 14, group: 'vehicle_type', str: '货车' }];
    const spans = computeDeclSpans({ vehicle_type: ['货车'] }, '来向一侧行车道内停有一辆货车,货车排队', prev);
    expect(spans).toEqual([{ start: 12, end: 14, group: 'vehicle_type', str: '货车' }]);
  });
});

describe('spansMatchText:缓存校验', () => {
  it('span 与文本吻合返回 true,编辑后不吻合返回 false', () => {
    const spans: DeclSpan[] = [{ start: 0, end: 2, group: 'g', str: '来向' }];
    expect(spansMatchText(spans, '来向一侧')).toBe(true);
    expect(spansMatchText(spans, '去向一侧')).toBe(false);
  });
});

describe('replaceDeclaredSpans:位置锚定替换,同步平移其余 span', () => {
  it('仅目标组 span 替换为新值,背景同形词不动', () => {
    const text = '来向一侧行车道内停有一辆小型车,后方小型车排队';
    const spans: DeclSpan[] = [
      { start: 12, end: 15, group: 'vehicle_type', str: '小型车' },
      { start: 18, end: 21, group: 'vehicle_type', str: '小型车' },
    ];
    const r = replaceDeclaredSpans(text, spans, 'vehicle_type', '货车');
    expect(r.text).toBe('来向一侧行车道内停有一辆货车,后方货车排队');
    expect(r.spans).toEqual([
      { start: 12, end: 14, group: 'vehicle_type', str: '货车' },
      { start: 17, end: 19, group: 'vehicle_type', str: '货车' },
    ]);
  });

  it('其他组 span 只平移不替换', () => {
    const text = '来向一侧停有一辆小型车';
    const spans: DeclSpan[] = [
      { start: 0, end: 2, group: 'direction', str: '来向' },
      { start: 8, end: 11, group: 'vehicle_type', str: '小型车' },
    ];
    const r = replaceDeclaredSpans(text, spans, 'direction', '去向一侧来向'); // 变长
    expect(r.text).toBe('去向一侧来向一侧停有一辆小型车');
    expect(r.spans).toEqual([
      { start: 0, end: 6, group: 'direction', str: '去向一侧来向' },
      { start: 12, end: 15, group: 'vehicle_type', str: '小型车' },
    ]);
  });
});

describe('shiftSpansForEdits / swapSkeletonPrefix:骨架前缀就地换新', () => {
  it('区间之后的 span 平移长度差,重叠的丢弃', () => {
    const spans: DeclSpan[] = [
      { start: 0, end: 2, group: 'a', str: 'x' },
      { start: 3, end: 5, group: 'b', str: 'y' },
      { start: 8, end: 10, group: 'c', str: 'z' },
    ];
    const out = shiftSpansForEdits(spans, [{ start: 3, end: 4, newLen: 6 }]);
    expect(out).toEqual([
      { start: 0, end: 2, group: 'a', str: 'x' },
      { start: 13, end: 15, group: 'c', str: 'z' },
    ]);
  });

  it('骨架换新(车型变短):差异区间后的 span 平移长度差', () => {
    const oldSk = '来向一侧行车道内停有一辆小型车';
    const newSk = '去向一侧行车道内停有一辆货车';
    const text = oldSk + ',余下';
    const spans: DeclSpan[] = [{ start: 16, end: 18, group: 'g', str: '余下' }];
    const r = swapSkeletonPrefix(text, spans, oldSk, newSk);
    expect(r.text).toBe(newSk + ',余下');
    // cs=0,ce=1:edit {0,14,newLen:13},delta=-1,{16,18} → {15,17}
    expect(r.spans).toEqual([{ start: 15, end: 17, group: 'g', str: '余下' }]);
  });

  it('骨架变长：差异区间前的 span 不动,其后的按公式平移(锁定 legacy 口径)', () => {
    const oldSk = '来向一侧停有一辆';
    const newSk = '来向一侧道路施工,现场有施工车辆';
    const text = oldSk + ',余下';
    const spans: DeclSpan[] = [
      { start: 2, end: 4, group: 'a', str: '一侧' },  // 恰在差异区间前(end == edit.start)→ 保留
      { start: 9, end: 11, group: 'g', str: '余下' },
    ];
    const r = swapSkeletonPrefix(text, spans, oldSk, newSk);
    expect(r.text).toBe(newSk + ',余下');
    // legacy 公式：newLen = newSk.length - ce(含公共前缀 cs 的偏移量),
    // span 失锚后由 declaredSpans 的 spansMatchText 校验重算兜底
    expect(r.spans).toEqual([
      { start: 2, end: 4, group: 'a', str: '一侧' },
      { start: 21, end: 23, group: 'g', str: '余下' },
    ]);
  });
});

describe('aliasesOf / mapMentionToOption:旧扁平多选归选项', () => {
  const group = { key: 'vehicle_type', label: '车辆类型', options: ['小型车', '大客车', '货车', '工程车'] };

  it('别名表含自身且按长度降序(最长优先;等长保持定义顺序,稳定排序)', () => {
    expect(aliasesOf('小型车')).toEqual(['小型车', '私家车', '小车', '轿车']);
    expect(aliasesOf('货车')).toEqual(['货车', '卡车']);
  });

  it('提及按最长命中归选项：「小车」→小型车,「客车」→大客车(别名)', () => {
    expect(mapMentionToOption(group, '一辆小车停在路边')).toBe('小型车');
    expect(mapMentionToOption(group, '黄色工程作业车')).toBe('工程车');
    expect(mapMentionToOption(group, '客车')).toBe('大客车');
  });

  it('均不命中返回 null(未映射提及保持现状)', () => {
    expect(mapMentionToOption(group, '自行车')).toBeNull();
  });

  it('映射专用扩展词：「人员」→施工人员(不进公共别名表)', () => {
    const g = { key: 'person_type', label: '人员类型', options: ['行人', '施工人员', '滞留驾乘人员'] };
    expect(mapMentionToOption(g, '无人员行走')).toBe('施工人员');
  });
});

describe('tokenizeSpans:span → 分段数据(供视图层渲染 token)', () => {
  it('span 位置为 token 段,其余为纯文本段', () => {
    const segs = tokenizeSpans(
      [{ start: 4, end: 7, group: 'vehicle_type', str: '小型车' }],
      'abc,小型车;测试',
    );
    expect(segs).toEqual([
      { text: 'abc,', group: null },
      { text: '小型车', group: 'vehicle_type' },
      { text: ';测试', group: null },
    ]);
  });

  it('无 span 时整体为单个纯文本段;空串返回空数组', () => {
    expect(tokenizeSpans([], 'abc')).toEqual([{ text: 'abc', group: null }]);
    expect(tokenizeSpans([], '')).toEqual([]);
  });
});
