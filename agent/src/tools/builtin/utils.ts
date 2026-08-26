/**
 * Shared helpers for builtin tools: zod input-error formatting, toolserver
 * error mapping, and output truncation. Path resolution lives solely in
 * fileTools.resolveWorkspacePath (strict workspace confinement — the single
 * path boundary; see its doc comment).
 */
import { z } from 'zod';

import type { ExecutableToolErrorResult } from '../contract';
import type { ToolserverErrorInfo } from './httpToolserver';

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
