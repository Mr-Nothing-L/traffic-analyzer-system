/**
 * 会话管理:内存索引 + node:sqlite 持久化(见 storage.ts,每 workspace 一个
 * DB 文件)。session = {id, workspaceDir, mode, title, messages, entries,
 * createdAt, lastActiveAt};create/get/list/appendMessages/appendEntries +
 * idle 过期清扫(默认 2h,只清内存不删盘;delete 才删盘)。
 *
 * 构造时可传 workspaces 把磁盘上的历史 session 全量加载进内存;get() 对
 * 已打开 storage 做懒恢复(过期清扫后重新命中)。过期清扫用 unref 的
 * setInterval,不阻塞进程退出。
 */
import { randomUUID } from 'node:crypto';

import type { Message } from '#/message';

import type { PermissionMode } from '../permissions/types';

import { SessionStorage, type StoredSession, type TimelineEntry } from './storage';

export interface Session {
  readonly id: string;
  readonly workspaceDir: string;
  readonly mode: PermissionMode;
  /** 会话标题(首轮用户输入前 30 字,未产生用户输入时为 '')。 */
  title: string;
  readonly messages: Message[];
  readonly entries: TimelineEntry[];
  readonly createdAt: number;
  lastActiveAt: number;
}

/** GET /sessions 的列表项(不含 messages/entries)。 */
export interface SessionSummary {
  readonly id: string;
  readonly workspaceDir: string;
  readonly mode: PermissionMode;
  readonly title: string;
  readonly createdAt: number;
  readonly lastActiveAt: number;
}

export interface SessionManagerOptions {
  /** idle 过期阈值(ms),默认 2 小时。 */
  readonly idleMs?: number;
  /** 清扫周期(ms),默认 60s。 */
  readonly sweepIntervalMs?: number;
  /** 会话被清扫/删除时的回调(用于取消挂起的审批等清理)。 */
  readonly onExpire?: (session: Session) => void;
  /** 启动时从这些 workspace 的 .agent/sessions.db 加载全部历史 session。 */
  readonly workspaces?: readonly string[];
}

export const DEFAULT_SESSION_IDLE_MS = 2 * 60 * 60 * 1000;
export const DEFAULT_SWEEP_INTERVAL_MS = 60_000;

const TITLE_MAX_CHARS = 30;

export class SessionManager {
  private readonly sessions = new Map<string, Session>();
  private readonly storages = new Map<string, SessionStorage>();
  private readonly idleMs: number;
  private readonly onExpire: ((session: Session) => void) | undefined;
  private readonly sweeper: ReturnType<typeof setInterval>;

  constructor(options: SessionManagerOptions = {}) {
    this.idleMs = options.idleMs ?? DEFAULT_SESSION_IDLE_MS;
    this.onExpire = options.onExpire;
    for (const workspaceDir of options.workspaces ?? []) {
      const storage = this.storageFor(workspaceDir);
      for (const row of storage.listSessions()) {
        this.sessions.set(row.id, this.materialize(storage, row));
      }
    }
    this.sweeper = setInterval(() => {
      this.sweep();
    }, options.sweepIntervalMs ?? DEFAULT_SWEEP_INTERVAL_MS);
    this.sweeper.unref();
  }

  create(input: { workspaceDir: string; mode: PermissionMode }): Session {
    const now = Date.now();
    const session: Session = {
      id: randomUUID(),
      workspaceDir: input.workspaceDir,
      mode: input.mode,
      title: '',
      messages: [],
      entries: [],
      createdAt: now,
      lastActiveAt: now,
    };
    this.storageFor(input.workspaceDir).insertSession(session);
    this.sessions.set(session.id, session);
    return session;
  }

  /** 内存命中直接返回;否则在已打开的 storage 里懒恢复(过期只清内存不删盘)。 */
  get(id: string): Session | undefined {
    const cached = this.sessions.get(id);
    if (cached !== undefined) return cached;
    for (const storage of this.storages.values()) {
      const row = storage.getSession(id);
      if (row !== undefined) {
        const session = this.materialize(storage, row);
        this.sessions.set(session.id, session);
        return session;
      }
    }
    return undefined;
  }

  /** 所有已知 session 的摘要:磁盘为准,内存中的活跃时间/标题更新。 */
  list(): SessionSummary[] {
    const byId = new Map<string, SessionSummary>();
    for (const storage of this.storages.values()) {
      for (const row of storage.listSessions()) {
        byId.set(row.id, row);
      }
    }
    for (const session of this.sessions.values()) {
      byId.set(session.id, summaryOf(session));
    }
    return [...byId.values()].sort((a, b) => a.createdAt - b.createdAt);
  }

  /** 追加消息并刷新活跃时间(同步落盘)。未知 session 返回 false。 */
  appendMessages(id: string, messages: readonly Message[]): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    session.messages.push(...messages);
    const storage = this.storages.get(session.workspaceDir);
    storage?.appendMessages(id, messages);
    this.bumpLastActive(session, storage);
    return true;
  }

  /** 追加时间线条目并刷新活跃时间(同步落盘);首个 user 条目生成 title。 */
  appendEntries(id: string, entries: readonly TimelineEntry[]): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    session.entries.push(...entries);
    const storage = this.storages.get(session.workspaceDir);
    storage?.appendEntries(id, entries);
    if (session.title === '') {
      const firstUser = entries.find((e) => e.kind === 'user');
      if (firstUser !== undefined && firstUser.kind === 'user') {
        session.title = firstUser.text.slice(0, TITLE_MAX_CHARS);
        storage?.updateTitle(id, session.title);
      }
    }
    this.bumpLastActive(session, storage);
    return true;
  }

  touch(id: string): void {
    const session = this.sessions.get(id);
    if (session !== undefined) {
      this.bumpLastActive(session, this.storages.get(session.workspaceDir));
    }
  }

  /** 删除会话:内存 + 磁盘三张表一起清。未知 session 返回 false。 */
  delete(id: string): boolean {
    const session = this.sessions.get(id);
    this.sessions.delete(id);
    if (session !== undefined) {
      this.storages.get(session.workspaceDir)?.deleteSession(id);
      this.onExpire?.(session);
      return true;
    }
    // 内存未命中(例如已被清扫),仍尝试从各 storage 删盘。
    for (const storage of this.storages.values()) {
      if (storage.getSession(id) !== undefined) {
        storage.deleteSession(id);
        return true;
      }
    }
    return false;
  }

  /** 清扫超过 idleMs 未活跃的会话(只清内存不删盘),返回被清扫的数量。 */
  sweep(): number {
    const cutoff = Date.now() - this.idleMs;
    let removed = 0;
    for (const session of this.sessions.values()) {
      if (session.lastActiveAt < cutoff) {
        this.sessions.delete(session.id);
        this.onExpire?.(session);
        removed += 1;
      }
    }
    return removed;
  }

  close(): void {
    clearInterval(this.sweeper);
    for (const storage of this.storages.values()) {
      storage.close();
    }
    this.storages.clear();
  }

  private storageFor(workspaceDir: string): SessionStorage {
    let storage = this.storages.get(workspaceDir);
    if (storage === undefined) {
      storage = new SessionStorage(workspaceDir);
      this.storages.set(workspaceDir, storage);
    }
    return storage;
  }

  private materialize(storage: SessionStorage, row: StoredSession): Session {
    return {
      id: row.id,
      workspaceDir: row.workspaceDir,
      mode: row.mode,
      title: row.title,
      messages: storage.loadMessages(row.id),
      entries: storage.loadEntries(row.id),
      createdAt: row.createdAt,
      lastActiveAt: row.lastActiveAt,
    };
  }

  private bumpLastActive(session: Session, storage: SessionStorage | undefined): void {
    session.lastActiveAt = Date.now();
    storage?.updateLastActive(session.id, session.lastActiveAt);
  }
}

function summaryOf(session: Session): SessionSummary {
  return {
    id: session.id,
    workspaceDir: session.workspaceDir,
    mode: session.mode,
    title: session.title,
    createdAt: session.createdAt,
    lastActiveAt: session.lastActiveAt,
  };
}
