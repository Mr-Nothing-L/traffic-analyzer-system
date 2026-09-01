/**
 * 自定义会话标题单元测试:SessionManager.setTitle 语义(自定义标记 /
 * 空串恢复自动派生)、自动派生不覆盖自定义标题、recall 保留自定义标题、
 * 落盘重开后 titleCustom 保持;不打真实模型 API(不经过 HTTP)。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SessionManager } from './session';
import type { TimelineEntry } from './storage';

const userEntry = (text: string, messageIndex = 0): TimelineEntry => ({
  kind: 'user',
  text,
  images: [],
  messageIndex,
  at: Date.now(),
});

describe('SessionManager 自定义标题', () => {
  let workspace: string;
  let manager: SessionManager | undefined;

  beforeEach(() => {
    workspace = mkdtempSync(path.join(tmpdir(), 'session-title-'));
  });
  afterEach(() => {
    manager?.close();
    rmSync(workspace, { recursive: true, force: true });
  });

  it('首个 user 条目自动派生标题(截 30 字)', () => {
    manager = new SessionManager();
    const s = manager.create({ workspaceDir: workspace, mode: 'yolo' });
    manager.appendEntries(s.id, [userEntry('分析这个视频的交通事件')]);
    expect(manager.get(s.id)?.title).toBe('分析这个视频的交通事件');
    expect(manager.get(s.id)?.titleCustom).toBe(false);
  });

  it('自定义标题后自动派生不再覆盖;重开库后 titleCustom 保持', () => {
    manager = new SessionManager();
    const s = manager.create({ workspaceDir: workspace, mode: 'yolo' });
    expect(manager.setTitle(s.id, '  02-08 倒车复检  ')).toBe(true);
    manager.appendEntries(s.id, [userEntry('分析视频')]);
    expect(manager.get(s.id)?.title).toBe('02-08 倒车复检');
    manager.close();

    // 重开:新 SessionManager 打开同一 workspace 的 sessions.db
    manager = new SessionManager({ workspaces: [workspace] });
    const restored = manager.get(s.id);
    expect(restored?.title).toBe('02-08 倒车复检');
    expect(restored?.titleCustom).toBe(true);
    // 重开后追加条目仍不覆盖自定义标题
    manager.appendEntries(s.id, [userEntry('再分析一次')]);
    expect(manager.get(s.id)?.title).toBe('02-08 倒车复检');
  });

  it('setTitle 空串:恢复自动派生(下一条 user 条目重算)', () => {
    manager = new SessionManager();
    const s = manager.create({ workspaceDir: workspace, mode: 'yolo' });
    manager.setTitle(s.id, '自定义名');
    expect(manager.setTitle(s.id, '   ')).toBe(true);
    expect(manager.get(s.id)?.title).toBe('');
    expect(manager.get(s.id)?.titleCustom).toBe(false);
    manager.appendEntries(s.id, [userEntry('重新分析')]);
    expect(manager.get(s.id)?.title).toBe('重新分析');
  });

  it('recall 到 0:自定义标题保留,自动派生标题才清空', () => {
    manager = new SessionManager();
    const custom = manager.create({ workspaceDir: workspace, mode: 'yolo' });
    manager.appendEntries(custom.id, [userEntry('分析视频')]);
    manager.setTitle(custom.id, '我的复检');
    expect(manager.recall(custom.id, 0)).toBe('ok');
    expect(manager.get(custom.id)?.title).toBe('我的复检');

    const auto = manager.create({ workspaceDir: workspace, mode: 'yolo' });
    manager.appendEntries(auto.id, [userEntry('分析视频')]);
    expect(manager.get(auto.id)?.title).toBe('分析视频');
    expect(manager.recall(auto.id, 0)).toBe('ok');
    expect(manager.get(auto.id)?.title).toBe('');
  });

  it('未知 session:setTitle 返回 false', () => {
    manager = new SessionManager();
    expect(manager.setTitle('ghost', 'x')).toBe(false);
  });
});
