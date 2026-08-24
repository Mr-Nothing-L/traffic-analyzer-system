import type { ApprovalRequest, ApprovalResponse } from './types';

/**
 * Abstraction over the approval round-trip (UI prompt, SSE request to the web
 * layer, auto-answer in tests, ...).
 */
export interface ApprovalService {
  requestToolApproval(request: ApprovalRequest): Promise<ApprovalResponse>;
}

/**
 * Default ApprovalService backed by an injected callback, so the Web/SSE layer
 * can bridge approval requests to the frontend without this package knowing
 * about transports.
 */
export class CallbackApprovalService implements ApprovalService {
  constructor(
    private readonly handler: (request: ApprovalRequest) => Promise<ApprovalResponse>,
  ) {}

  requestToolApproval(request: ApprovalRequest): Promise<ApprovalResponse> {
    return this.handler(request);
  }
}
