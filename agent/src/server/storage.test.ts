/**
 * SessionStorage 单元测试:PRAGMA user_version 建库标记/版本校验、
 * loadEntriesAfter 续传查询。真实 node:sqlite 临时库,不打模型 API。
 */
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SCHEMA_VERSION, SessionStorage, type TimelineEntry } from './storage';

let workspace: string;

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-storage-test-'));
});

afterEach(() => {
  rmSync(workspace, { recursive: true, force: true });
});

function userVersion(dbPath: string): number {
  const db = new DatabaseSync(dbPath);
  try {
    const row = db.prepare('PRAGMA user_version').get() as Record<string, unknown>;
    return Number(row.user_version);
  } finally {
    db.close();
  }
}

describe('PRAGMA user_version', () => {
  it('建库时写入 user_version = SCHEMA_VERSION', () => {
    const storage = new SessionStorage(workspace);
    expect(userVersion(storage.dbPath)).toBe(SCHEMA_VERSION);
    storage.close();
  });

  it('版本不符的已有库:打开时抛清晰错误', () => {
    const storage = new SessionStorage(workspace);
    const dbPath = storage.dbPath;
    storage.close();
    const db = new DatabaseSync(dbPath);
    db.exec('PRAGMA user_version = 5');
    db.close();

    expect(() => new SessionStorage(workspace)).toThrowError(/schema 版本不兼容.*v5/);
  });

  it('改造前的旧库(user_version=0,已有表):幂等建表并补标版本', () => {
    // 模拟旧库:建表但不写 user_version。
    const dir = path.join(workspace, '.agent');
    mkdirSync(dir, { recursive: true });
    const dbPath = path.join(dir, 'sessions.db');
    const db = new DatabaseSync(dbPath);
    db.exec(
      'CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, workspace_dir TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL DEFAULT \'\', created_at INTEGER NOT NULL, last_active_at INTEGER NOT NULL)',
    );
    db.close();
    expect(userVersion(dbPath)).toBe(0);

    const storage = new SessionStorage(workspace);
    expect(userVersion(dbPath)).toBe(SCHEMA_VERSION);
    storage.insertSession({
      id: 's1',
      workspaceDir: workspace,
      mode: 'manual',
      title: '',
      createdAt: 1,
      lastActiveAt: 1,
    });
    expect(storage.getSession('s1')?.id).toBe('s1');
    storage.close();
  });
});

describe('loadEntriesAfter', () => {
  it('按 fromSeq 过滤并带回落盘 seq', () => {
    const storage = new SessionStorage(workspace);
    const entries: TimelineEntry[] = [
      { kind: 'user', text: '一', images: [], at: 1 },
      { kind: 'assistant', text: '答一', think: '', at: 2 },
      { kind: 'user', text: '二', images: [], at: 3 },
    ];
    storage.appendEntries('s1', entries);

    const all = storage.loadEntriesAfter('s1', 0);
    expect(all.map((r) => r.seq)).toEqual([1, 2, 3]);
    expect(all[0]?.entry).toMatchObject({ kind: 'user', text: '一' });

    expect(storage.loadEntriesAfter('s1', 1).map((r) => r.seq)).toEqual([2, 3]);
    expect(storage.loadEntriesAfter('s1', 3)).toEqual([]);
    storage.close();
  });
});
