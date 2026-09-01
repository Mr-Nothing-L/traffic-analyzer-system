/**
 * 审批桥:把 ApprovalService 的 requestToolApproval 桥接到当前 /chat 的
 * SSE 流。挂起时发出 {type:'approval_request', ...} 事件并暂存 resolve;
 * POST /approval 到达时 resolve;超时(默认 5 分钟)或 cancelAll(reason)
 * (cancel / 客户端断连 / 轮末兜底)以 cancelled 语义落定。
 *
 * 桥本身不认识 HTTP/SSE,emit 回调由 app 层在 /chat 开始/结束时绑定。
 */
import { randomUUID } from 'node:crypto';

import type { ApprovalRequest, ApprovalResponse } from '../permissions/types';
import type { ToolAccesses } from '../tools/contract';

export const DEFAULT_APPROVAL_TIMEOUT_MS = 5 * 60 * 1000;

/** 审批内容预览。 */
export interface PreviewContent {
  readonly language: string;
  readonly content: string;
  readonly truncated: boolean;
}

/** 发给前端的审批请求事件(经 SSE 'data:' 行)。 */
export interface ApprovalRequestEvent {
  readonly type: 'approval_request';
  readonly requestId: string;
  readonly toolName: string;
  /** 即 ApprovalRequest.action,如 `write_file(/abs/path)`。 */
  readonly approvalRule: string;
  readonly description?: string;
  /** 执行声明的资源访问(由 ApprovalRequest 携带,经 gate 填充)。 */
  readonly accesses: ToolAccesses;
  /** 工具调用参数的内容预览(按工具类型构造)。 */
  readonly preview?: PreviewContent;
}

export interface ApprovalDecisionInput {
  readonly decision: 'approved' | 'rejected' | 'cancelled';
  readonly scope?: 'session';
  readonly feedback?: string;
}

export interface ApprovalBridgeOptions {
  /** 挂起审批的超时(ms),超时自动 cancelled;默认 5 分钟。 */
  readonly timeoutMs?: number;
}

interface PendingApproval {
  readonly resolve: (response: ApprovalResponse) => void;
  readonly timer: ReturnType<typeof setTimeout>;
  readonly event: ApprovalRequestEvent;
}

export class ApprovalBridge {
  private readonly timeoutMs: number;
  private readonly pending = new Map<string, PendingApproval>();
  private emit: ((event: ApprovalRequestEvent) => void) | undefined;
  private settleHook: ((requestId: string, response: ApprovalResponse) => void) | undefined;

  constructor(options: ApprovalBridgeOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? DEFAULT_APPROVAL_TIMEOUT_MS;
  }

  /** 绑定当前 /chat 的 SSE 输出;结束时务必 unbind。 */
  bindEmitter(emit: (event: ApprovalRequestEvent) => void): void {
    this.emit = emit;
  }

  unbindEmitter(): void {
    this.emit = undefined;
  }

  /** 绑定审批落定(回执/超时/取消)钩子,用于时间线条目记录;结束时务必 unbind。 */
  bindSettleHook(hook: (requestId: string, response: ApprovalResponse) => void): void {
    this.settleHook = hook;
  }

  unbindSettleHook(): void {
    this.settleHook = undefined;
  }

  /**
   * ApprovalService handler:发 approval_request 事件并挂起,直到
   * resolveDecision 被调用或超时。accesses 由 ApprovalRequest 携带;preview
   * 由调用方根据 arguments 构造后传入。
   */
  requestApproval(
    request: ApprovalRequest,
    extra?: { readonly preview?: PreviewContent },
  ): Promise<ApprovalResponse> {
    const requestId = randomUUID();
    const event: ApprovalRequestEvent = {
      type: 'approval_request',
      requestId,
      toolName: request.toolName,
      approvalRule: request.action,
      accesses: request.accesses ?? [],
      ...(extra?.preview !== undefined ? { preview: extra.preview } : {}),
      ...(request.description !== undefined ? { description: request.description } : {}),
    };
    return new Promise<ApprovalResponse>((resolve) => {
      const timer = setTimeout(() => {
        this.settle(requestId, { decision: 'cancelled' });
      }, this.timeoutMs);
      this.pending.set(requestId, { resolve, timer, event });
      this.emit?.(event);
    });
  }

  /** POST /approval 回执:未知 requestId 返回 false。 */
  resolveDecision(requestId: string, input: ApprovalDecisionInput): boolean {
    return this.settle(requestId, {
      decision: input.decision,
      ...(input.scope !== undefined ? { scope: input.scope } : {}),
      ...(input.feedback !== undefined ? { feedback: input.feedback } : {}),
    });
  }

  has(requestId: string): boolean {
    return this.pending.has(requestId);
  }

  /** 获取指定未决审批的完整事件(含 accesses/preview)。 */
  getPending(requestId: string): ApprovalRequestEvent | undefined {
    return this.pending.get(requestId)?.event;
  }

  /** 当前未决审批的 requestId 列表。 */
  pendingRequestIds(): readonly string[] {
    return [...this.pending.keys()];
  }

  /** 会话结束/过期/cancel/断连时,把全部挂起审批以 cancelled 语义落定
   *  (reason 进 feedback,随拒绝结果回灌给模型;审批不受 abort 信号影响,
   *  不主动落定会拖满审批超时)。 */
  cancelAll(reason?: string): void {
    for (const requestId of [...this.pending.keys()]) {
      this.settle(requestId, {
        decision: 'cancelled',
        ...(reason !== undefined ? { feedback: reason } : {}),
      });
    }
  }

  private settle(requestId: string, response: ApprovalResponse): boolean {
    const entry = this.pending.get(requestId);
    if (entry === undefined) return false;
    this.pending.delete(requestId);
    clearTimeout(entry.timer);
    this.settleHook?.(requestId, response);
    entry.resolve(response);
    return true;
  }
}
