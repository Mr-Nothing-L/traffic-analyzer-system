// text.ts 纯函数测试:ellipsisMiddle 中间省略语义
import { describe, it, expect } from 'vitest';
import { ellipsisMiddle } from '../text';

describe('ellipsisMiddle:短文本原样返回', () => {
  it('远短于 max 的路径不动', () => {
    expect(ellipsisMiddle('/data/高速', 48)).toBe('/data/高速');
  });

  it('长度恰好等于 max 不省略(边界)', () => {
    const s = 'a'.repeat(48);
    expect(ellipsisMiddle(s, 48)).toBe(s);
  });
});

describe('ellipsisMiddle:超长时中间省略', () => {
  it('保留头尾、中间一个 …,长度 ≤ max', () => {
    const s = '/' + 'a'.repeat(60) + '/' + 'b'.repeat(60);
    const r = ellipsisMiddle(s, 48);
    expect(r.length).toBeLessThanOrEqual(48);
    expect(r).toContain('…');
    expect(r.startsWith('/a')).toBe(true);
    expect(r.endsWith('bbbb')).toBe(true);
    expect(r.indexOf('…')).toBe(r.lastIndexOf('…')); // 只有一个省略号
  });

  it('长度 max+1 即触发省略(边界)', () => {
    const r = ellipsisMiddle('a'.repeat(49), 48);
    expect(r.length).toBeLessThanOrEqual(48);
    expect(r).toContain('…');
  });

  it('默认 max=48', () => {
    const s = 'x'.repeat(100);
    expect(ellipsisMiddle(s).length).toBeLessThanOrEqual(48);
  });
});

describe('ellipsisMiddle:尽量在分隔符处断', () => {
  it('头部断在路径段边界(不含被截断的半段)', () => {
    // max=20 → keep=19,头 10 字符内的最后一个 '/' 在 /aaa 后
    const r = ellipsisMiddle('/aaa/bbbbbbbbbbbbbbbb/cc', 20);
    expect(r.startsWith('/aaa…')).toBe(true);
    expect(r.endsWith('/cc')).toBe(true);
  });

  it('尾部尽量从分隔符开始', () => {
    const r = ellipsisMiddle('/very/long/path/with/segments/tail', 24);
    expect(r.length).toBeLessThanOrEqual(24);
    // 尾部应是完整的一段(以 / 开头)
    const tail = r.split('…')[1];
    expect(tail.startsWith('/')).toBe(true);
  });

  it('头让出额度后尾部可保留更长', () => {
    // 头被 '/' 截短,省下的字符给尾
    const r = ellipsisMiddle('/ab/' + 'c'.repeat(40) + '/dddddddd', 30);
    expect(r.endsWith('/dddddddd')).toBe(true);
  });
});

describe('ellipsisMiddle:退化与异常输入', () => {
  it('max < 3 放不下省略号:退化为头部截断', () => {
    expect(ellipsisMiddle('abcdef', 2)).toBe('ab');
  });

  it('无分隔符的超长文本也能省略', () => {
    const r = ellipsisMiddle('a'.repeat(80), 48);
    expect(r.length).toBeLessThanOrEqual(48);
    expect(r).toContain('…');
  });

  it('空字符串原样返回', () => {
    expect(ellipsisMiddle('')).toBe('');
  });
});
