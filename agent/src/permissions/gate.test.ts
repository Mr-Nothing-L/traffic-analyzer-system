import { describe, expect, it, vi } from 'vitest';

import { CallbackApprovalService } from './approval';
import { PermissionGate } from './gate';
import type { RunnableToolExecution, ToolAccesses } from '../tools/contract';
import { ToolAccesses as accesses } from '../tools/contract';
import type { ApprovalRequest, ApprovalResponse, PermissionMode } from './types';

function execution(accessList: ToolAccesses, approvalRule = 'write_file(/f)'): RunnableToolExecution {
  return {
    accesses: accessList,
    approvalRule,
    execute: () => Promise.resolve({ output: 'ok' }),
  };
}

function context(exec: RunnableToolExecution, name = 'write_file') {
  return {
    toolCall: { id: 'call-1', name, arguments: '{}' as string | null },
    execution: exec,
  };
}

function gateWith(
  mode: PermissionMode,
  handler: (request: ApprovalRequest) => Promise<ApprovalResponse>,
) {
  const service = new CallbackApprovalService(handler);
  const gate = new PermissionGate({ mode, approvalService: service });
  return { gate, service };
}

const autoApprove = () => Promise.resolve<ApprovalResponse>({ decision: 'approved' });

describe('PermissionGate', () => {
  it('yolo mode approves everything without asking, even sensitive writes', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('yolo', handler);

    const decision = await gate.authorize(
      context(execution(accesses.writeFile('/home/u/.env'), 'write_file(/home/u/.env)')),
    );

    expect(decision).toEqual({ kind: 'approve', policyName: 'yolo-mode-approve' });
    expect(handler).not.toHaveBeenCalled();
  });

  it('manual mode approves readonly accesses without asking', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('manual', handler);

    const decision = await gate.authorize(
      context(execution(accesses.readFile('/data/report.md')), 'read_file'),
    );

    expect(decision).toEqual({ kind: 'approve', policyName: 'default-readonly-approve' });
    expect(handler).not.toHaveBeenCalled();
  });

  it('auto mode approves ordinary tool operations without asking', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('auto', handler);

    const decision = await gate.authorize(context(execution(accesses.writeFile('/out/f.txt'))));

    expect(decision).toEqual({ kind: 'approve', policyName: 'auto-mode-approve' });
    expect(handler).not.toHaveBeenCalled();
  });

  it('auto mode still asks on sensitive file access', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('auto', handler);

    const decision = await gate.authorize(
      context(execution(accesses.writeFile('/home/u/.env'), 'write_file(/home/u/.env)')),
    );

    expect(decision).toEqual({ kind: 'approve', policyName: 'sensitive-file-access-ask' });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('setMode changes subsequent adjudications', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('manual', handler);
    const exec = execution(accesses.writeFile('/out/f.txt'));

    const before = await gate.authorize(context(exec));
    expect(before).toMatchObject({ kind: 'approve', policyName: 'fallback-ask' });
    expect(handler).toHaveBeenCalledTimes(1);

    gate.setMode('auto');
    const after = await gate.authorize(context(exec));
    expect(after).toEqual({ kind: 'approve', policyName: 'auto-mode-approve' });
    expect(handler).toHaveBeenCalledTimes(1);

    gate.setMode('manual');
    const backToManual = await gate.authorize(context(exec));
    expect(backToManual).toMatchObject({ kind: 'approve', policyName: 'fallback-ask' });
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('manual mode asks for writes and approves on approval', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('manual', handler);

    const decision = await gate.authorize(context(execution(accesses.writeFile('/out/f.txt'))));

    expect(decision).toEqual({ kind: 'approve', policyName: 'fallback-ask' });
    expect(handler).toHaveBeenCalledTimes(1);
    const request = handler.mock.calls[0]?.[0];
    expect(request).toMatchObject({
      toolCallId: 'call-1',
      toolName: 'write_file',
      action: 'write_file(/f)',
    });
  });

  it('manual mode denies on rejection and carries feedback', async () => {
    const handler = vi.fn(() =>
      Promise.resolve<ApprovalResponse>({ decision: 'rejected', feedback: 'do not touch that' }),
    );
    const { gate } = gateWith('manual', handler);

    const decision = await gate.authorize(context(execution(accesses.writeFile('/out/f.txt'))));

    expect(decision).toMatchObject({
      kind: 'deny',
      feedback: 'do not touch that',
      cancelled: false,
    });
  });

  it('manual mode marks cancellation', async () => {
    const handler = vi.fn(() => Promise.resolve<ApprovalResponse>({ decision: 'cancelled' }));
    const { gate } = gateWith('manual', handler);

    const decision = await gate.authorize(context(execution(accesses.writeFile('/out/f.txt'))));

    expect(decision).toMatchObject({ kind: 'deny', cancelled: true });
  });

  it('sensitive file access asks even when readonly in manual mode', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('manual', handler);

    const decision = await gate.authorize(
      context(execution(accesses.readFile('/home/u/.aws/credentials')), 'read_file'),
    );

    expect(decision).toEqual({ kind: 'approve', policyName: 'sensitive-file-access-ask' });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('session-scope approval is remembered and short-circuits later asks', async () => {
    const handler = vi.fn(() =>
      Promise.resolve<ApprovalResponse>({ decision: 'approved', scope: 'session' }),
    );
    const { gate } = gateWith('manual', handler);
    const exec = execution(accesses.writeFile('/out/f.txt'));

    const first = await gate.authorize(context(exec));
    expect(first).toMatchObject({ kind: 'approve', policyName: 'fallback-ask' });
    expect(handler).toHaveBeenCalledTimes(1);

    const second = await gate.authorize(context(exec));
    expect(second).toEqual({ kind: 'approve', policyName: 'session-approval-history' });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('approval without session scope is not remembered', async () => {
    const handler = vi.fn(autoApprove);
    const { gate } = gateWith('manual', handler);
    const exec = execution(accesses.writeFile('/out/f.txt'));

    await gate.authorize(context(exec));
    await gate.authorize(context(exec));

    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('invokes the ask continuation and honors its override', async () => {
    const handler = vi.fn(autoApprove);
    const gate = new PermissionGate({
      mode: 'manual',
      approvalService: new CallbackApprovalService(handler),
      policies: [
        {
          name: 'custom-ask',
          evaluate: () => ({
            kind: 'ask' as const,
            resolveApproval: (response) =>
              response.decision === 'approved'
                ? { kind: 'deny' as const, message: 'overridden by continuation' }
                : undefined,
          }),
        },
      ],
    });

    const decision = await gate.authorize(context(execution(accesses.writeFile('/out/f.txt'))));

    expect(decision).toMatchObject({
      kind: 'deny',
      policyName: 'custom-ask',
      message: 'overridden by continuation',
    });
  });
});
