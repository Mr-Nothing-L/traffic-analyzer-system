// chips.ts 行为锁定测试：chips 三层联动(选中态 → 文本同步 → 草稿)
// 优先级：1) 声明提及 span 锚定替换 2) 骨架前缀就地换新;取消选中不删正文词
import { describe, it, expect } from 'vitest';
import { initDraft, buildRevision, isDirty, evOptions } from '../model';
import { applyChipChange } from '../chips';
import { EVENTS, ev, makeSft, makeDeclSft, makeMultiNestSft, makeMultiFlatSft } from './fixtures';
import type { EventDef } from '../types';

function groupOf(e: EventDef, key: string) {
  const g = evOptions(e).find(x => x.key === key);
  if (!g) throw new Error('no group ' + key);
  return g;
}

describe('单选换值：声明提及 span 锚定替换 + 骨架前缀就地换新', () => {
  it('换车型：声明 span 替换为新值、骨架同步、背景同形词不动、decl 同步', () => {
    const { draft } = initDraft(EVENTS, makeDeclSft());
    applyChipChange(draft, EVENTS, ev(1), groupOf(ev(1), 'vehicle_type'), '货车');
    expect(draft.attrs[1].vehicle_type).toBe('货车');
    expect(draft.texts[1]).toBe('来向一侧行车道内停有一辆货车,另有一辆小型车驶过。');
    expect(draft.mentions![1].vehicle_type).toEqual(['货车']);
    expect(draft.skeletons[1]).toBe('来向一侧行车道内停有一辆货车');
  });

  it('取消选中(再点当前值):骨架前缀同步去掉该槽,正文其余词保留', () => {
    const { draft } = initDraft(EVENTS, makeDeclSft());
    const g = groupOf(ev(1), 'vehicle_type');
    applyChipChange(draft, EVENTS, ev(1), g, '货车');
    applyChipChange(draft, EVENTS, ev(1), g, '货车'); // 再点取消
    expect(draft.attrs[1].vehicle_type).toBe('');
    expect(draft.texts[1]).toBe('来向一侧行车道内停有一辆,另有一辆小型车驶过。');
    expect(draft.skeletons[1]).toBe('来向一侧行车道内停有一辆');
  });

  it('chip 变更使草稿变 dirty', () => {
    const { draft, savedSig } = initDraft(EVENTS, makeDeclSft());
    expect(isDirty(draft, EVENTS, savedSig)).toBe(false);
    applyChipChange(draft, EVENTS, ev(1), groupOf(ev(1), 'vehicle_type'), '货车');
    expect(isDirty(draft, EVENTS, savedSig)).toBe(true);
    const rev = buildRevision(draft, EVENTS);
    expect(rev.event_attributes).toEqual({
      1: { direction: '来向', lane_type: '行车道', vehicle_type: '货车' },
    });
  });

  it('连续换值(链式):第二次替换仍锚定声明 span,不误伤背景', () => {
    const { draft } = initDraft(EVENTS, makeDeclSft());
    const g = groupOf(ev(1), 'vehicle_type');
    applyChipChange(draft, EVENTS, ev(1), g, '货车');
    applyChipChange(draft, EVENTS, ev(1), g, '大客车');
    expect(draft.texts[1]).toBe('来向一侧行车道内停有一辆大客车,另有一辆小型车驶过。');
    expect(draft.mentions![1].vehicle_type).toEqual(['大客车']);
  });
});

describe('多选组(新格式嵌套 per-option 绑定):按选项键增删', () => {
  it('取消选中：该选项提及移出 decl 暂存 mentionsOff,正文词保留', () => {
    const { draft } = initDraft(EVENTS, makeMultiNestSft());
    applyChipChange(draft, EVENTS, ev(7), groupOf(ev(7), 'work_elements'), '交通锥/隔离栏');
    expect(draft.attrs[7].work_elements).toEqual(['施工车辆']);
    expect(draft.texts[7]).toBe('来向一侧道路施工,现场有工程车和锥桶。'); // 不删词
    expect(draft.mentions![7].work_elements).toEqual({ 施工车辆: ['工程车'] });
    expect(draft.mentionsOff![7].work_elements).toEqual({ '交通锥/隔离栏': ['锥桶'] });
  });

  it('重新选中：暂存中仍在文本里的提及恢复声明与标注', () => {
    const { draft } = initDraft(EVENTS, makeMultiNestSft());
    const g = groupOf(ev(7), 'work_elements');
    applyChipChange(draft, EVENTS, ev(7), g, '交通锥/隔离栏');
    applyChipChange(draft, EVENTS, ev(7), g, '交通锥/隔离栏'); // 重新选中
    expect(draft.attrs[7].work_elements).toEqual(['施工车辆', '交通锥/隔离栏']); // options 定义顺序
    expect(draft.mentions![7].work_elements).toEqual({
      施工车辆: ['工程车'], '交通锥/隔离栏': ['锥桶'],
    });
    expect(draft.mentionsOff![7].work_elements).toEqual({});
  });

  it('重新选中时提及已不在文本中 → 不恢复', () => {
    const { draft } = initDraft(EVENTS, makeMultiNestSft());
    const g = groupOf(ev(7), 'work_elements');
    applyChipChange(draft, EVENTS, ev(7), g, '交通锥/隔离栏');
    draft.texts[7] = '来向一侧道路施工,现场有工程车。'; // 人工删掉「锥桶」
    applyChipChange(draft, EVENTS, ev(7), g, '交通锥/隔离栏');
    expect(draft.mentions![7].work_elements).toEqual({ 施工车辆: ['工程车'] });
  });
});

describe('多选组(旧扁平数组):按「声明提及 → 选项」别名映射归选项', () => {
  it('取消选中「施工车辆」:别名命中「工程车」的提及移出,其余保留', () => {
    const { draft } = initDraft(EVENTS, makeMultiFlatSft());
    applyChipChange(draft, EVENTS, ev(7), groupOf(ev(7), 'work_elements'), '施工车辆');
    expect(draft.attrs[7].work_elements).toEqual(['交通锥/隔离栏']);
    expect(draft.mentions![7].work_elements).toEqual(['锥桶']);
    expect(draft.mentionsOff![7].work_elements).toEqual(['工程车']);
  });

  it('重新选中：暂存提及仍在文本中则加回 decl', () => {
    const { draft } = initDraft(EVENTS, makeMultiFlatSft());
    const g = groupOf(ev(7), 'work_elements');
    applyChipChange(draft, EVENTS, ev(7), g, '施工车辆');
    applyChipChange(draft, EVENTS, ev(7), g, '施工车辆');
    expect(draft.mentions![7].work_elements).toEqual(['锥桶', '工程车']);
    expect(draft.mentionsOff![7].work_elements).toEqual([]);
  });
});

describe('纯文本卡(无 attr_mentions):chip 只改 attrs,不动文本', () => {
  it('无声明提及样本选中 chip:attrs 更新,文本原样', () => {
    const { draft } = initDraft(EVENTS, makeSft());
    applyChipChange(draft, EVENTS, ev(1), groupOf(ev(1), 'direction'), '去向');
    expect(draft.attrs[1].direction).toBe('去向');
    expect(draft.texts[1]).toBe('来向一侧行车道内停有一辆小型车。');
  });
});
