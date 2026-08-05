// quickPick.ts 纯逻辑测试:buildQuickItems 平铺结构 + filterQuickItems 过滤语义
import { describe, it, expect } from 'vitest';
import { buildQuickItems, filterQuickItems, rootNameOf } from '../quickPick';

const ROOTS = [
  { path: '/data/高速', subs: ['Alpha', 'beta', 'Gamma'] },
  { path: '/mnt/backup', subs: ['alpha-2'] },
];

describe('buildQuickItems:每根 = 根本身项 + 子目录项', () => {
  it('平铺结构与根名/标签/路径正确', () => {
    const items = buildQuickItems(ROOTS);
    expect(items).toHaveLength(6);
    expect(items[0]).toMatchObject({
      path: '/data/高速', label: '高速 (根目录)', rootPath: '/data/高速', rootName: '高速', isRoot: true,
    });
    expect(items[1]).toMatchObject({ path: '/data/高速/Alpha', label: 'Alpha', isRoot: false });
    expect(items[5]).toMatchObject({ path: '/mnt/backup/alpha-2', rootName: 'backup' });
  });

  it('根为 / 时根名为 / 且子路径无双斜杠', () => {
    const items = buildQuickItems([{ path: '/', subs: ['etc'] }]);
    expect(rootNameOf('/')).toBe('/');
    expect(items[1].path).toBe('/etc');
  });

  it('空白名单返回空数组', () => {
    expect(buildQuickItems([])).toEqual([]);
  });
});

describe('filterQuickItems:子串匹配,忽略大小写', () => {
  const items = buildQuickItems(ROOTS);

  it('空关键词(含纯空白)原样返回', () => {
    expect(filterQuickItems(items, '')).toHaveLength(6);
    expect(filterQuickItems(items, '   ')).toHaveLength(6);
  });

  it('命中子目录名(大小写不敏感)', () => {
    const r = filterQuickItems(items, 'alpha');
    expect(r.map((x) => x.path)).toEqual(['/data/高速/Alpha', '/mnt/backup/alpha-2']);
  });

  it('命中根名时该根整组保留(根本身 + 全部子目录)', () => {
    const r = filterQuickItems(items, 'BACKUP');
    expect(r.map((x) => x.label)).toEqual(['backup (根目录)', 'alpha-2']);
  });

  it('命中完整路径片段', () => {
    const r = filterQuickItems(items, 'mnt/');
    expect(r.map((x) => x.rootName)).toEqual(['backup', 'backup']);
  });

  it('命中中文根名', () => {
    const r = filterQuickItems(items, '高速');
    expect(r).toHaveLength(4);
  });

  it('无命中返回空', () => {
    expect(filterQuickItems(items, 'zzz')).toEqual([]);
  });
});
