// model.ts 行为锁定测试：草稿初始化 / description 解析 / 骨架句 / 签名与 dirty
// 用例口径：docs/交通事件数据标注说明文档 v4.5(事件名、action 编号、结论行格式)
import { describe, it, expect } from 'vitest';
import {
  skeleton, sanitizeFileAttrs, parseSftDescription,
  envLines, conclusionLines, buildRevision, signature, initDraft, isDirty,
} from '../model';
import { EVENTS, ev, makeSft, makeDeclSft } from './fixtures';

describe('parseSftDescription:think 按空行分段,「事件名：」前缀定位', () => {
  it('匹配事件段落入 sections,匹配不到的段落原样入 unmatched', () => {
    const desc = '<think>\n机动车违停：段落一。\n\n开头说明段落。\n\n道路施工：施工段落。\n</think>';
    const r = parseSftDescription(desc, EVENTS);
    expect(r.sections).toEqual({ 1: '段落一。', 7: '施工段落。' });
    expect(r.unmatched).toEqual(['开头说明段落。']);
  });

  it('未激活事件的段落丢弃(不进 sections 也不进 unmatched)', () => {
    const desc = '<think>\n抛洒物：路面有塑料袋。\n\n机动车违停：段落。\n</think>';
    const r = parseSftDescription(desc, EVENTS);
    expect(r.sections).toEqual({ 1: '段落。' });
    expect(r.unmatched).toEqual([]);
  });

  it('重复事件段落取首段,后续丢弃', () => {
    const desc = '<think>\n机动车违停：首段。\n\n机动车违停：重复段。\n</think>';
    expect(parseSftDescription(desc, EVENTS).sections).toEqual({ 1: '首段。' });
  });

  it('answer 提取天气/时间/场景,全角半角冒号均可', () => {
    const desc = '<answer>\n天气：晴\n时间: 傍晚\n场景：高速\n最终结论：x\n</answer>';
    expect(parseSftDescription(desc, EVENTS).env).toEqual({ 天气: '晴', 时间: '傍晚', 场景: '高速' });
  });

  it('无 think/answer 标签时全部回退空', () => {
    const r = parseSftDescription(' plain text ', EVENTS);
    expect(r.sections).toEqual({});
    expect(r.unmatched).toEqual([]);
    expect(r.env).toEqual({ 天气: '', 时间: '', 场景: '' });
  });
});

describe('skeleton:按模板拼句,空值从句整体省略', () => {
  it('事件 1 全属性：方向+一侧 / 车道+内 / 停有一辆+车型', () => {
    expect(skeleton(ev(1), { direction: '来向', lane_type: '行车道', vehicle_type: '小型车' }))
      .toBe('来向一侧行车道内停有一辆小型车');
  });

  it('空值从句省略,仅保留固定文字与有值槽位', () => {
    expect(skeleton(ev(1), { direction: '去向' })).toBe('去向一侧停有一辆');
    expect(skeleton(ev(1), {})).toBe('停有一辆');
  });

  it('多选槽位用「、」连接,带 pre 前缀', () => {
    expect(skeleton(ev(7), { direction: '来向', work_elements: ['施工车辆', '交通锥/隔离栏'] }))
      .toBe('来向一侧道路施工,现场有施工车辆、交通锥/隔离栏');
    expect(skeleton(ev(7), { direction: '来向', work_elements: [] })).toBe('来向一侧道路施工');
  });

  it('无模板的事件返回空串', () => {
    // 9 = 正常占位,无骨架模板;10/11 模板随「手动勾选检出插骨干句」需求补齐
    expect(skeleton({ event_id: 9, name_zh: '正常', is_active: false }, {})).toBe('');
    expect(skeleton(ev(11), { direction: '来向', lane_type: '应急车道', vehicle_type: '小型车' }))
      .toBe('来向一侧应急车道出现小型车实线变道行为');
    expect(skeleton(ev(10), { direction: '去向', object_type: '塑料袋/纸张' }))
      .toBe('去向一侧路面有抛洒物(塑料袋/纸张)');
  });
});

describe('sanitizeFileAttrs:只保留当前选项定义内的合法键值', () => {
  it('非法键、非法值、空数组一律丢弃', () => {
    const raw = {
      direction: '来向',
      lane_type: '逆行道',      // 不在封闭枚举内
      vehicle_type: '小型车',
      ghost: 'x',               // 未定义键
    };
    expect(sanitizeFileAttrs(ev(1), raw)).toEqual({ direction: '来向', vehicle_type: '小型车' });
  });

  it('多选组按 options 定义顺序过滤', () => {
    const raw = { work_elements: ['锥桶', '车道封闭', '施工车辆'] }; // 锥桶非法
    expect(sanitizeFileAttrs(ev(7), raw)).toEqual({ work_elements: ['施工车辆', '车道封闭'] });
  });

  it('非对象输入回退空', () => {
    expect(sanitizeFileAttrs(ev(1), null)).toEqual({});
  });
});

describe('initDraft:从 sft_label 初始化草稿', () => {
  it('action 反映射为检出勾选;未激活事件不勾且文本留空', () => {
    const { draft } = initDraft(EVENTS, makeSft({ action: [1, 11] }));
    expect(draft.checks).toEqual({ 1: true, 7: false, 10: false, 11: true });
    expect(draft.texts[10]).toBe('');
  });

  it('无 attr_mentions 的样本整体为纯文本卡：mentions=null,无 skeletons', () => {
    const { draft } = initDraft(EVENTS, makeSft());
    expect(draft.mentions).toBeNull();
    expect(draft.skeletons).toEqual({});
    expect(draft.texts[1]).toBe('来向一侧行车道内停有一辆小型车。');
    expect(draft.env).toEqual({ 天气: '晴', 时间: '白天', 场景: '高速' });
  });

  it('声明通道样本：mentions 清洗(空数组/非法选项丢弃)、attrs 清洗、skeletons 生成', () => {
    const sft = makeDeclSft();
    (sft.attr_mentions as any)[1].lane_type = [];               // 空数组丢弃
    (sft.attr_mentions as any)[1].ghost = ['x'];                // 未定义键丢弃
    (sft.event_attributes as any)[1].lane_type = '逆行道';       // 非法值丢弃
    const { draft } = initDraft(EVENTS, sft);
    expect(draft.mentions).toEqual({ 1: { direction: ['来向'], vehicle_type: ['小型车'] } });
    expect(draft.attrs[1]).toEqual({ direction: '来向', vehicle_type: '小型车' });
    expect(draft.skeletons[1]).toBe('来向一侧停有一辆小型车'); // lane_type 被清洗后无该槽
  });

  it('未激活/无选项组事件的声明提及一律丢弃(保存必遭后端 422)', () => {
    const sft = makeSft({
      attr_mentions: { 10: { direction: ['来向'] }, 11: { direction: ['来向'] }, 1: { direction: ['来向'] } },
    });
    const { draft } = initDraft(EVENTS, sft);
    expect(draft.mentions).toEqual({ 1: { direction: ['来向'] } });
  });
});

describe('buildRevision / signature / isDirty:重建 description 与 action', () => {
  it('未编辑时与磁盘版本逐字节一致(round-trip)', () => {
    const sft = makeSft();
    const { draft } = initDraft(EVENTS, sft);
    const rev = buildRevision(draft, EVENTS);
    expect(rev.description).toBe(sft.description);
    expect(rev.action).toEqual([1]);
  });

  it('编辑文本 → dirty;改回原文 → clean', () => {
    const { draft, savedSig } = initDraft(EVENTS, makeSft());
    expect(isDirty(draft, EVENTS, savedSig)).toBe(false);
    draft.texts[1] += '补充。';
    expect(isDirty(draft, EVENTS, savedSig)).toBe(true);
    draft.texts[1] = '来向一侧行车道内停有一辆小型车。';
    expect(isDirty(draft, EVENTS, savedSig)).toBe(false);
    expect(signature(draft, EVENTS)).toBe(savedSig);
  });

  it('勾选联动结论行与 action;全部取消勾选输出「未检出」结论', () => {
    const { draft } = initDraft(EVENTS, makeSft());
    expect(conclusionLines(EVENTS, draft.checks)).toEqual([
      '最终结论：本视频块检出以下事件。', 'class1: 机动车违停',
    ]);
    draft.checks[1] = false;
    const rev = buildRevision(draft, EVENTS);
    expect(rev.action).toEqual([]);
    expect(rev.description).toContain('最终结论：本视频块未检出任何事件,交通状况正常。');
  });

  it('env 空值回退「未知」;编辑 env 影响签名', () => {
    const { draft, savedSig } = initDraft(EVENTS, makeSft());
    expect(envLines(draft.env)).toEqual(['天气：晴', '时间：白天', '场景：高速']);
    draft.env['天气'] = '  ';
    expect(envLines(draft.env)[0]).toBe('天气：未知');
    expect(isDirty(draft, EVENTS, savedSig)).toBe(true);
  });

  it('unmatched 段原样附加到 think 末尾', () => {
    const sft = makeSft({
      description: '<think>\n机动车违停：段落。\n\n未知前缀：无法归类。\n</think>\n<answer>\n天气：晴\n</answer>',
    });
    const { draft } = initDraft(EVENTS, sft);
    expect(buildRevision(draft, EVENTS).description).toContain('段落。\n\n未知前缀：无法归类。');
  });

  it('event_attributes 仅保留非空键,全空输出 null', () => {
    const { draft } = initDraft(EVENTS, makeSft());
    expect(buildRevision(draft, EVENTS).event_attributes).toBeNull();
    draft.attrs[1] = { direction: '来向', lane_type: '' };
    expect(buildRevision(draft, EVENTS).event_attributes).toEqual({ 1: { direction: '来向' } });
  });

  it('attr_mentions 按当前文本子串过滤：人工删掉的提及保存时移除', () => {
    const { draft } = initDraft(EVENTS, makeDeclSft());
    // 人工编辑把「小型车」从文本中删掉
    draft.texts[1] = '来向一侧行车道内停有一辆车,另有一辆车驶过。';
    const rev = buildRevision(draft, EVENTS);
    expect(rev.attr_mentions).toEqual({ 1: { direction: ['来向'], lane_type: ['行车道'] } });
    // 全部提及都删掉 → 该事件条目省略,整体为 null
    draft.texts[1] = '一辆车停着。';
    expect(buildRevision(draft, EVENTS).attr_mentions).toBeNull();
  });
});
