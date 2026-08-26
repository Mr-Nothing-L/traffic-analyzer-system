/**
 * 轮次写入器(TurnPersister):单轮 /chat 内「什么条目/消息何时落盘、压缩
 * 发生后整体重写还是增量 append」的单一持有者(此前双水位计数器横跨
 * agentLoop.ts 与 app.ts 两处,靠 compaction 事件传布尔协同,不变量无处
 * 安放;D5 收敛于此)。
 *
 * 由 loop 事件驱动(app.ts 的 onEvent / onStepPersist 接到本对象),不变量
 * 写成代码:
 *  - entries 与 messages 各持一个「已落盘水位」:只推进,不回退;
 *  - 条目落盘时机与此前一致:user / steer 条目立即;assistant / tool 条目
 *    在 step_done 时批量(approval 条目依赖 settleHook 回填 decision,需等
 *    本步审批落定);
 *  - 消息落盘由 onStepPersist 的更新驱动:appended → 增量 append;
 *    compacted → 立即整体重写(replaceMessages,软遮蔽旧行)——压缩成果
 *    即时持久化,中途崩溃不回退未压缩历史(旧行为要等轮末才回写);
 *  - 轮末 finalize 兜底:done / 异常 / 取消路径都把剩余条目与消息补齐,
 *    崩溃不丢半截轮次(半截 tool calls 悬挂由恢复时的 repair.ts 修复)。
 */
import type { Message } from '../llm/kosong';

import type { StepPersistUpdate } from '../loop/agentLoop';
import type { SessionManager } from './session';
import type { TimelineEntry } from './storage';

export interface TurnPersisterOptions {
  readonly sessions: SessionManager;
  readonly sessionId: string;
  /** 本轮会话已有消息数:消息水位从这里起算(本轮 user 消息未追加前)。 */
  readonly baseMessageCount: number;
}

export class TurnPersister {
  private readonly sessions: SessionManager;
  private readonly sessionId: string;
  private readonly turnEntries: TimelineEntry[] = [];
  /** 条目水位:turnEntries 前 persistedEntries 条已 append 落盘。 */
  private persistedEntries = 0;
  /** 消息水位:loop 消息数组(含 base 历史)前 persistedMessages 条已落盘。 */
  private persistedMessages: number;
  private finalized = false;

  constructor(options: TurnPersisterOptions) {
    this.sessions = options.sessions;
    this.sessionId = options.sessionId;
    this.persistedMessages = options.baseMessageCount;
  }

  /** 累积时间线条目(不落盘;落盘统一走 flushEntries)。 */
  pushEntry(entry: TimelineEntry): void {
    this.turnEntries.push(entry);
  }

  /** 审批回执回填:在已累积的 approval 条目上补 decision(倒序找最近一条);
   * 若该条目尚未落盘,decision 随下一次 flushEntries 一并序列化。 */
  settleApproval(
    requestId: string,
    decision: 'approved' | 'rejected' | 'cancelled',
  ): void {
    for (let i = this.turnEntries.length - 1; i >= 0; i -= 1) {
      const entry = this.turnEntries[i];
      if (entry !== undefined && entry.kind === 'approval' && entry.requestId === requestId) {
        entry.decision = decision;
        return;
      }
    }
  }

  /** 落盘水位之后的全部条目(user / steer 条目立即调,step_done 时批量)。 */
  flushEntries(): void {
    if (this.persistedEntries >= this.turnEntries.length) return;
    this.sessions.appendEntries(this.sessionId, this.turnEntries.slice(this.persistedEntries));
    this.persistedEntries = this.turnEntries.length;
  }

  /** 本轮首条 user 消息由 handleChat 直接落盘;水位对齐到 base + 1。 */
  markUserMessagePersisted(): void {
    this.persistedMessages += 1;
  }

  /** loop 的 onStepPersist 入口:appended → 增量 append;compacted → 整体重写。 */
  onStepPersist(update: StepPersistUpdate): void {
    if (update.kind === 'compacted') {
      this.sessions.replaceMessages(this.sessionId, [...update.messages]);
      this.persistedMessages = update.messages.length;
      return;
    }
    if (update.messages.length === 0) return;
    this.sessions.appendMessages(this.sessionId, [...update.messages]);
    this.persistedMessages += update.messages.length;
  }

  /**
   * 轮末兜底(done / 异常 / 取消路径都调):补齐剩余条目与消息。messages
   * 为 loop 返回的最终数组(异常路径拿不到时传 undefined,只兜底条目)。
   * 幂等,不会重复落盘已推进过的水位。
   */
  finalize(messages: readonly Message[] | undefined): void {
    if (this.finalized) return;
    this.finalized = true;
    this.flushEntries();
    if (messages !== undefined && messages.length > this.persistedMessages) {
      // loop 现行实现对每条消息都即时通知(onStepPersist),此分支只是
      // 防御水位与返回值脱节的回归安全网(如未来新增未通知的落盘点)。
      this.sessions.appendMessages(this.sessionId, [...messages].slice(this.persistedMessages));
      this.persistedMessages = messages.length;
    }
  }
}
