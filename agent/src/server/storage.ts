/**
 * 会话持久化:node:sqlite(内置,无新增依赖),每 workspace 一个 DB 文件
 * <workspaceDir>/.agent/sessions.db(目录自动创建)。
 *
 * 三张表:
 *   sessions(id, workspace_dir, mode, title, created_at, last_active_at)
 *   entries(session_id, seq, entry_json)   —— 渲染友好的时间线条目(自包含,
 *                                             前端无需懂 kosong Message)
 *   messages(session_id, seq, message_json)—— kosong Message 序列化,供续跑恢复
 *
 * entries/messages 以 (session_id, seq) 为主键,seq 单调递增保证顺序。
 */
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import type { Message } from '#/message';

import type { PermissionMode } from '../permissions/types';
import type { ExecutableToolOutput } from '../tools/contract';

/** 时间线条目:/chat 的 SSE 流累积而成,GET /sessions/{id}/history 原样返回。 */
export type TimelineEntry =
  | {
      kind: 'user';
      text: string;
      /** dataURL 形式的图片附件(无附件时为 [])。 */
      images: string[];
      videoPath?: string;
      /**
       * entry ↔ kosong message 映射:落盘该 user 条目时 session.messages 的
       * 长度,即该条目对应 user message 在 messages 中的下标。recall 按此值
       * 把 messages 截断到「该 user 消息之前」。旧数据(该字段缺失)不可撤回。
       * 注:压缩(手动/自动)会把 messages 整体重写落盘(见 replaceMessages),
       * 压缩区折叠为一条摘要消息后,旧条目的 messageIndex 相对新布局偏大,
       * recall 取 min 兜底即「截到保留区起点(摘要之后)」,语义仍成立。
       */
      messageIndex?: number;
      at: number;
    }
  | {
      kind: 'assistant';
      /** 一轮内聚合的可见文本(无则 '')。 */
      text: string;
      /** 一轮内聚合的思考内容(无则 '')。 */
      think: string;
      at: number;
    }
  | {
      kind: 'tool';
      toolCallId: string;
      name: string;
      arguments: string | null;
      output: ExecutableToolOutput;
      isError: boolean;
      /** 工具附带的结构化备注(如 spawn_subagent 的 {reason,steps} 调试 JSON)。 */
      note?: string;
      at: number;
    }
  | {
      kind: 'approval';
      requestId: string;
      toolName: string;
      approvalRule: string;
      description?: string;
      /** 回执结果;挂起中(历史里异常残留)时缺失。 */
      decision?: 'approved' | 'rejected' | 'cancelled';
      at: number;
    }
  | { kind: 'detection'; data: unknown; at: number };

export interface StoredSession {
  readonly id: string;
  readonly workspaceDir: string;
  readonly mode: PermissionMode;
  readonly title: string;
  readonly createdAt: number;
  readonly lastActiveAt: number;
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  workspace_dir TEXT NOT NULL,
  mode TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  last_active_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  entry_json TEXT NOT NULL,
  PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  message_json TEXT NOT NULL,
  PRIMARY KEY (session_id, seq)
);
`;

export class SessionStorage {
  readonly dbPath: string;
  private readonly db: DatabaseSync;

  constructor(workspaceDir: string) {
    const dir = path.join(workspaceDir, '.agent');
    mkdirSync(dir, { recursive: true });
    this.dbPath = path.join(dir, 'sessions.db');
    this.db = new DatabaseSync(this.dbPath);
    this.db.exec(SCHEMA);
  }

  insertSession(session: StoredSession): void {
    this.db
      .prepare(
        'INSERT INTO sessions (id, workspace_dir, mode, title, created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?)',
      )
      .run(
        session.id,
        session.workspaceDir,
        session.mode,
        session.title,
        session.createdAt,
        session.lastActiveAt,
      );
  }

  getSession(id: string): StoredSession | undefined {
    const row = this.db.prepare('SELECT * FROM sessions WHERE id = ?').get(id);
    return row === undefined ? undefined : rowToSession(row);
  }

  listSessions(): StoredSession[] {
    return this.db
      .prepare('SELECT * FROM sessions ORDER BY created_at ASC')
      .all()
      .map(rowToSession);
  }

  updateTitle(id: string, title: string): void {
    this.db.prepare('UPDATE sessions SET title = ? WHERE id = ?').run(title, id);
  }

  updateMode(id: string, mode: PermissionMode): void {
    this.db.prepare('UPDATE sessions SET mode = ? WHERE id = ?').run(mode, id);
  }

  updateLastActive(id: string, lastActiveAt: number): void {
    this.db.prepare('UPDATE sessions SET last_active_at = ? WHERE id = ?').run(lastActiveAt, id);
  }

  appendEntries(sessionId: string, entries: readonly TimelineEntry[]): void {
    const stmt = this.db.prepare(
      'INSERT INTO entries (session_id, seq, entry_json) VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM entries WHERE session_id = ?), ?)',
    );
    for (const entry of entries) {
      stmt.run(sessionId, sessionId, JSON.stringify(entry));
    }
  }

  loadEntries(sessionId: string): TimelineEntry[] {
    return this.db
      .prepare('SELECT entry_json FROM entries WHERE session_id = ? ORDER BY seq ASC')
      .all(sessionId)
      .map((row) => JSON.parse(String(row.entry_json)) as TimelineEntry);
  }

  appendMessages(sessionId: string, messages: readonly Message[]): void {
    const stmt = this.db.prepare(
      'INSERT INTO messages (session_id, seq, message_json) VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?), ?)',
    );
    for (const message of messages) {
      stmt.run(sessionId, sessionId, JSON.stringify(message));
    }
  }

  loadMessages(sessionId: string): Message[] {
    return this.db
      .prepare('SELECT message_json FROM messages WHERE session_id = ? ORDER BY seq ASC')
      .all(sessionId)
      .map((row) => JSON.parse(String(row.message_json)) as Message);
  }

  /**
   * 截断时间线条目:只保留前 keepCount 条(删除尾部,recall 用)。
   * seq 从 1 起、append 用 MAX(seq)+1、所有删除都是尾删,故 seq 始终连续,
   * 可直接按 seq > keepCount 删。
   */
  truncateEntries(sessionId: string, keepCount: number): void {
    this.db
      .prepare('DELETE FROM entries WHERE session_id = ? AND seq > ?')
      .run(sessionId, keepCount);
  }

  /** 截断 kosong 消息:只保留前 keepCount 条(seq 连续性同 truncateEntries)。 */
  truncateMessages(sessionId: string, keepCount: number): void {
    this.db
      .prepare('DELETE FROM messages WHERE session_id = ? AND seq > ?')
      .run(sessionId, keepCount);
  }

  /** 整体重写某 session 的消息序列(压缩后落盘;entries 显示表不动),
   * 使重启/懒恢复后仍保持压缩态。重写后 seq 从 1 重新连续递增。 */
  replaceMessages(sessionId: string, messages: readonly Message[]): void {
    this.db.prepare('DELETE FROM messages WHERE session_id = ?').run(sessionId);
    this.appendMessages(sessionId, messages);
  }

  /** DELETE 语义:三张表一起清(与 idle 过期只清内存相区别)。 */
  deleteSession(id: string): void {
    this.db.prepare('DELETE FROM entries WHERE session_id = ?').run(id);
    this.db.prepare('DELETE FROM messages WHERE session_id = ?').run(id);
    this.db.prepare('DELETE FROM sessions WHERE id = ?').run(id);
  }

  close(): void {
    this.db.close();
  }
}

function rowToSession(row: Record<string, unknown>): StoredSession {
  return {
    id: String(row.id),
    workspaceDir: String(row.workspace_dir),
    mode: row.mode === 'yolo' || row.mode === 'auto' ? row.mode : 'manual',
    title: String(row.title),
    createdAt: Number(row.created_at),
    lastActiveAt: Number(row.last_active_at),
  };
}
