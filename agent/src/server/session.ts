/**
 * 会话管理:内存索引 + node:sqlite 持久化(见 storage.ts,每 workspace 一个
 * DB 文件)。session = {id, workspaceDir, mode, title, messages, entries,
 * createdAt, lastActiveAt};create/get/list/appendMessages/appendEntries +
 * idle 过期清扫(默认 2h,只清内存不删盘;delete 才删盘)。
 *
 * 构造时可传 workspaces、运行时可调 restoreWorkspace 打开磁盘上的
 * sessions.db:都只「打开 storage」不加载内容(list() 直接以磁盘行为准);
 * get() 对已打开 storage 做整会话懒恢复(含 messages,续跑/压缩需要),
 * getEntries() 只读 entries(history 接口用,messages 里可能有几十 MB 的
 * 视频 dataURL,全量解析会阻塞事件循环)。过期清扫用 unref 的
 * setInterval,不阻塞进程退出。
 */
import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import path from 'node:path';

import type { Message } from '../llm/kosong';

import type { PermissionMode } from '../permissions/types';

import { repairTailMessages } from './repair';
import { SessionStorage, type StoredSession, type TimelineEntry } from './storage';
import { readWorkspaceRegistry } from './workspaceRegistry';

export interface Session {
  readonly id: string;
  readonly workspaceDir: string;
  /** 权限模式;运行中可经 setMode 切换。 */
  mode: PermissionMode;
  /** 会话标题(首轮用户输入前 30 字,未产生用户输入时为 '')。 */
  title: string;
  readonly messages: Message[];
  readonly entries: TimelineEntry[];
  readonly createdAt: number;
  lastActiveAt: number;
  /** 最近一次 /chat 轮次上报的真实上下文占用(token);尚无 usage 时为 undefined。 */
  lastKnownUsage?: number;
}

/** GET /sessions 的列表项(不含 messages/entries)。 */
export interface SessionSummary {
  readonly id: string;
  readonly workspaceDir: string;
  readonly mode: PermissionMode;
  readonly title: string;
  readonly createdAt: number;
  readonly lastActiveAt: number;
  /** 最近一次已知上下文占用(有真实 usage 才返回)。 */
  readonly usedTokens?: number;
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
  /**
   * 工作区登记表路径(web 层写入的 JSON 数组)。若提供,list() 前会自查恢复
   * 其中尚未打开的工作区;缺省时读环境变量 AGENT_WORKSPACE_REGISTRY_PATH。
   */
  readonly workspaceRegistryPath?: string;
}

export const DEFAULT_SESSION_IDLE_MS = 2 * 60 * 60 * 1000;
export const DEFAULT_SWEEP_INTERVAL_MS = 60_000;

/** recall 的结果:成功 / 未知 session / entryIndex 非法(越界、非 user、缺映射)。 */
export type RecallResult = 'ok' | 'not_found' | 'invalid_entry';

const TITLE_MAX_CHARS = 30;

export class SessionManager {
  private readonly sessions = new Map<string, Session>();
  private readonly storages = new Map<string, SessionStorage>();
  private readonly idleMs: number;
  private readonly onExpire: ((session: Session) => void) | undefined;
  private readonly registryPath: string | undefined;
  private readonly sweeper: ReturnType<typeof setInterval>;

  constructor(options: SessionManagerOptions = {}) {
    this.idleMs = options.idleMs ?? DEFAULT_SESSION_IDLE_MS;
    this.onExpire = options.onExpire;
    this.registryPath =
      options.workspaceRegistryPath ?? process.env.AGENT_WORKSPACE_REGISTRY_PATH;
    for (const workspaceDir of options.workspaces ?? []) {
      // 只打开 storage:磁盘行由 list()/get() 按需读取,启动不做全量加载。
      this.storageFor(workspaceDir);
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

  /**
   * history 专用:只取时间线条目,不物化整会话。内存命中直接返回;否则在
   * 已打开的 storage 里找该 session 并只解析 entries —— messages 里可能有
   * 几十 MB 的视频 dataURL(load_video),history 不需要,全量解析会阻塞
   * 事件循环。未知 session 返回 undefined。
   */
  getEntries(id: string): TimelineEntry[] | undefined {
    const cached = this.sessions.get(id);
    if (cached !== undefined) return cached.entries;
    for (const storage of this.storages.values()) {
      if (storage.getSession(id) !== undefined) return storage.loadEntries(id);
    }
    return undefined;
  }

  /**
   * events 续传(GET /sessions/{id}/events):返回磁盘上 seq > fromSeq 的
   * 条目(带 seq)。entries 内存与磁盘同步双写,直接以磁盘为准;未知
   * session 返回 undefined。
   */
  getEntriesAfter(
    id: string,
    fromSeq: number,
  ): { seq: number; entry: TimelineEntry }[] | undefined {
    for (const storage of this.storages.values()) {
      if (storage.getSession(id) !== undefined) return storage.loadEntriesAfter(id, fromSeq);
    }
    return undefined;
  }

  /** 所有已知 session 的摘要:磁盘为准,内存中的活跃时间/标题更新。
   *
   * 若配置了工作区登记表,先自查恢复其中尚未打开的工作区,让 agent server
   * 自行保证列表能看到全部历史会话(代理层不再在列表前做 restore 副作用)。
   */
  list(): SessionSummary[] {
    this.restoreFromRegistry();
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

  /**
   * 按工作区登记表自查恢复:只恢复尚未打开的 workspace,幂等。
   * 登记表路径未配置、不存在或损坏时 noop。
   */
  restoreFromRegistry(): void {
    if (this.registryPath === undefined) return;
    for (const workspaceDir of readWorkspaceRegistry(this.registryPath)) {
      if (this.storages.has(workspaceDir)) continue;
      this.restoreWorkspace(workspaceDir);
    }
  }

  /**
   * 运行时恢复一个 workspace 的历史会话(POST /workspaces/restore 用):
   * 打开 <workspaceDir>/.agent/sessions.db(文件不存在返回 0,不创建文件)。
   * 只打开 storage、不把 session 物化进内存:list() 直接以磁盘行为准,
   * 会话内容由 get()/getEntries() 按需懒恢复,避免启动时全量解析
   * messages(可能几十 MB)阻塞事件循环。幂等:storage 已打开时返回 0,
   * 否则返回磁盘会话数。
   */
  restoreWorkspace(workspaceDir: string): number {
    const dbPath = path.join(workspaceDir, '.agent', 'sessions.db');
    if (!existsSync(dbPath)) return 0;
    if (this.storages.has(workspaceDir)) return 0;
    const storage = this.storageFor(workspaceDir);
    return storage.listSessions().length;
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
  appendEntries(id: string, entries: readonly TimelineEntry[]): number[] {
    const session = this.sessions.get(id);
    if (session === undefined) return [];
    session.entries.push(...entries);
    const storage = this.storages.get(session.workspaceDir);
    const seqs = storage?.appendEntries(id, entries) ?? [];
    if (session.title === '') {
      const firstUser = entries.find((e) => e.kind === 'user');
      if (firstUser !== undefined && firstUser.kind === 'user') {
        session.title = firstUser.text.slice(0, TITLE_MAX_CHARS);
        storage?.updateTitle(id, session.title);
      }
    }
    this.bumpLastActive(session, storage);
    return seqs;
  }

  touch(id: string): void {
    const session = this.sessions.get(id);
    if (session !== undefined) {
      this.bumpLastActive(session, this.storages.get(session.workspaceDir));
    }
  }

  /** 更新指定 seq 的时间线条目(approval decision 回填已落盘条目)。 */
  updateEntry(id: string, seq: number, entry: TimelineEntry): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    const storage = this.storages.get(session.workspaceDir);
    storage?.updateEntry(id, seq, entry);
    return true;
  }

  /** 切换权限模式:内存 + 磁盘同步更新。未知 session 返回 false。 */
  setMode(id: string, mode: PermissionMode): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    session.mode = mode;
    this.storages.get(session.workspaceDir)?.updateMode(id, mode);
    return true;
  }

  /** 记录 session 最近一次真实上下文占用(内存态,随 /chat 的 context_usage 更新)。 */
  setLastKnownUsage(id: string, usedTokens: number): void {
    const session = this.sessions.get(id);
    if (session !== undefined) session.lastKnownUsage = usedTokens;
  }

  /** 整体替换 session 的消息历史(手动/自动压缩后),内存 + 磁盘同步;
   * 未知 session 返回 false。entries 显示表不动。 */
  replaceMessages(id: string, messages: readonly Message[]): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    session.messages.length = 0;
    session.messages.push(...messages);
    this.storages.get(session.workspaceDir)?.replaceMessages(id, messages);
    return true;
  }

  /**
   * 撤回:删除 entries[entryIndex..](内存+磁盘),并把 kosong messages 截断到
   * 该 user 条目对应的 user 消息之前(按条目落盘时记录的 messageIndex)。
   * 返回 'ok' | 'not_found' | 'invalid_entry'(越界/非 user/缺映射的旧条目)。
   */
  recall(id: string, entryIndex: number): RecallResult {
    const session = this.get(id);
    if (session === undefined) return 'not_found';
    const entry = session.entries[entryIndex];
    if (entry === undefined || entry.kind !== 'user' || entry.messageIndex === undefined) {
      return 'invalid_entry';
    }
    // 压缩后 messages 已整体重写落盘:messageIndex 相对新布局可能偏大,取小者兜底。
    const keepMessages = Math.min(entry.messageIndex, session.messages.length);
    session.entries.length = entryIndex;
    session.messages.length = keepMessages;
    const storage = this.storages.get(session.workspaceDir);
    storage?.truncateEntries(id, entryIndex);
    storage?.truncateMessages(id, keepMessages);
    if (entryIndex === 0 && session.title !== '') {
      // 标题取自首个 user 条目;它被一并撤回时清空标题(下轮首个 user 条目重算)。
      session.title = '';
      storage?.updateTitle(id, '');
    }
    this.bumpLastActive(session, storage);
    return 'ok';
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
    // 崩溃恢复:半截轮次可能留下「assistant 带 toolCalls 但工具结果未全部
    // 落盘」的悬挂尾部,合成 isError 工具消息补齐(见 repair.ts),并回写
    // 磁盘,保证恢复的历史 provider-valid。
    const loaded = storage.loadMessages(row.id);
    const repaired = repairTailMessages(loaded);
    if (repaired !== loaded) {
      storage.appendMessages(row.id, repaired.slice(loaded.length));
    }
    return {
      id: row.id,
      workspaceDir: row.workspaceDir,
      mode: row.mode,
      title: row.title,
      messages: [...repaired],
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
    ...(session.lastKnownUsage !== undefined ? { usedTokens: session.lastKnownUsage } : {}),
  };
}
