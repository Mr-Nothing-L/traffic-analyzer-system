// SFT Forge 面板纯逻辑测试(utils/sftforge.ts):
// - kind 徽标:三类已知 kind 有中文标签与专属配色类,未知 kind 原样回退;
// - think 段落切分:空行分段、「事件名:」前缀识别、目标事件段落 hit 标记
//   (与 sft/model.parseSftDescription 同口径);无 <think> 回退空数组;
// - 路径末段文件名、JSONL 导出文本(逐行 JSON,末尾换行)。
import { describe, it, expect } from 'vitest';
import {
  buildJsonlText,
  forgeKindCls,
  forgeKindLabel,
  responseSegments,
  thinkParagraphs,
  videoBaseName,
} from '../sftforge';

const NAMES: Record<number, string> = { 1: '违法停车', 6: '交通拥堵' };

describe('轮次 kind 徽标', () => {
  it('已知 kind 有中文标签与专属配色类', () => {
    expect(forgeKindLabel('forward')).toBe('正向盲推');
    expect(forgeKindCls('forward')).toBe('forward');
    expect(forgeKindLabel('correct_missing')).toBe('漏检纠错');
    expect(forgeKindCls('correct_missing')).toBe('missing');
    expect(forgeKindLabel('review_extra')).toBe('误报复核');
    expect(forgeKindCls('review_extra')).toBe('extra');
  });

  it('未知 kind 原样回退', () => {
    expect(forgeKindLabel('weird')).toBe('weird');
    expect(forgeKindCls('weird')).toBe('other');
  });
});

describe('JSON 主响应路径(responseSegments,生产端点实际输出)', () => {
  it('forward JSON:event_thoughts 逐事件成段,present=true 的段落 hit', () => {
    const resp = JSON.stringify({
      weather: '晴',
      time_of_day: '白天',
      scene: '高速公路',
      event_thoughts: [
        { event_id: 1, present: true, thinking: '应急车道内停有一辆小型车。' },
        { event_id: 6, present: false, thinking: '未观察到拥堵迹象。' },
      ],
    });
    const segs = responseSegments(resp, new Set(), NAMES);
    expect(segs).toHaveLength(2);
    expect(segs[0]).toEqual({ label: '违法停车', body: '应急车道内停有一辆小型车。', hit: true });
    expect(segs[1]).toEqual({ label: '交通拥堵', body: '未观察到拥堵迹象。', hit: false });
  });

  it('forward JSON:本轮纠错目标事件(中文名命中)即使 present=false 也 hit', () => {
    const resp = JSON.stringify({
      event_thoughts: [{ event_id: 6, present: true, thinking: '多车道出现拥堵。' }],
    });
    const segs = responseSegments(resp, new Set(['交通拥堵']), NAMES);
    expect(segs[0].hit).toBe(true);
  });

  it('forward JSON 兼容 markdown 代码块包裹与未知事件 id 回退标签', () => {
    const resp =
      '```json\n' +
      JSON.stringify({
        event_thoughts: [{ event_id: 99, present: true, thinking: 'x。' }],
      }) +
      '\n```';
    const segs = responseSegments(resp, new Set(), NAMES);
    expect(segs).toEqual([{ label: '事件99', body: 'x。', hit: true }]);
  });

  it('纠错 JSON({thinking, verdict}):单段整体 hit,verdict 作 label', () => {
    const resp = JSON.stringify({ thinking: '重新观察后确认存在拥堵…', verdict: '确认' });
    const segs = responseSegments(resp, new Set(['交通拥堵']), NAMES);
    expect(segs).toEqual([{ label: '确认', body: '重新观察后确认存在拥堵…', hit: true }]);
  });

  it('纠错 JSON 无 verdict 时 label 为 null', () => {
    const resp = JSON.stringify({ thinking: '补充观察段落…' });
    expect(responseSegments(resp, new Set(), NAMES)[0].label).toBeNull();
  });

  it('非 JSON 回退 <think> 切分;均不可解析返回空数组(展示原文)', () => {
    const think = '<think>\n违法停车：应急车道内停有一辆小型车。\n</think>';
    const segs = responseSegments(think, new Set(['违法停车']), NAMES);
    expect(segs).toEqual([{ label: '违法停车', body: '违法停车：应急车道内停有一辆小型车。', hit: true }]);
    expect(responseSegments('裸文本无标签', new Set(), NAMES)).toEqual([]);
  });
});


describe('think 段落切分与目标段落高亮', () => {
  const text =
    '<think>\n违法停车：应急车道内停有一辆小型车。\n\n交通拥堵：多车道出现拥堵。\n\n未发现其他事件。\n</think>\n<answer>\n最终结论:…\n</answer>';

  it('空行分段并识别「事件名:」前缀', () => {
    const paras = thinkParagraphs(text, new Set());
    expect(paras).toHaveLength(3);
    expect(paras[0].prefix).toBe('违法停车');
    expect(paras[1].prefix).toBe('交通拥堵');
    expect(paras[2].prefix).toBeNull(); // 无前缀段落原样保留
  });

  it('目标事件名命中的段落标 hit(纠错修复段落高亮)', () => {
    const paras = thinkParagraphs(text, new Set(['交通拥堵']));
    expect(paras.map((p) => p.hit)).toEqual([false, true, false]);
  });

  it('无 <think> 段返回空数组(调用方回退展示原文)', () => {
    expect(thinkParagraphs('裸文本无标签', new Set(['违法停车']))).toEqual([]);
    expect(thinkParagraphs('', new Set())).toEqual([]);
  });
});

describe('文件名与 JSONL 导出', () => {
  it('取绝对路径末段为展示名', () => {
    expect(videoBaseName('/data/videos/01-02_Event_1.mp4')).toBe('01-02_Event_1.mp4');
    expect(videoBaseName('solo.mp4')).toBe('solo.mp4');
  });

  it('训练行序列化为逐行 JSON(末尾换行;空数组为空串)', () => {
    const rows = [
      { index: 0, video: '/a.mp4', output: '<think></think>' },
      { index: 1, video: '/b.mp4', output: '<think>x</think>' },
    ];
    expect(buildJsonlText(rows)).toBe(
      '{"index":0,"video":"/a.mp4","output":"<think></think>"}\n' +
        '{"index":1,"video":"/b.mp4","output":"<think>x</think>"}\n',
    );
    expect(buildJsonlText([])).toBe('');
  });
});
