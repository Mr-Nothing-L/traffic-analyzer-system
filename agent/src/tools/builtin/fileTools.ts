/**
 * Sandbox file tools: read_file / write_file / run_script.
 *
 * Every path goes through resolvePathAccess; sandbox violations are hard
 * vetoes returned as isError results (never entering the permission chain).
 * write_file declares a file write access so the permission chain can gate
 * it; run_script executes a script file inside the workspace (sh → bash,
 * py → python3) with a bounded timeout and truncated captured output.
 */
import { execFile } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

import { z } from 'zod';

import {
  PathSecurityError,
  canonicalizePath,
  isSensitiveFile,
  isWithinWorkspace,
  type PathAccessOperation,
  type WorkspaceConfig,
} from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolErrorResult,
  type ExecutableToolResult,
} from '../contract';
import type { ToolDescriptionLookup } from './videoTools';
import { invalidInputResult, truncateOutput } from './utils';

const execFileAsync = promisify(execFile);

const MAX_READ_CHARS = 100_000;
const DEFAULT_TIMEOUT_SEC = 60;
const MAX_TIMEOUT_SEC = 300;
const EXEC_MAX_BUFFER = 16 * 1024 * 1024;

const SCRIPT_INTERPRETERS: Record<string, string> = {
  '.sh': 'bash',
  '.py': 'python3',
};

export type WorkspacePathResolution =
  | { readonly ok: true; readonly path: string }
  | { readonly ok: false; readonly result: ExecutableToolErrorResult };

/**
 * Strict workspace confinement for builtin tools. Unlike resolvePathAccess's
 * default `absolute-outside-allowed` guard mode, ANY path resolving outside
 * the workspace (absolute or relative) is a hard veto → isError
 * (PATH_OUTSIDE_WORKSPACE); paths inside additionalDirs still pass. Sensitive
 * files are vetoed as PATH_SENSITIVE. Built from the sandbox module's
 * canonicalizePath/isWithinWorkspace/isSensitiveFile primitives (the sandbox
 * module itself is not modified).
 */
export function resolveWorkspacePath(
  rawPath: string,
  workspace: WorkspaceConfig,
  operation: PathAccessOperation,
): WorkspacePathResolution {
  let canonical: string;
  try {
    const expanded =
      rawPath === '~'
        ? os.homedir()
        : rawPath.startsWith('~/')
          ? path.join(os.homedir(), rawPath.slice(2))
          : rawPath;
    canonical = canonicalizePath(expanded, workspace.workspaceDir);
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

  if (isSensitiveFile(canonical)) {
    return {
      ok: false,
      result: {
        output:
          `路径访问被沙盒硬性拒绝 [PATH_SENSITIVE]: "${rawPath}" 命中敏感文件模式` +
          `(env / 私钥 / credentials),已阻止访问以保护密钥。`,
        isError: true,
      },
    };
  }

  if (!isWithinWorkspace(canonical, workspace)) {
    const verb = operation === 'write' ? '写入' : operation === 'search' ? '搜索' : '读取';
    return {
      ok: false,
      result: {
        output:
          `路径访问被沙盒硬性拒绝 [PATH_OUTSIDE_WORKSPACE]: "${rawPath}" 解析为 ` +
          `"${canonical}",位于工作区之外;${verb}操作仅限工作区(及附加目录)内。`,
        isError: true,
      },
    };
  }

  return { ok: true, path: canonical };
}

const readFileInputSchema = z.strictObject({
  path: z.string(),
});

const writeFileInputSchema = z.strictObject({
  path: z.string(),
  content: z.string(),
});

const runScriptInputSchema = z.strictObject({
  path: z.string(),
  args: z.array(z.string()).optional(),
  timeout_sec: z.number().int().min(1).max(MAX_TIMEOUT_SEC).optional(),
});

export function createReadFileTool(
  workspace: WorkspaceConfig,
  description: string,
): ExecutableTool {
  return {
    name: 'read_file',
    description,
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '文件路径(相对沙盒工作区,或其内的绝对路径)' },
      },
      required: ['path'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = readFileInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('read_file', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const filePath = resolved.path;
      return {
        accesses: ToolAccesses.readFile(filePath),
        approvalRule: `read_file(${filePath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          let content: string;
          try {
            content = await readFile(filePath, 'utf8');
          } catch (error) {
            return {
              output: `读取文件失败: ${error instanceof Error ? error.message : String(error)}`,
              isError: true,
            };
          }
          const truncated = truncateOutput(content, MAX_READ_CHARS);
          return { output: JSON.stringify({ path: filePath, content: truncated }) };
        },
      };
    },
  };
}

export function createWriteFileTool(
  workspace: WorkspaceConfig,
  description: string,
): ExecutableTool {
  return {
    name: 'write_file',
    description,
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '文件路径(相对沙盒工作区,或其内的绝对路径)' },
        content: { type: 'string', description: '要写入的完整文本内容' },
      },
      required: ['path', 'content'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = writeFileInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('write_file', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.path, workspace, 'write');
      if (!resolved.ok) return resolved.result;
      const filePath = resolved.path;
      return {
        accesses: ToolAccesses.writeFile(filePath),
        approvalRule: `write_file(${filePath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          try {
            await mkdir(path.dirname(filePath), { recursive: true });
            await writeFile(filePath, parsed.data.content, 'utf8');
          } catch (error) {
            return {
              output: `写入文件失败: ${error instanceof Error ? error.message : String(error)}`,
              isError: true,
            };
          }
          return {
            output: JSON.stringify({
              path: filePath,
              bytes_written: Buffer.byteLength(parsed.data.content, 'utf8'),
            }),
          };
        },
      };
    },
  };
}

export function createRunScriptTool(
  workspace: WorkspaceConfig,
  description: string,
): ExecutableTool {
  return {
    name: 'run_script',
    description,
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '脚本文件路径(沙盒内,.sh 或 .py)' },
        args: {
          type: 'array',
          items: { type: 'string' },
          default: [],
          description: '命令行参数列表',
        },
        timeout_sec: {
          type: 'integer',
          minimum: 1,
          maximum: MAX_TIMEOUT_SEC,
          default: DEFAULT_TIMEOUT_SEC,
          description: `超时秒数,默认 ${DEFAULT_TIMEOUT_SEC},上限 ${MAX_TIMEOUT_SEC}`,
        },
      },
      required: ['path'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = runScriptInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('run_script', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const scriptPath = resolved.path;
      const interpreter = SCRIPT_INTERPRETERS[path.extname(scriptPath).toLowerCase()];
      if (interpreter === undefined) {
        return {
          output: `不支持的脚本类型 "${path.extname(scriptPath)}",仅支持 .sh(bash)与 .py(python3)。`,
          isError: true,
        };
      }
      const args = parsed.data.args ?? [];
      const timeoutSec = Math.min(
        MAX_TIMEOUT_SEC,
        Math.max(1, parsed.data.timeout_sec ?? DEFAULT_TIMEOUT_SEC),
      );
      return {
        accesses: ToolAccesses.readFile(scriptPath),
        approvalRule: `run_script(${scriptPath})`,
        execute: async (ctx): Promise<ExecutableToolResult> => {
          try {
            const { stdout, stderr } = await execFileAsync(
              interpreter,
              [scriptPath, ...args],
              {
                cwd: workspace.workspaceDir,
                timeout: timeoutSec * 1000,
                maxBuffer: EXEC_MAX_BUFFER,
                signal: ctx.signal,
                env: { PATH: process.env['PATH'] ?? '/usr/local/bin:/usr/bin:/bin' },
              },
            );
            return {
              output: JSON.stringify({
                exit_code: 0,
                stdout: truncateOutput(stdout),
                stderr: truncateOutput(stderr),
              }),
            };
          } catch (error) {
            return scriptErrorResult(error, timeoutSec);
          }
        },
      };
    },
  };
}

interface ExecFailure {
  readonly message: string;
  readonly code?: number | string;
  readonly killed?: boolean;
  readonly signal?: string;
  readonly stdout?: string;
  readonly stderr?: string;
}

function scriptErrorResult(error: unknown, timeoutSec: number): ExecutableToolResult {
  const failure = error as ExecFailure;
  if (error instanceof Error && error.name === 'AbortError') {
    return { output: '脚本执行被中止(abort)。', isError: true };
  }
  if (failure.killed === true || failure.signal === 'SIGTERM') {
    return {
      output: JSON.stringify({
        error: `脚本执行超时(${timeoutSec}s),已被终止`,
        stdout: truncateOutput(failure.stdout ?? ''),
        stderr: truncateOutput(failure.stderr ?? ''),
      }),
      isError: true,
    };
  }
  if (typeof failure.code === 'number') {
    // Non-zero exit is a legitimate outcome: the model needs stdout/stderr.
    return {
      output: JSON.stringify({
        exit_code: failure.code,
        stdout: truncateOutput(failure.stdout ?? ''),
        stderr: truncateOutput(failure.stderr ?? ''),
      }),
    };
  }
  return { output: `脚本启动失败: ${failure.message}`, isError: true };
}

/** Create all three sandbox file tools; descriptions come from agent/config/toolset.json. */
export function createFileTools(
  workspace: WorkspaceConfig,
  describe: ToolDescriptionLookup,
): ExecutableTool[] {
  return [
    createReadFileTool(workspace, describe('read_file')),
    createWriteFileTool(workspace, describe('write_file')),
    createRunScriptTool(workspace, describe('run_script')),
  ];
}
