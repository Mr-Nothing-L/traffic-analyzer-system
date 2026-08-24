/**
 * 内存会话管理:session = {id, workspaceDir, mode, messages, createdAt,
 * lastActiveAt};create/get/appendMessages + idle 过期清扫(默认 2h)。
 *
 * 纯内存实现,进程重启即丢;过期清扫用 unref 的 setInterval,不阻塞进程退出。
 */
import { randomUUID } from 'node:crypto';

import type { Message } from '#/message';

import type { PermissionMode } from '../permissions/types';

export interface Session {
  readonly id: string;
  readonly workspaceDir: string;
  readonly mode: PermissionMode;
  readonly messages: Message[];
  readonly createdAt: number;
  lastActiveAt: number;
}

export interface SessionManagerOptions {
  /** idle 过期阈值(ms),默认 2 小时。 */
  readonly idleMs?: number;
  /** 清扫周期(ms),默认 60s。 */
  readonly sweepIntervalMs?: number;
  /** 会话被清扫/删除时的回调(用于取消挂起的审批等清理)。 */
  readonly onExpire?: (session: Session) => void;
}

export const DEFAULT_SESSION_IDLE_MS = 2 * 60 * 60 * 1000;
export const DEFAULT_SWEEP_INTERVAL_MS = 60_000;

export class SessionManager {
  private readonly sessions = new Map<string, Session>();
  private readonly idleMs: number;
  private readonly onExpire: ((session: Session) => void) | undefined;
  private readonly sweeper: ReturnType<typeof setInterval>;

  constructor(options: SessionManagerOptions = {}) {
    this.idleMs = options.idleMs ?? DEFAULT_SESSION_IDLE_MS;
    this.onExpire = options.onExpire;
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
      messages: [],
      createdAt: now,
      lastActiveAt: now,
    };
    this.sessions.set(session.id, session);
    return session;
  }

  get(id: string): Session | undefined {
    return this.sessions.get(id);
  }

  /** 追加消息并刷新活跃时间。未知 session 返回 false。 */
  appendMessages(id: string, messages: readonly Message[]): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    session.messages.push(...messages);
    session.lastActiveAt = Date.now();
    return true;
  }

  touch(id: string): void {
    const session = this.sessions.get(id);
    if (session !== undefined) session.lastActiveAt = Date.now();
  }

  delete(id: string): boolean {
    const session = this.sessions.get(id);
    if (session === undefined) return false;
    this.sessions.delete(id);
    this.onExpire?.(session);
    return true;
  }

  /** 清扫超过 idleMs 未活跃的会话,返回被清扫的数量。 */
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
  }
}
