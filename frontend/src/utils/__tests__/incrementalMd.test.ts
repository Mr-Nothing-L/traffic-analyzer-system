// 增量 markdown 渲染测试:
// - splitMdBlocks:空行切块、偏移正确、代码围栏未闭合时空行不构成边界;
// - IncrementalMd:冻结块只渲染一次(幂等/缓存)、非追加输入整体重置、
//   冻结+尾部拼接与一次性 mdToHtml 渲染等价(块结构一致)。
import { describe, it, expect, vi, afterEach } from 'vitest';
import { splitMdBlocks, IncrementalMd } from '../incrementalMd';
import { mdToHtml } from '../markdown';

const blockTexts = (text: string) => splitMdBlocks(text).map((b) => text.slice(b.start, b.end));

describe('splitMdBlocks(空行切块)', () => {
  it('按空行切块,起止偏移可还原原文块文本', () => {
    const text = '第一段\n\n第二段\n第二段续行\n\n第三段';
    expect(blockTexts(text)).toEqual(['第一段', '第二段\n第二段续行', '第三段']);
  });

  it('连续空行只算一次边界,块内不含空行', () => {
    const text = 'a\n\n\n\nb';
    expect(blockTexts(text)).toEqual(['a', 'b']);
  });

  it('代码围栏未闭合时,围栏内空行不切', () => {
    const text = '前文\n\n```js\nconst a = 1;\n\nconst b = 2;';
    expect(blockTexts(text)).toEqual(['前文', '```js\nconst a = 1;\n\nconst b = 2;']);
  });

  it('围栏闭合后恢复按空行切块', () => {
    const text = '```js\ncode\n\nmore\n```\n\n后文';
    expect(blockTexts(text)).toEqual(['```js\ncode\n\nmore\n```', '后文']);
  });

  it('空文本 / 全空行无块', () => {
    expect(splitMdBlocks('')).toEqual([]);
    expect(splitMdBlocks('\n\n\n')).toEqual([]);
  });
});

describe('IncrementalMd(冻结缓存)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('块数不超过尾部余量时不冻结,全部走 tailHtml', () => {
    const r = new IncrementalMd();
    const out = r.update('只有一段');
    expect(out.frozen).toEqual([]);
    expect(out.tailHtml).toBe(mdToHtml('只有一段'));
  });

  it('冻结末尾 2 块之外的已完成块,key 为源码起始偏移', () => {
    const text = '一\n\n二\n\n三\n\n四';
    const out = new IncrementalMd().update(text);
    expect(out.frozen.map((b) => b.key)).toEqual([0, text.indexOf('二')]);
    expect(out.frozen[0].html).toBe(mdToHtml('一'));
    expect(out.frozen[1].html).toBe(mdToHtml('二'));
    expect(out.tailHtml).toBe(mdToHtml('三\n\n四'));
  });

  it('追加增长时冻结块不重复渲染(命中缓存)', () => {
    const r = new IncrementalMd();
    const t1 = '一\n\n二\n\n三';
    r.update(t1); // 冻结「一」
    const t2 = `${t1}\n\n四`;
    const out2 = r.update(t2); // 「一」已在缓存,「二」新冻结
    expect(out2.frozen.map((b) => b.key)).toEqual([0, t2.indexOf('二')]);
    expect(out2.frozen[0].html).toBe(mdToHtml('一'));
    expect(out2.frozen[1].html).toBe(mdToHtml('二'));
  });

  it('同文本幂等:返回同一结果对象,不重算', () => {
    const r = new IncrementalMd();
    const text = '一\n\n二\n\n三\n\n四';
    const a = r.update(text);
    const b = r.update(text);
    expect(b).toBe(a);
  });

  it('非追加输入(异常输入)整体重置:缓存清空后按新文本重新冻结', () => {
    const r = new IncrementalMd();
    r.update('一\n\n二\n\n三\n\n四');
    const out = r.update('完全不同的新文本\n\n第二块\n\n第三块\n\n第四块');
    expect(out.frozen[0].html).toBe(mdToHtml('完全不同的新文本'));
  });

  it('未闭合围栏整块留在尾部,不冻结;闭合后才冻结', () => {
    const r = new IncrementalMd();
    const t1 = '前文\n\n```js\nline1\n\nline2\n\n后续段';
    const o1 = r.update(t1);
    // 「前文」与未闭合围栏块(吞掉了后续段):共 2 块,均在尾部余量内,无冻结
    expect(o1.frozen).toEqual([]);
    const t2 = '前文\n\n```js\nline1\n\nline2\n```\n\n后续段\n\n再一段\n\n末段';
    const o2 = r.update(t2);
    // 围栏闭合:前文 / 围栏块 / 后续段 / 再一段 / 末段 → 冻结前 3 块
    expect(o2.frozen.map((b) => b.key)).toEqual([
      0,
      t2.indexOf('```js'),
      t2.indexOf('后续段'),
    ]);
  });
});
