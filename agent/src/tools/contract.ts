/**
 * Tool contract: tools declare their resource accesses before execution so the
 * permission chain and the concurrency scheduler can consume the metadata.
 *
 * Ported from MoonshotAI/kimi-code (MIT) packages/agent-core-v2/src/tool/toolContract.ts,
 * trimmed to what this runtime needs (no DI, no delivery/display metadata).
 */
import type { ContentPart } from '../kosong/message';
import type { Tool } from '../kosong/tool';

export type { Tool } from '../kosong/tool';

export type ExecutableToolOutput = string | ContentPart[];

export interface ExecutableToolSuccessResult {
  readonly output: ExecutableToolOutput;
  readonly isError?: false;
  readonly stopTurn?: boolean;
  readonly note?: string;
}

export interface ExecutableToolErrorResult {
  readonly output: ExecutableToolOutput;
  readonly isError: true;
  readonly stopTurn?: boolean;
  readonly note?: string;
}

export type ExecutableToolResult = ExecutableToolSuccessResult | ExecutableToolErrorResult;

export interface ToolUpdate {
  kind: 'stdout' | 'stderr' | 'progress' | 'status';
  text?: string;
  percent?: number;
}

export interface ExecutableToolContext {
  readonly toolCallId: string;
  readonly signal: AbortSignal;
  readonly onUpdate?: (update: ToolUpdate) => void;
  /**
   * 嵌套(子代理)loop 事件上报通道,由 loop 执行工具时注入:工具(如
   * spawn_subagent)把子 loop 事件交给它,loop 侧包装成 AgentLoopEvent
   * 'subagent_event' 进入父 loop 事件流。声明为 unknown 是为了避免
   * contract ↔ loop 的循环导入;生产方保证传入 AgentLoopEvent。
   */
  readonly onSubagentEvent?: (event: unknown) => void | Promise<void>;
}

export interface RunnableToolExecution {
  readonly accesses?: ToolAccesses;
  readonly description?: string;
  readonly stopBatchAfterThis?: boolean;
  /**
   * Stable string identifying "what is being done" for approval UX and
   * session-scope approval memory, e.g. `write_file(/abs/path)`.
   */
  readonly approvalRule: string;
  readonly matchesRule?: (ruleArgs: string) => boolean;
  /**
   * 单次执行超时(ms);缺省用 loop 级 toolTimeoutMs。长任务工具
   * (如 spawn_subagent,600s)在此抬高自己的上限。
   */
  readonly timeoutMs?: number;
  readonly execute: (ctx: ExecutableToolContext) => Promise<ExecutableToolResult>;
}

/**
 * resolveExecution may fail before producing a runnable execution (e.g. path
 * vetoed by the sandbox); in that case it returns an error result directly.
 */
export type ToolExecution = RunnableToolExecution | ExecutableToolErrorResult;

export function isRunnableToolExecution(
  execution: ToolExecution,
): execution is RunnableToolExecution {
  return typeof (execution as RunnableToolExecution).execute === 'function';
}

export interface ExecutableTool<Input = unknown> extends Tool {
  resolveExecution(input: Input): ToolExecution | Promise<ToolExecution>;
}

export type ToolFileAccessOperation = 'read' | 'write' | 'readwrite' | 'search';

export interface ToolFileAccess {
  readonly kind: 'file';
  readonly operation: ToolFileAccessOperation;
  readonly path: string;
  readonly recursive?: boolean;
}

export interface ToolResourceAccessAll {
  readonly kind: 'all';
}

export type ToolResourceAccess = ToolFileAccess | ToolResourceAccessAll;
export type ToolAccesses = readonly ToolResourceAccess[];

export const ToolAccesses = {
  none(): ToolAccesses {
    return [];
  },

  all(): ToolAccesses {
    return [{ kind: 'all' }];
  },

  file(
    operation: ToolFileAccessOperation,
    path: string,
    options: { readonly recursive?: boolean } = {},
  ): ToolAccesses {
    return [{ kind: 'file', operation, path, recursive: options.recursive }];
  },

  readFile(path: string): ToolAccesses {
    return ToolAccesses.file('read', path);
  },

  readTree(path: string): ToolAccesses {
    return ToolAccesses.file('read', path, { recursive: true });
  },

  writeFile(path: string): ToolAccesses {
    return ToolAccesses.file('write', path);
  },

  writeTree(path: string): ToolAccesses {
    return ToolAccesses.file('write', path, { recursive: true });
  },

  readWriteFile(path: string): ToolAccesses {
    return ToolAccesses.file('readwrite', path);
  },

  searchTree(path: string): ToolAccesses {
    return ToolAccesses.file('search', path, { recursive: true });
  },

  /**
   * Write-write / write-read conflict: two accesses conflict when at least one
   * side writes and their paths overlap (exact match or recursive prefix).
   */
  conflict(left: ToolAccesses, right: ToolAccesses): boolean {
    return left.some((leftAccess) =>
      right.some((rightAccess) => resourceAccessesConflict(leftAccess, rightAccess)),
    );
  },
};

function resourceAccessesConflict(left: ToolResourceAccess, right: ToolResourceAccess): boolean {
  if (left.kind === 'all' || right.kind === 'all') return true;
  if (!fileOperationsConflict(left.operation, right.operation)) return false;
  return fileAccessesOverlap(left, right);
}

function fileOperationsConflict(
  left: ToolFileAccessOperation,
  right: ToolFileAccessOperation,
): boolean {
  return fileOperationWrites(left) || fileOperationWrites(right);
}

function fileOperationWrites(operation: ToolFileAccessOperation): boolean {
  switch (operation) {
    case 'read':
    case 'search':
      return false;
    case 'write':
    case 'readwrite':
      return true;
  }
}

function fileAccessesOverlap(left: ToolFileAccess, right: ToolFileAccess): boolean {
  const leftPath = normalizePath(left.path);
  const rightPath = normalizePath(right.path);
  if (leftPath === rightPath) return true;

  const leftPrefix = leftPath.endsWith('/') ? leftPath : `${leftPath}/`;
  const rightPrefix = rightPath.endsWith('/') ? rightPath : `${rightPath}/`;
  return (
    (left.recursive === true && rightPath.startsWith(leftPrefix)) ||
    (right.recursive === true && leftPath.startsWith(rightPrefix))
  );
}

function normalizePath(path: string): string {
  const normalized = path.replaceAll('\\', '/').replaceAll(/\/+/g, '/');
  const folded = normalized.toLowerCase();
  if (folded.length > 1 && folded.endsWith('/')) {
    return folded.slice(0, -1);
  }
  return folded;
}
