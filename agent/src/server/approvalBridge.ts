/**
 * 审批桥:把 ApprovalService 的 requestToolApproval 桥接到当前 /chat 的
 * SSE 流。挂起时发出 {type:'approval_request', ...} 事件并暂存 resolve;
 * POST /approval 到达时 resolve;超时(默认 5 分钟)自动 cancelled。
 *
 * 桥本身不认识 HTTP/SSE,emit 回调由 app 层在 /chat 开始/结束时绑定。
 */
import { randomUUID } from 'node:crypto';

import type { ApprovalRequest, ApprovalResponse } from '../permissions/types';
import type { ToolAccesses } from '../tools/contract';

export const DEFAULT_APPROVAL_TIMEOUT_MS = 5 * 60 * 1000;

/** 发给前端的审批请求事件(经 SSE 'data:' 行)。 */
export interface ApprovalRequestEvent {
  readonly type: 'approval_request';
  readonly requestId: string;
  readonly toolName: string;
  /** 即 ApprovalRequest.action,如 `write_file(/abs/path)`。 */
  readonly approvalRule: string;
  readonly description?: string;
  /** 执行声明的资源访问(由 gate 层快照,ApprovalRequest 本身不携带)。 */
  readonly accesses: ToolAccesses;
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
}

export class ApprovalBridge {
  private readonly timeoutMs: number;
  private readonly pending = new Map<string, PendingApproval>();
  private emit: ((event: ApprovalRequestEvent) => void) | undefined;

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

  /**
   * ApprovalService handler:发 approval_request 事件并挂起,直到
   * resolveDecision 被调用或超时。accesses 由调用方(gate 快照)提供。
   */
  requestApproval(
    request: ApprovalRequest,
    extra: { readonly accesses: ToolAccesses },
  ): Promise<ApprovalResponse> {
    const requestId = randomUUID();
    const event: ApprovalRequestEvent = {
      type: 'approval_request',
      requestId,
      toolName: request.toolName,
      approvalRule: request.action,
      accesses: extra.accesses,
      ...(request.description !== undefined ? { description: request.description } : {}),
    };
    return new Promise<ApprovalResponse>((resolve) => {
      const timer = setTimeout(() => {
        this.settle(requestId, { decision: 'cancelled' });
      }, this.timeoutMs);
      this.pending.set(requestId, { resolve, timer });
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

  /** 会话结束/过期时取消所有挂起的审批。 */
  cancelAll(): void {
    for (const requestId of [...this.pending.keys()]) {
      this.settle(requestId, { decision: 'cancelled' });
    }
  }

  private settle(requestId: string, response: ApprovalResponse): boolean {
    const entry = this.pending.get(requestId);
    if (entry === undefined) return false;
    this.pending.delete(requestId);
    clearTimeout(entry.timer);
    entry.resolve(response);
    return true;
  }
}
