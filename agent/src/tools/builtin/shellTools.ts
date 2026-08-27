/**
 * Shell tools: run_command + background job management (job_list / job_output / job_kill).
 *
 * - run_command executes arbitrary shell commands inside the workspace sandbox
 *   using `bash -c`. Foreground mode waits for completion and returns captured
 *   stdout/stderr/exit_code; background mode spawns a detached-ish child process,
 *   returns a job_id immediately, and buffers its output for later retrieval.
 * - job_list / job_output / job_kill operate on the injected BackgroundJobRegistry.
 *   The registry is intentionally NOT a module-level singleton; the server assembly
 *   constructs one instance per session/process and injects it into the tool
 *   factories (lessons learned from spawnSubagent.ts module-level semaphore).
 *
 * Security: commands run with cwd=workspace and env={PATH} only, matching
 * run_script. run_command declares execute-level `all` access so it enters the
 * permission chain; approvalRule is per-command (first 80 chars) so session-scope
 * approvals do not over-apply.
 */
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { promisify } from 'node:util';

import { z } from 'zod';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolContext,
  type ExecutableToolErrorResult,
  type ExecutableToolResult,
} from '../contract';
import { invalidInputResult, truncateOutput } from './utils';
import type { ToolsetEntrySpec, ToolsetLookup } from './videoTools';

const execFileAsync = promisify(execFile);

const DEFAULT_TIMEOUT_SEC = 30;
const MAX_TIMEOUT_SEC = 300;
const EXEC_MAX_BUFFER = 16 * 1024 * 1024;

/** Per-background-job output buffer cap (characters). Old data is dropped. */
const MAX_BACKGROUND_OUTPUT_CHARS = 64 * 1024;

const runCommandInputSchema = z.strictObject({
  command: z.string().min(1),
  timeout_sec: z.number().int().min(1).max(MAX_TIMEOUT_SEC).optional(),
  background: z.boolean().optional(),
});

const jobOutputInputSchema = z.strictObject({
  job_id: z.string().min(1),
  stdout_offset: z.number().int().min(0).optional(),
  stderr_offset: z.number().int().min(0).optional(),
});

const jobKillInputSchema = z.strictObject({
  job_id: z.string().min(1),
  signal: z.string().optional(),
});

export type JobStatus = 'running' | 'exited' | 'killed';

export interface BackgroundJobInfo {
  readonly job_id: string;
  readonly command: string;
  readonly status: JobStatus;
  readonly exit_code: number | null;
  readonly started_at: string;
}

export interface BackgroundJobOutput {
  readonly stdout: string;
  readonly stderr: string;
  readonly stdout_truncated: boolean;
  readonly stderr_truncated: boolean;
  readonly stdout_next_offset: number;
  readonly stderr_next_offset: number;
  readonly finished: boolean;
  readonly exit_code: number | null;
}

interface BackgroundJobRecord {
  readonly job_id: string;
  readonly command: string;
  readonly process: ChildProcess;
  readonly started_at: Date;
  readonly stdout: CappedBuffer;
  readonly stderr: CappedBuffer;
  status: JobStatus;
  exit_code: number | null;
}

/** Simple FIFO text buffer that drops the oldest characters when over capacity. */
class CappedBuffer {
  private text = '';
  private dropped = 0;

  constructor(private readonly capacity: number) {}

  append(chunk: string): void {
    this.text += chunk;
    if (this.text.length > this.capacity) {
      const over = this.text.length - this.capacity;
      this.dropped += over;
      this.text = this.text.slice(over);
    }
  }

  read(offset = 0): { readonly text: string; readonly truncated: boolean; readonly nextOffset: number } {
    const start = Math.max(0, offset - this.dropped);
    return {
      text: this.text.slice(start),
      truncated: offset < this.dropped,
      nextOffset: this.dropped + this.text.length,
    };
  }
}

/**
 * Injectable registry for background jobs. One instance per session/process,
 * passed to the shell tool factories via ShellToolsDeps.
 */
export class BackgroundJobRegistry {
  private jobs = new Map<string, BackgroundJobRecord>();
  private counter = 0;

  /**
   * Spawn `bash -c <command>` in the background, capture its stdout/stderr,
   * and return a stable job_id for later list/output/kill operations.
   */
  start(command: string, cwd: string, env: NodeJS.ProcessEnv): string {
    this.counter += 1;
    const job_id = `job_${this.counter}`;
    const stdout = new CappedBuffer(MAX_BACKGROUND_OUTPUT_CHARS);
    const stderr = new CappedBuffer(MAX_BACKGROUND_OUTPUT_CHARS);

    const child = spawn('bash', ['-c', command], {
      cwd,
      env,
      // Keep the child tied to our process group so a SIGTERM reaches it and
      // its descendants (e.g. `sleep` launched from the bash -c line).
      detached: false,
    });

    const job: BackgroundJobRecord = {
      job_id,
      command,
      process: child,
      started_at: new Date(),
      stdout,
      stderr,
      status: 'running',
      exit_code: null,
    };

    child.stdout?.setEncoding('utf8');
    child.stderr?.setEncoding('utf8');
    child.stdout?.on('data', (chunk: string) => stdout.append(chunk));
    child.stderr?.on('data', (chunk: string) => stderr.append(chunk));

    // Use 'close' so all stdout/stderr data has been drained before marking
    // the job finished.
    child.on('close', (code, signal) => {
      if (signal) {
        job.status = 'killed';
        job.exit_code = code ?? null;
      } else {
        job.status = 'exited';
        job.exit_code = code ?? 0;
      }
    });

    child.on('error', () => {
      if (job.status === 'running') {
        job.status = 'exited';
        job.exit_code = child.exitCode ?? 1;
      }
    });

    this.jobs.set(job_id, job);
    return job_id;
  }

  list(): BackgroundJobInfo[] {
    return [...this.jobs.values()].map((job) => ({
      job_id: job.job_id,
      command: job.command,
      status: job.status,
      exit_code: job.exit_code,
      started_at: job.started_at.toISOString(),
    }));
  }

  getOutput(
    job_id: string,
    stdout_offset = 0,
    stderr_offset = 0,
  ): BackgroundJobOutput | undefined {
    const job = this.jobs.get(job_id);
    if (job === undefined) return undefined;
    const so = job.stdout.read(stdout_offset);
    const se = job.stderr.read(stderr_offset);
    return {
      stdout: so.text,
      stderr: se.text,
      stdout_truncated: so.truncated,
      stderr_truncated: se.truncated,
      stdout_next_offset: so.nextOffset,
      stderr_next_offset: se.nextOffset,
      finished: job.status !== 'running',
      exit_code: job.exit_code,
    };
  }

  kill(job_id: string, signal: NodeJS.Signals = 'SIGTERM'): boolean {
    const job = this.jobs.get(job_id);
    if (job === undefined || job.status !== 'running') return false;
    return job.process.kill(signal);
  }
}

export interface ShellToolsDeps {
  readonly workspace: WorkspaceConfig;
  readonly jobRegistry: BackgroundJobRegistry;
}

function buildCommandEnv(): NodeJS.ProcessEnv {
  return { PATH: process.env['PATH'] ?? '/usr/local/bin:/usr/bin:/bin' };
}

function truncateCommand(command: string, maxLen = 80): string {
  const normalized = command.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLen) return normalized;
  return `${normalized.slice(0, maxLen)}…`;
}

interface ExecFailure {
  readonly message: string;
  readonly code?: number | string;
  readonly killed?: boolean;
  readonly signal?: string;
  readonly stdout?: string;
  readonly stderr?: string;
}

function commandErrorResult(error: unknown, timeoutSec: number): ExecutableToolResult {
  const failure = error as ExecFailure;
  if (error instanceof Error && error.name === 'AbortError') {
    return { output: '命令执行被中止(abort)。', isError: true };
  }
  if (failure.killed === true || failure.signal === 'SIGTERM') {
    return {
      output: JSON.stringify({
        error: `命令执行超时(${timeoutSec}s),已被终止`,
        stdout: truncateOutput(failure.stdout ?? ''),
        stderr: truncateOutput(failure.stderr ?? ''),
      }),
      isError: true,
    };
  }
  if (typeof failure.code === 'number') {
    return {
      output: JSON.stringify({
        exit_code: failure.code,
        stdout: truncateOutput(failure.stdout ?? ''),
        stderr: truncateOutput(failure.stderr ?? ''),
      }),
    };
  }
  return { output: `命令启动失败: ${failure.message}`, isError: true };
}

export function createRunCommandTool(
  deps: ShellToolsDeps,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'run_command',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = runCommandInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('run_command', parsed.error);
      const input = parsed.data;
      const cwd = deps.workspace.workspaceDir;
      const env = buildCommandEnv();
      const timeoutSec = Math.min(
        MAX_TIMEOUT_SEC,
        Math.max(1, input.timeout_sec ?? DEFAULT_TIMEOUT_SEC),
      );
      const approvalCommand = truncateCommand(input.command);

      return {
        accesses: ToolAccesses.all(),
        approvalRule: `run_command(${approvalCommand})`,
        execute: async (ctx): Promise<ExecutableToolResult> => {
          if (input.background === true) {
            const job_id = deps.jobRegistry.start(input.command, cwd, env);
            return { output: JSON.stringify({ job_id, command: input.command, status: 'running' }) };
          }
          try {
            const { stdout, stderr } = await execFileAsync('bash', ['-c', input.command], {
              cwd,
              timeout: timeoutSec * 1000,
              maxBuffer: EXEC_MAX_BUFFER,
              signal: ctx.signal,
              env,
            });
            return {
              output: JSON.stringify({
                exit_code: 0,
                stdout: truncateOutput(stdout),
                stderr: truncateOutput(stderr),
              }),
            };
          } catch (error) {
            return commandErrorResult(error, timeoutSec);
          }
        },
      };
    },
  };
}

export function createJobListTool(
  deps: ShellToolsDeps,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'job_list',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      if (
        rawInput !== undefined &&
        rawInput !== null &&
        Object.keys(rawInput as object).length > 0
      ) {
        return invalidInputResult(
          'job_list',
          new z.ZodError([
            { code: 'unrecognized_keys', keys: Object.keys(rawInput as object), path: [], message: 'job_list 不接受参数' },
          ]),
        );
      }
      return {
        accesses: ToolAccesses.none(),
        approvalRule: 'job_list()',
        execute: async (): Promise<ExecutableToolResult> => ({
          output: JSON.stringify(deps.jobRegistry.list()),
        }),
      };
    },
  };
}

export function createJobOutputTool(
  deps: ShellToolsDeps,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'job_output',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = jobOutputInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('job_output', parsed.error);
      const input = parsed.data;
      return {
        accesses: ToolAccesses.none(),
        approvalRule: `job_output(${input.job_id})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const output = deps.jobRegistry.getOutput(
            input.job_id,
            input.stdout_offset ?? 0,
            input.stderr_offset ?? 0,
          );
          if (output === undefined) {
            return { output: `未找到后台任务 ${input.job_id}`, isError: true };
          }
          return { output: JSON.stringify(output) };
        },
      };
    },
  };
}

export function createJobKillTool(
  deps: ShellToolsDeps,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'job_kill',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = jobKillInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('job_kill', parsed.error);
      const input = parsed.data;
      const signal = (input.signal ?? 'SIGTERM') as NodeJS.Signals;
      return {
        accesses: ToolAccesses.all(),
        approvalRule: `job_kill(${input.job_id})`,
        execute: async (): Promise<ExecutableToolResult> => {
          try {
            const ok = deps.jobRegistry.kill(input.job_id, signal);
            if (!ok) {
              return { output: `无法终止任务 ${input.job_id}(不存在或已结束)`, isError: true };
            }
            return { output: JSON.stringify({ killed: true, job_id: input.job_id, signal }) };
          } catch (error) {
            return {
              output: `终止任务失败: ${error instanceof Error ? error.message : String(error)}`,
              isError: true,
            };
          }
        },
      };
    },
  };
}

export function createShellTools(deps: ShellToolsDeps, lookup: ToolsetLookup): ExecutableTool[] {
  return [
    createRunCommandTool(deps, lookup('run_command')),
    createJobListTool(deps, lookup('job_list')),
    createJobOutputTool(deps, lookup('job_output')),
    createJobKillTool(deps, lookup('job_kill')),
  ];
}
