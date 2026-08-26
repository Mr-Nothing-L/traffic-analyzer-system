/**
 * Built-in permission policies, in chain order (first hit wins):
 * yolo-mode-approve → sensitive-file-access-ask → session-approval-history →
 * execute-access-ask → auto-mode-approve → default-readonly-approve →
 * fallback-ask.
 *
 * Ported from MoonshotAI/kimi-code (MIT)
 * packages/agent-core-v2/src/agent/permissionPolicy/policies/, minus DI.
 */
import { isSensitiveFile } from '../sandbox/path-access';
import type {
  PermissionPolicy,
  PermissionPolicyContext,
  PermissionPolicyResult,
} from './types';

/** Remembers approvalRule strings approved with scope 'session'. */
export class SessionApprovalStore {
  private readonly rules = new Set<string>();

  remember(approvalRule: string): void {
    this.rules.add(approvalRule);
  }

  has(approvalRule: string): boolean {
    return this.rules.has(approvalRule);
  }
}

export class YoloModeApprovePolicy implements PermissionPolicy {
  readonly name = 'yolo-mode-approve';

  evaluate(context: PermissionPolicyContext): PermissionPolicyResult | undefined {
    return context.mode === 'yolo' ? { kind: 'approve' } : undefined;
  }
}

export class SensitiveFileAccessAskPolicy implements PermissionPolicy {
  readonly name = 'sensitive-file-access-ask';

  evaluate(context: PermissionPolicyContext): PermissionPolicyResult | undefined {
    const hit = context.execution.accesses?.some(
      (access) => access.kind === 'file' && isSensitiveFile(access.path),
    );
    return hit === true ? { kind: 'ask' } : undefined;
  }
}

/**
 * 执行级访问(kind 'all',如 run_script):脚本可读写工作区任意文件,
 * 无法静态枚举其资源访问,故除 yolo(链首已放行)外一律人工批准,
 * auto 模式也不例外。排在 session-approval-history 之后,会话内已批准
 * 的 approvalRule 仍可放行;敏感文件硬 veto 在沙盒层先行,不受影响。
 */
export class ExecuteAccessAskPolicy implements PermissionPolicy {
  readonly name = 'execute-access-ask';

  evaluate(context: PermissionPolicyContext): PermissionPolicyResult | undefined {
    const hit = context.execution.accesses?.some((access) => access.kind === 'all');
    return hit === true ? { kind: 'ask' } : undefined;
  }
}

/**
 * auto 模式:自动批准一切工具操作。链上排在 sensitive-file-access-ask 与
 * execute-access-ask 之后,故敏感文件访问与执行级访问仍会在更前面 ask。
 */
export class AutoModeApprovePolicy implements PermissionPolicy {
  readonly name = 'auto-mode-approve';

  evaluate(context: PermissionPolicyContext): PermissionPolicyResult | undefined {
    return context.mode === 'auto' ? { kind: 'approve' } : undefined;
  }
}

export class SessionApprovalHistoryPolicy implements PermissionPolicy {
  readonly name = 'session-approval-history';

  constructor(private readonly store: SessionApprovalStore) {}

  evaluate(context: PermissionPolicyContext): PermissionPolicyResult | undefined {
    return this.store.has(context.execution.approvalRule)
      ? { kind: 'approve', reason: { match: 'session-approval' } }
      : undefined;
  }
}

/** Approves executions whose declared accesses are exclusively read/search. */
export class DefaultReadonlyApprovePolicy implements PermissionPolicy {
  readonly name = 'default-readonly-approve';

  evaluate(context: PermissionPolicyContext): PermissionPolicyResult | undefined {
    const accesses = context.execution.accesses;
    if (accesses === undefined) return undefined;
    const readonly = accesses.every(
      (access) =>
        access.kind === 'file' && (access.operation === 'read' || access.operation === 'search'),
    );
    return readonly ? { kind: 'approve' } : undefined;
  }
}

export class FallbackAskPolicy implements PermissionPolicy {
  readonly name = 'fallback-ask';

  evaluate(): PermissionPolicyResult {
    return { kind: 'ask' };
  }
}

export function createDefaultPolicies(store: SessionApprovalStore): PermissionPolicy[] {
  return [
    new YoloModeApprovePolicy(),
    new SensitiveFileAccessAskPolicy(),
    new SessionApprovalHistoryPolicy(store),
    new ExecuteAccessAskPolicy(),
    new AutoModeApprovePolicy(),
    new DefaultReadonlyApprovePolicy(),
    new FallbackAskPolicy(),
  ];
}
