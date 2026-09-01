/**
 * Permission chain types: an ordered responsibility chain of policies, first
 * hit wins, `undefined` means "not applicable, pass to the next policy".
 *
 * Ported from MoonshotAI/kimi-code (MIT)
 * packages/agent-core-v2/src/agent/permissionPolicy/types.ts, minus DI.
 */
import type { RunnableToolExecution, ToolAccesses } from '../tools/contract';

export type PermissionMode = 'yolo' | 'manual' | 'auto';

export interface ApprovalRequest {
  toolCallId: string;
  toolName: string;
  /** Human/rule-readable description of the action, e.g. `write_file(/abs/path)`. */
  action: string;
  description?: string;
  /** 本次执行声明的资源访问(由 gate 从 execution 填充)。 */
  accesses?: ToolAccesses;
  /** 工具调用原始参数(JSON 字符串),供审批桥构造内容预览。 */
  arguments?: string | null;
}

export interface ApprovalResponse {
  decision: 'approved' | 'rejected' | 'cancelled';
  /** 'session' remembers the approval for the rest of the session. */
  scope?: 'session';
  feedback?: string;
}

export type PermissionReasonValue = string | number | boolean | null;
export type PermissionDecisionReason = Readonly<Record<string, PermissionReasonValue>>;

export type PermissionPolicyResult =
  | {
      readonly kind: 'approve';
      readonly reason?: PermissionDecisionReason;
    }
  | {
      readonly kind: 'deny';
      readonly reason?: PermissionDecisionReason;
      readonly message?: string;
    }
  | {
      readonly kind: 'ask';
      readonly reason?: PermissionDecisionReason;
      /**
       * Continuation invoked with the user's approval response. Returning a
       * result overrides the default mapping (approved→approve,
       * rejected/cancelled→deny); returning undefined keeps the default.
       */
      readonly resolveApproval?: (response: ApprovalResponse) => PermissionPolicyResult | undefined;
    };

export interface PermissionPolicyContext {
  readonly mode: PermissionMode;
  readonly toolCall: {
    readonly id: string;
    readonly name: string;
    readonly arguments: string | null;
  };
  readonly execution: RunnableToolExecution;
}

export interface PermissionPolicy {
  readonly name: string;
  evaluate(
    context: PermissionPolicyContext,
  ): PermissionPolicyResult | undefined | Promise<PermissionPolicyResult | undefined>;
}
