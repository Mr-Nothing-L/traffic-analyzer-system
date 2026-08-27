/**
 * 会话持久化:node:sqlite(内置,无新增依赖),每 workspace 一个 DB 文件
 * <workspaceDir>/.agent/sessions.db(目录自动创建)。
 *
 * 三张表:
 *   sessions(id, workspace_dir, mode, title, created_at, last_active_at)
 *   entries(session_id, seq, entry_json)   —— 渲染友好的时间线条目(自包含,
 *                                             前端无需懂 kosong Message)
 *   messages(session_id, seq, message_json, shadowed)
 *                                       —— kosong Message 序列化,供续跑恢复;
 *                                          shadowed=1 为压缩回写时软遮蔽的旧行,
 *                                          读取只看 shadowed=0
 *
 * entries/messages 以 (session_id, seq) 为主键,seq 单调递增保证顺序
 * (messages 的 seq 含遮蔽行也连续,append 始终 MAX(seq)+1 续接)。
 */
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import type { Message } from '../llm/kosong';

import type { PermissionMode } from '../permissions/types';
import type { DetectionPayload } from '../tools/builtin/submitDetection';
import type { ExecutableToolOutput, ToolAccesses } from '../tools/contract';
import type { PreviewContent } from './approvalBridge';

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
      /** 成功结果的结构化附件(如 submit_detection 的检测载荷),原样落盘。 */
      payload?: unknown;
      at: number;
    }
  | {
      kind: 'approval';
      requestId: string;
      toolName: string;
      approvalRule: string;
      description?: string;
      /** 执行声明的资源访问(与 ApprovalRequestEvent 一致)。 */
      accesses?: ToolAccesses;
      /** 工具调用参数的内容预览。 */
      preview?: PreviewContent;
      /** 回执结果;挂起中(历史里异常残留)时缺失。 */
      decision?: 'approved' | 'rejected' | 'cancelled';
      /**
       * 仅在运行时恢复路径附加:该未决审批仍存在于服务端未决集合,
       * 前端应渲染为可操作面板。
       */
      pending?: boolean;
      at: number;
    }
  | {
      kind: 'detection';
      /**
       * submit_detection 的结构化检测载荷,由 stop_turn 结果的 payload 直接
       * 落盘。仅迁移兼容场景为字符串:payload 通道落地之前的旧版本在
       * JSON.parse(note) 失败时会把 note 原文存进 data,读取路径已做一次性
       * 还原(见 reviveLegacyDetectionEntry)。
       */
      data: DetectionPayload | string;
      at: number;
    };

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
  shadowed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, seq)
);
`;

/** 当前 schema 版本:建库时写入 PRAGMA user_version,打开已有库时校验/迁移。 */
export const SCHEMA_VERSION = 2;

/**
 * 迁移兼容(仅读取路径):payload 通道落地前,旧版本在 note 不是合法 JSON 时
 * 会把原文字符串存进 detection 条目的 data;这里一次性尝试还原成对象,还原
 * 不了保持原样(前端按 detection-raw 降级渲染)。新条目一律是结构化载荷。
 */
function reviveLegacyDetectionEntry(entry: TimelineEntry): TimelineEntry {
  if (entry.kind !== 'detection' || typeof entry.data !== 'string') return entry;
  try {
    return { ...entry, data: JSON.parse(entry.data) as DetectionPayload };
  } catch {
    return entry;
  }
}

export class SessionStorage {
  readonly dbPath: string;
  private readonly db: DatabaseSync;

  constructor(workspaceDir: string) {
    const dir = path.join(workspaceDir, '.agent');
    mkdirSync(dir, { recursive: true });
    this.dbPath = path.join(dir, 'sessions.db');
    this.db = new DatabaseSync(this.dbPath);
    const versionRow = this.db.prepare('PRAGMA user_version').get() as
      | Record<string, unknown>
      | undefined;
    const version = Number(versionRow?.user_version ?? 0);
    if (version === 0) {
      // 新库:建表并标记版本。例外:更老版本创建的库可能已有表但从未写过
      // user_version——SCHEMA 的 IF NOT EXISTS 不会给已有 messages 表补列,
      // 按 v1 语义补 shadowed。
      this.db.exec(SCHEMA);
      const hasShadowed = this.db
        .prepare("SELECT 1 AS found FROM pragma_table_info('messages') WHERE name = 'shadowed'")
        .get();
      if (hasShadowed === undefined) {
        this.db.exec('ALTER TABLE messages ADD COLUMN shadowed INTEGER NOT NULL DEFAULT 0');
      }
      this.db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
    } else if (version < SCHEMA_VERSION) {
      this.migrate(version);
    } else if (version !== SCHEMA_VERSION) {
      this.db.close();
      throw new Error(
        `sessions.db schema 版本不兼容:库为 v${version},当前代码支持 v${SCHEMA_VERSION};` +
          `请用匹配版本的 agent server 打开或迁移该库(${this.dbPath})`,
      );
    }
  }

  /** 逐版本迁移旧库到当前 SCHEMA_VERSION。 */
  private migrate(from: number): void {
    if (from < 2) {
      // v1 → v2:messages 表加 shadowed 列(软遮蔽,见 replaceMessages);
      // 已有行默认 0(全部活跃),语义与旧库一致。
      this.db.exec('ALTER TABLE messages ADD COLUMN shadowed INTEGER NOT NULL DEFAULT 0');
    }
    this.db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
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

  appendEntries(sessionId: string, entries: readonly TimelineEntry[]): number[] {
    const seqs: number[] = [];
    const stmt = this.db.prepare(
      'INSERT INTO entries (session_id, seq, entry_json) VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM entries WHERE session_id = ?), ?)',
    );
    for (const entry of entries) {
      const info = stmt.run(sessionId, sessionId, JSON.stringify(entry));
      seqs.push(Number(info.lastInsertRowid));
    }
    return seqs;
  }

  /** 更新指定 seq 的时间线条目(用于 approval decision 回填已落盘条目)。 */
  updateEntry(sessionId: string, seq: number, entry: TimelineEntry): void {
    this.db
      .prepare('UPDATE entries SET entry_json = ? WHERE session_id = ? AND seq = ?')
      .run(JSON.stringify(entry), sessionId, seq);
  }

  loadEntries(sessionId: string): TimelineEntry[] {
    return this.db
      .prepare('SELECT entry_json FROM entries WHERE session_id = ? ORDER BY seq ASC')
      .all(sessionId)
      .map((row) => reviveLegacyDetectionEntry(JSON.parse(String(row.entry_json)) as TimelineEntry));
  }

  /** events 续传:返回 seq > fromSeq 的条目,每条带落盘 seq(前端据以推进水位)。 */
  loadEntriesAfter(
    sessionId: string,
    fromSeq: number,
  ): { seq: number; entry: TimelineEntry }[] {
    return this.db
      .prepare(
        'SELECT seq, entry_json FROM entries WHERE session_id = ? AND seq > ? ORDER BY seq ASC',
      )
      .all(sessionId, fromSeq)
      .map((row) => ({
        seq: Number(row.seq),
        entry: reviveLegacyDetectionEntry(JSON.parse(String(row.entry_json)) as TimelineEntry),
      }));
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
      .prepare(
        'SELECT message_json FROM messages WHERE session_id = ? AND shadowed = 0 ORDER BY seq ASC',
      )
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

  /**
   * 截断 kosong 消息:物理尾删活跃(shadowed=0)序列,只保留前 keepCount 条
   * (recall 用;被遮蔽的历史行不动)。活跃行 seq 始终连续(append 续接、
   * 删除都是尾删),按活跃序列中第 keepCount 条的 seq 截尾即可。
   */
  truncateMessages(sessionId: string, keepCount: number): void {
    if (keepCount <= 0) {
      this.db
        .prepare('DELETE FROM messages WHERE session_id = ? AND shadowed = 0')
        .run(sessionId);
      return;
    }
    const cutoff = this.db
      .prepare(
        'SELECT seq FROM messages WHERE session_id = ? AND shadowed = 0 ORDER BY seq ASC LIMIT 1 OFFSET ?',
      )
      .get(sessionId, keepCount - 1);
    if (cutoff === undefined) return; // 活跃序列不足 keepCount 条,无尾可删
    this.db
      .prepare('DELETE FROM messages WHERE session_id = ? AND shadowed = 0 AND seq > ?')
      .run(sessionId, Number(cutoff.seq));
  }

  /** 整体重写某 session 的消息序列(压缩后落盘;entries 显示表不动),
   * 使重启/懒恢复后仍保持压缩态。旧活跃行软遮蔽(shadowed=1,留在库中
   * 可查)而非物理删除;新序列 append ,seq 从全局 MAX(seq)+1 续接,与
   * P1 的增量落盘水位互不干扰。 */
  replaceMessages(sessionId: string, messages: readonly Message[]): void {
    this.db
      .prepare('UPDATE messages SET shadowed = 1 WHERE session_id = ? AND shadowed = 0')
      .run(sessionId);
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
