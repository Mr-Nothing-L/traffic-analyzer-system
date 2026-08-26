// 会话按工作区分组测试(参考 kimi-code 侧栏:工作区文件夹为组,组下挂会话):
// 当前工作区组最上(不可折叠);其余工作区各一组(basename 标题 + 完整路径 title,
// 可折叠),组间按组内最近活跃倒序;无 workspaceDir 的旧会话归「未分组」沉底;
// 组内按 lastActiveAt 倒序。
import { describe, it, expect } from 'vitest';
import { groupSessionsByWorkspace, wsBasename } from '../sessionGroups';
import type { AgentSessionInfo } from '../../stores/agentchat';

function s(id: string, workspaceDir: string | undefined, lastActiveAt: number): AgentSessionInfo {
  return { id, workspaceDir, lastActiveAt };
}

describe('wsBasename', () => {
  it('取目录 basename(兼容 / 与 \\,尾部分隔符容错)', () => {
    expect(wsBasename('/data/ws-a')).toBe('ws-a');
    expect(wsBasename('/data/ws-a/')).toBe('ws-a');
    expect(wsBasename('C:\\data\\ws-b')).toBe('ws-b');
    expect(wsBasename('/')).toBe('/');
  });
});

describe('groupSessionsByWorkspace', () => {
  it('归类:当前工作区组最上,其他工作区各一组,未分组沉底', () => {
    const groups = groupSessionsByWorkspace(
      [
        s('a1', '/ws/a', 100),
        s('b1', '/ws/b', 90),
        s('old1', undefined, 80),
        s('b2', '/ws/b', 70),
        s('c1', '/ws/c', 60),
        s('old2', undefined, 50),
      ],
      '/ws/a',
    );
    expect(groups.map((g) => g.key)).toEqual(['own', '/ws/b', '/ws/c', 'ungrouped']);
    expect(groups[0]).toMatchObject({ label: '', collapsible: false });
    expect(groups[0]!.items.map((x) => x.id)).toEqual(['a1']);
    // 其他组:标题 = basename,title = 完整路径,可折叠
    expect(groups[1]).toMatchObject({ label: 'b', title: '/ws/b', collapsible: true });
    expect(groups[1]!.items.map((x) => x.id)).toEqual(['b1', 'b2']); // 组内 lastActiveAt 倒序
    expect(groups[2]).toMatchObject({ label: 'c', title: '/ws/c' });
    expect(groups[3]).toMatchObject({ key: 'ungrouped', label: '未分组', collapsible: true });
    expect(groups[3]!.items.map((x) => x.id)).toEqual(['old1', 'old2']);
  });

  it('组间排序:其他工作区组按组内最近活跃倒序', () => {
    const groups = groupSessionsByWorkspace(
      [s('b1', '/ws/b', 10), s('c1', '/ws/c', 999), s('b2', '/ws/b', 500)],
      '/ws/a',
    );
    expect(groups.map((g) => g.key)).toEqual(['/ws/c', '/ws/b']);
  });

  it('当前工作区无会话时不产出 own 组;无当前工作区(null)时全部按其他/未分组处理', () => {
    expect(groupSessionsByWorkspace([s('b1', '/ws/b', 1)], '/ws/a').map((g) => g.key)).toEqual([
      '/ws/b',
    ]);
    // currentWs 为 null:没有 own 组,带 workspaceDir 的仍按工作区分组
    const groups = groupSessionsByWorkspace([s('b1', '/ws/b', 1), s('o1', undefined, 2)], null);
    expect(groups.map((g) => g.key)).toEqual(['/ws/b', 'ungrouped']);
  });

  it('空列表产出空分组', () => {
    expect(groupSessionsByWorkspace([], '/ws/a')).toEqual([]);
  });
});
