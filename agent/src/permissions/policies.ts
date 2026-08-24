/**
 * Built-in permission policies, in chain order (first hit wins):
 * yolo-mode-approve → sensitive-file-access-ask → session-approval-history →
 * default-readonly-approve → fallback-ask.
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
    new DefaultReadonlyApprovePolicy(),
    new FallbackAskPolicy(),
  ];
}
