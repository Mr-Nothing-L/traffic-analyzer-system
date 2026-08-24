/**
 * Shared helpers for builtin tools: sandbox path resolution (hard veto →
 * isError result), zod input-error formatting, and output truncation.
 */
import { z } from 'zod';

import {
  PathSecurityError,
  resolvePathAccess,
  type PathAccessOperation,
  type WorkspaceConfig,
} from '../../sandbox/path-access';
import type { ExecutableToolErrorResult } from '../contract';
import type { ToolserverErrorInfo } from './httpToolserver';

export type SandboxPathResolution =
  | { readonly ok: true; readonly path: string }
  | { readonly ok: false; readonly result: ExecutableToolErrorResult };

/**
 * Resolve `rawPath` against the workspace. A sandbox violation is a hard veto:
 * it is converted to an isError result (never enters the permission chain).
 */
export function resolveSandboxPath(
  rawPath: string,
  workspace: WorkspaceConfig,
  operation: PathAccessOperation,
): SandboxPathResolution {
  try {
    const access = resolvePathAccess(rawPath, workspace.workspaceDir, workspace, { operation });
    return { ok: true, path: access.path };
  } catch (error) {
    if (error instanceof PathSecurityError) {
      return {
        ok: false,
        result: {
          output: `路径访问被沙盒硬性拒绝 [${error.code}]: ${error.message}`,
          isError: true,
        },
      };
    }
    throw error;
  }
}

/** Format a zod failure as a model-readable isError result. */
export function invalidInputResult(toolName: string, error: z.ZodError): ExecutableToolErrorResult {
  const details = error.issues
    .map((issue) => `- ${issue.path.join('.') || '(root)'}: ${issue.message}`)
    .join('\n');
  return { output: `${toolName} 参数不合法,请修正后重试:\n${details}`, isError: true };
}

/** Map a toolserver error to an isError tool result. */
export function toolserverErrorResult(error: ToolserverErrorInfo): ExecutableToolErrorResult {
  const status = error.status === undefined ? '' : ` (HTTP ${error.status})`;
  return { output: `工具服务错误 [${error.code}]${status}: ${error.message}`, isError: true };
}

export const OUTPUT_TRUNCATE_LIMIT = 8000;

export function truncateOutput(text: string, max: number = OUTPUT_TRUNCATE_LIMIT): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}\n... [输出被截断,原文共 ${text.length} 字符]`;
}
