/**
 * SessionStorage 单元测试:PRAGMA user_version 建库标记/版本校验/迁移、
 * loadEntriesAfter 续传查询、messages 软遮蔽(replaceMessages/truncateMessages)。
 * 真实 node:sqlite 临时库,不打模型 API。
 */
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { createUserMessage, extractText, type Message } from '../llm/kosong';

import { SCHEMA_VERSION, SessionStorage, type TimelineEntry } from './storage';
import type { DetectionPayload } from '../tools/builtin/submitDetection';

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

describe('detection 条目兼容读取(迁移)', () => {
  it('旧字符串条目读取时一次性还原为对象;非 JSON 原文保持原样;新结构化条目不受影响', () => {
    const storage = new SessionStorage(workspace);
    const payload = {
      video_path: 'demo.mp4',
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: true,
      events: [],
      report_markdown: '# 报告',
    };
    const entries: TimelineEntry[] = [
      // 旧版本落盘形态:note 原文(JSON 字符串)
      { kind: 'detection', data: JSON.stringify(payload), at: 1 },
      // 旧版本解析失败落盘形态:非 JSON 原文
      { kind: 'detection', data: '原始文本结论', at: 2 },
      // 新形态:结构化载荷(payload 通道直接落盘)
      { kind: 'detection', data: payload as DetectionPayload, at: 3 },
    ];
    storage.appendEntries('s1', entries);

    const loaded = storage.loadEntries('s1');
    expect(loaded[0]).toMatchObject({ kind: 'detection', data: payload });
    expect(loaded[1]).toMatchObject({ kind: 'detection', data: '原始文本结论' });
    expect(loaded[2]).toMatchObject({ kind: 'detection', data: payload });

    // events 续传读取路径同样还原
    const after = storage.loadEntriesAfter('s1', 0);
    expect(after[0]?.entry).toMatchObject({ kind: 'detection', data: payload });
    expect(after[1]?.entry).toMatchObject({ kind: 'detection', data: '原始文本结论' });
    storage.close();
  });
});

describe('messages 软遮蔽(shadowed)', () => {
  /** 直读 messages 原始行(含遮蔽行),验证 shadowed 标记与 seq 续接。 */
  function rawMessageRows(
    dbPath: string,
    sessionId: string,
  ): { seq: number; shadowed: number; text: string }[] {
    const db = new DatabaseSync(dbPath);
    try {
      return db
        .prepare('SELECT seq, shadowed, message_json FROM messages WHERE session_id = ? ORDER BY seq ASC')
        .all(sessionId)
        .map((row) => ({
          seq: Number(row.seq),
          shadowed: Number(row.shadowed),
          text: extractText(JSON.parse(String(row.message_json)) as Message),
        }));
    } finally {
      db.close();
    }
  }

  it('replaceMessages 软遮蔽旧行:读取只看新序列,seq 续接,后续 append 不受干扰', () => {
    const storage = new SessionStorage(workspace);
    storage.appendMessages('s1', [
      createUserMessage('一'),
      createUserMessage('二'),
      createUserMessage('三'),
    ]);

    storage.replaceMessages('s1', [createUserMessage('摘要'), createUserMessage('四')]);

    // 读取只看活跃序列
    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['摘要', '四']);
    // 旧行软遮蔽保留;新行 seq 从全局 MAX+1 续接
    expect(rawMessageRows(storage.dbPath, 's1')).toEqual([
      { seq: 1, shadowed: 1, text: '一' },
      { seq: 2, shadowed: 1, text: '二' },
      { seq: 3, shadowed: 1, text: '三' },
      { seq: 4, shadowed: 0, text: '摘要' },
      { seq: 5, shadowed: 0, text: '四' },
    ]);

    // 增量落盘(P1)继续 append:seq 续接,遮蔽行不参与读取
    storage.appendMessages('s1', [createUserMessage('五')]);
    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['摘要', '四', '五']);
    expect(rawMessageRows(storage.dbPath, 's1').at(-1)).toEqual({ seq: 6, shadowed: 0, text: '五' });
    storage.close();
  });

  it('二次压缩:再次 replaceMessages 只遮蔽当前活跃行', () => {
    const storage = new SessionStorage(workspace);
    storage.appendMessages('s1', [createUserMessage('一'), createUserMessage('二')]);
    storage.replaceMessages('s1', [createUserMessage('摘要1')]);
    storage.replaceMessages('s1', [createUserMessage('摘要2')]);

    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['摘要2']);
    expect(rawMessageRows(storage.dbPath, 's1')).toEqual([
      { seq: 1, shadowed: 1, text: '一' },
      { seq: 2, shadowed: 1, text: '二' },
      { seq: 3, shadowed: 1, text: '摘要1' },
      { seq: 4, shadowed: 0, text: '摘要2' },
    ]);
    storage.close();
  });

  it('truncateMessages 仍物理尾删活跃序列,遮蔽行不动', () => {
    const storage = new SessionStorage(workspace);
    storage.appendMessages('s1', [createUserMessage('一'), createUserMessage('二')]);
    storage.replaceMessages('s1', [createUserMessage('摘要'), createUserMessage('四'), createUserMessage('五')]);

    // recall 语义:活跃序列只保留前 2 条,尾部物理删除
    storage.truncateMessages('s1', 2);
    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['摘要', '四']);
    expect(rawMessageRows(storage.dbPath, 's1')).toEqual([
      { seq: 1, shadowed: 1, text: '一' },
      { seq: 2, shadowed: 1, text: '二' },
      { seq: 3, shadowed: 0, text: '摘要' },
      { seq: 4, shadowed: 0, text: '四' },
    ]);

    // keepCount=0:活跃序列全删,遮蔽行仍不动
    storage.truncateMessages('s1', 0);
    expect(storage.loadMessages('s1')).toEqual([]);
    expect(rawMessageRows(storage.dbPath, 's1')).toHaveLength(2);

    // 截断后 append 仍按全局 MAX(seq)+1 续接(剩余行为遮蔽行 seq 1/2)
    storage.appendMessages('s1', [createUserMessage('新')]);
    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['新']);
    expect(rawMessageRows(storage.dbPath, 's1').at(-1)).toEqual({ seq: 3, shadowed: 0, text: '新' });
    storage.close();
  });

  it('老库迁移(v1 无 shadowed 列):加列默认 0、版本升到 2、旧数据可读可压缩', () => {
    // 模拟 v1 旧库:messages 表无 shadowed 列,user_version=1。
    const dir = path.join(workspace, '.agent');
    mkdirSync(dir, { recursive: true });
    const dbPath = path.join(dir, 'sessions.db');
    const db = new DatabaseSync(dbPath);
    db.exec('CREATE TABLE messages (session_id TEXT NOT NULL, seq INTEGER NOT NULL, message_json TEXT NOT NULL, PRIMARY KEY (session_id, seq))');
    db.exec('CREATE TABLE sessions (id TEXT PRIMARY KEY, workspace_dir TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL DEFAULT \'\', created_at INTEGER NOT NULL, last_active_at INTEGER NOT NULL)');
    db.exec('CREATE TABLE entries (session_id TEXT NOT NULL, seq INTEGER NOT NULL, entry_json TEXT NOT NULL, PRIMARY KEY (session_id, seq))');
    db.prepare('INSERT INTO messages (session_id, seq, message_json) VALUES (?, ?, ?)').run(
      's1',
      1,
      JSON.stringify(createUserMessage('旧消息')),
    );
    db.exec('PRAGMA user_version = 1');
    db.close();

    const storage = new SessionStorage(workspace);
    expect(userVersion(dbPath)).toBe(2);
    // 旧行默认 shadowed=0:迁移后照常可读
    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['旧消息']);
    // 迁移后软遮蔽路径正常:旧行被遮蔽,新序列 seq 续接
    storage.replaceMessages('s1', [createUserMessage('摘要')]);
    expect(storage.loadMessages('s1').map((m) => extractText(m))).toEqual(['摘要']);
    expect(rawMessageRows(storage.dbPath, 's1')).toEqual([
      { seq: 1, shadowed: 1, text: '旧消息' },
      { seq: 2, shadowed: 0, text: '摘要' },
    ]);
    storage.close();
  });
});
