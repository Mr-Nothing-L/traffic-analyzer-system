import type { ApprovalService } from './approval';
import { createDefaultPolicies, SessionApprovalStore } from './policies';
import type {
  ApprovalRequest,
  PermissionMode,
  PermissionPolicy,
  PermissionPolicyContext,
  PermissionPolicyResult,
} from './types';

export type PermissionGateDecision =
  | { readonly kind: 'approve'; readonly policyName: string }
  | {
      readonly kind: 'deny';
      readonly policyName: string;
      readonly message?: string;
      readonly feedback?: string;
      readonly cancelled: boolean;
    };

export interface PermissionGateOptions {
  readonly mode: PermissionMode;
  readonly approvalService: ApprovalService;
  readonly sessionApprovals?: SessionApprovalStore;
  /** Overrides the built-in chain when provided. */
  readonly policies?: readonly PermissionPolicy[];
}

const MAX_ASK_ITERATIONS = 8;

/**
 * Runs the permission policy chain for a tool execution: first policy
 * returning non-undefined wins; `ask` results are routed through the
 * ApprovalService, and scope:'session' approvals are remembered.
 */
export class PermissionGate {
  private mode: PermissionMode;
  private readonly approvalService: ApprovalService;
  private readonly sessionApprovals: SessionApprovalStore;
  private readonly policies: readonly PermissionPolicy[];

  constructor(options: PermissionGateOptions) {
    this.mode = options.mode;
    this.approvalService = options.approvalService;
    this.sessionApprovals = options.sessionApprovals ?? new SessionApprovalStore();
    this.policies = options.policies ?? createDefaultPolicies(this.sessionApprovals);
  }

  /** 切换权限模式;之后的裁决立即生效(进行中的 ask 不受影响)。 */
  setMode(mode: PermissionMode): void {
    this.mode = mode;
  }

  async authorize(
    context: Omit<PermissionPolicyContext, 'mode'>,
  ): Promise<PermissionGateDecision> {
    const fullContext: PermissionPolicyContext = { ...context, mode: this.mode };

    let policyName = 'none';
    let result: PermissionPolicyResult = { kind: 'ask' };
    for (const policy of this.policies) {
      const evaluated = await policy.evaluate(fullContext);
      if (evaluated !== undefined) {
        policyName = policy.name;
        result = evaluated;
        break;
      }
    }

    for (let i = 0; ; i++) {
      switch (result.kind) {
        case 'approve':
          return { kind: 'approve', policyName };
        case 'deny':
          return { kind: 'deny', policyName, message: result.message, cancelled: false };
        case 'ask': {
          if (i >= MAX_ASK_ITERATIONS) {
            return {
              kind: 'deny',
              policyName,
              message: 'Approval continuation did not settle',
              cancelled: false,
            };
          }
          const request: ApprovalRequest = {
            toolCallId: fullContext.toolCall.id,
            toolName: fullContext.toolCall.name,
            action: fullContext.execution.approvalRule,
            description: fullContext.execution.description,
          };
          const response = await this.approvalService.requestToolApproval(request);
          const resolved = result.resolveApproval?.(response);
          if (resolved !== undefined) {
            result = resolved;
            continue;
          }
          if (response.decision === 'approved') {
            if (response.scope === 'session') {
              this.sessionApprovals.remember(fullContext.execution.approvalRule);
            }
            return { kind: 'approve', policyName };
          }
          return {
            kind: 'deny',
            policyName,
            feedback: response.feedback,
            cancelled: response.decision === 'cancelled',
          };
        }
      }
    }
  }
}
