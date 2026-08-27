/**
 * Unit tests for shell tools: run_command + job_list/job_output/job_kill.
 *
 * These tests execute real but harmless shell commands (echo / sleep / printf)
 * inside a temporary workspace. No model API or toolserver is involved.
 */
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  isRunnableToolExecution,
  type ExecutableTool,
  type ExecutableToolErrorResult,
  type ExecutableToolResult,
  type RunnableToolExecution,
} from '../contract';
import {
  BackgroundJobRegistry,
  createJobKillTool,
  createJobListTool,
  createJobOutputTool,
  createRunCommandTool,
  createShellTools,
  type ShellToolsDeps,
} from './shellTools';
import type { ToolsetEntrySpec } from './videoTools';

let workspaceDir: string;
let workspace: WorkspaceConfig;
let jobRegistry: BackgroundJobRegistry;
let deps: ShellToolsDeps;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'shell-tools-test-'));
  workspace = { workspaceDir, additionalDirs: [] };
  jobRegistry = new BackgroundJobRegistry();
  deps = { workspace, jobRegistry };
});

afterEach(() => {
  // Ensure no lingering sleep processes remain.
  for (const job of jobRegistry.list()) {
    if (job.status === 'running') {
      jobRegistry.kill(job.job_id, 'SIGKILL');
    }
  }
  rmSync(workspaceDir, { recursive: true, force: true });
});

function spec(name: string): ToolsetEntrySpec {
  return { description: `desc:${name}`, parameters: { type: 'object', properties: {} } };
}

function shellTool(name: string): ExecutableTool {
  const tools = createShellTools(deps, (n) => spec(n));
  const tool = tools.find((t) => t.name === name);
  if (!tool) throw new Error(`tool ${name} not found`);
  return tool;
}

function runnable(tool: ExecutableTool, input: unknown): RunnableToolExecution {
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
  if (!isRunnableToolExecution(execution as never)) {
    throw new Error(`expected runnable execution, got: ${JSON.stringify(execution)}`);
  }
  return execution as RunnableToolExecution;
}

async function execute(tool: ExecutableTool, input: unknown): Promise<ExecutableToolResult> {
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
  if (!isRunnableToolExecution(execution as never)) {
    return execution as ExecutableToolErrorResult;
  }
  return (execution as RunnableToolExecution).execute({
    toolCallId: 'test-call',
    signal: new AbortController().signal,
  });
}

async function waitFor(
  predicate: () => boolean,
  timeoutMs = 5000,
  intervalMs = 50,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`waitFor timed out after ${timeoutMs}ms`);
}

function findJob(job_id: string) {
  return jobRegistry.list().find((j) => j.job_id === job_id);
}

describe('run_command', () => {
  it('runs an echo command and captures stdout', async () => {
    const result = await execute(shellTool('run_command'), { command: 'echo hello world' });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.exit_code).toBe(0);
    expect(parsed.stdout).toContain('hello world');
  });

  it('runs with the workspace as cwd', async () => {
    const result = await execute(shellTool('run_command'), { command: 'pwd' });
    expect(result.isError).toBeFalsy();
    expect(JSON.parse(result.output as string).stdout.trim()).toBe(workspaceDir);
  });

  it('returns non-zero exit code with stderr (not isError)', async () => {
    const result = await execute(shellTool('run_command'), {
      command: 'echo boom >&2; exit 7',
    });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.exit_code).toBe(7);
    expect(parsed.stderr).toContain('boom');
  });

  it('kills commands exceeding the timeout', async () => {
    const result = await execute(shellTool('run_command'), {
      command: 'sleep 30',
      timeout_sec: 1,
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('超时');
  }, 15000);

  it('declares execute-level (all) access and per-command approvalRule', () => {
    const execution = runnable(shellTool('run_command'), { command: 'echo hi' });
    expect(execution.accesses).toEqual([{ kind: 'all' }]);
    expect(execution.approvalRule).toBe('run_command(echo hi)');
  });

  it('truncates approvalRule at 80 chars for long commands', () => {
    const longCommand = 'echo ' + 'x'.repeat(100);
    const execution = runnable(shellTool('run_command'), { command: longCommand });
    expect(execution.approvalRule.length).toBeLessThanOrEqual(94); // 'run_command(' + 80 + '…' + ')'
    expect(execution.approvalRule.startsWith('run_command(')).toBe(true);
  });

  it('rejects an empty command at resolve time', async () => {
    const result = await execute(shellTool('run_command'), { command: '' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('command');
  });
});

describe('background jobs', () => {
  it('starts a background job and returns a job_id immediately', async () => {
    const result = await execute(shellTool('run_command'), {
      command: 'echo hello; sleep 0.1; echo done',
      background: true,
    });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.job_id).toMatch(/^job_\d+$/);
    expect(parsed.status).toBe('running');

    await waitFor(() => findJob(parsed.job_id)?.status !== 'running');

    const output = await execute(shellTool('job_output'), { job_id: parsed.job_id });
    expect(output.isError).toBeFalsy();
    const outParsed = JSON.parse(output.output as string);
    expect(outParsed.stdout).toContain('hello');
    expect(outParsed.stdout).toContain('done');
    expect(outParsed.finished).toBe(true);
    expect(outParsed.exit_code).toBe(0);
  });

  it('lists jobs with id, command, status and started_at', async () => {
    await execute(shellTool('run_command'), { command: 'sleep 0.5', background: true });
    const result = await execute(shellTool('job_list'), {});
    expect(result.isError).toBeFalsy();
    const list = JSON.parse(result.output as string) as Array<Record<string, unknown>>;
    expect(list).toHaveLength(1);
    expect(list[0]?.job_id).toMatch(/^job_\d+$/);
    expect(list[0]?.command).toBe('sleep 0.5');
    expect(list[0]?.status).toBe('running');
    expect(typeof list[0]?.started_at).toBe('string');
  });

  it('supports incremental output via offsets', async () => {
    const start = await execute(shellTool('run_command'), {
      command: 'printf abc',
      background: true,
    });
    const job_id = JSON.parse(start.output as string).job_id;

    await waitFor(() => findJob(job_id)?.status !== 'running');

    const first = await execute(shellTool('job_output'), { job_id, stdout_offset: 0 });
    const firstParsed = JSON.parse(first.output as string);
    expect(firstParsed.stdout).toBe('abc');
    expect(firstParsed.stdout_next_offset).toBe(3);

    const second = await execute(shellTool('job_output'), { job_id, stdout_offset: 1 });
    const secondParsed = JSON.parse(second.output as string);
    expect(secondParsed.stdout).toBe('bc');
    expect(secondParsed.stdout_next_offset).toBe(3);
  });

  it('kills a running background job', async () => {
    const start = await execute(shellTool('run_command'), {
      command: 'sleep 30',
      background: true,
    });
    const job_id = JSON.parse(start.output as string).job_id;

    const kill = await execute(shellTool('job_kill'), { job_id });
    expect(kill.isError).toBeFalsy();
    expect(JSON.parse(kill.output as string).killed).toBe(true);

    await waitFor(() => {
      const info = jobRegistry.list().find((j) => j.job_id === job_id);
      return info?.status === 'killed';
    });

    const output = await execute(shellTool('job_output'), { job_id });
    expect(JSON.parse(output.output as string).finished).toBe(true);
  });

  it('returns isError when killing a non-existent job', async () => {
    const result = await execute(shellTool('job_kill'), { job_id: 'job_no_such' });
    expect(result.isError).toBe(true);
  });

  it('returns isError when fetching output for a non-existent job', async () => {
    const result = await execute(shellTool('job_output'), { job_id: 'job_no_such' });
    expect(result.isError).toBe(true);
  });

  it('keeps each registry instance isolated', async () => {
    const otherRegistry = new BackgroundJobRegistry();
    const otherDeps: ShellToolsDeps = { workspace, jobRegistry: otherRegistry };
    const otherTool = createRunCommandTool(otherDeps, spec('run_command'));
    await execute(otherTool, { command: 'echo other', background: true });

    const list = await execute(shellTool('job_list'), {});
    expect(JSON.parse(list.output as string)).toHaveLength(0);
  });
});
