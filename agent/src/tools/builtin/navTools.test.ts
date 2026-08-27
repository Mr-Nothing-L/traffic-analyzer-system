/**
 * Unit tests for navTools: edit_file / glob_files / grep_files.
 * Runs against a temporary workspace; no external services.
 */
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  isRunnableToolExecution,
  type ExecutableTool,
  type ExecutableToolErrorResult,
  type RunnableToolExecution,
} from '../contract';
import {
  createEditFileTool,
  createGlobFilesTool,
  createGrepFilesTool,
} from './navTools';
import type { ToolsetEntrySpec } from './videoTools';

let workspaceDir: string;
let workspace: WorkspaceConfig;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'nav-tools-test-'));
  workspace = { workspaceDir, additionalDirs: [] };
});

afterEach(() => {
  rmSync(workspaceDir, { recursive: true, force: true });
});

function spec(): ToolsetEntrySpec {
  return { description: 'desc', parameters: { type: 'object', properties: {} } };
}

function runnable(tool: ExecutableTool, input: unknown): RunnableToolExecution {
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
  if (!isRunnableToolExecution(execution as never)) {
    throw new Error(`expected runnable execution, got: ${JSON.stringify(execution)}`);
  }
  return execution as RunnableToolExecution;
}

async function execute(
  tool: ExecutableTool,
  input: unknown,
): Promise<ExecutableToolResult | ExecutableToolErrorResult> {
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
  if (!isRunnableToolExecution(execution as never)) {
    return execution as ExecutableToolErrorResult;
  }
  return (execution as RunnableToolExecution).execute({
    toolCallId: 'test-call',
    signal: new AbortController().signal,
  });
}

describe('edit_file', () => {
  const tool = (): ExecutableTool => createEditFileTool(workspace, spec());

  it('replaces old_string with new_string and reports a summary', async () => {
    const filePath = path.join(workspaceDir, 'notes.txt');
    writeFileSync(filePath, 'line1\nhello world\nline3\n', 'utf8');

    const result = await execute(tool(), {
      path: 'notes.txt',
      old_string: 'hello world',
      new_string: 'hi there',
    });
    expect(result.isError).toBeFalsy();
    const summary = JSON.parse(result.output as string);
    expect(summary.path).toBe(filePath);
    expect(summary.replacements).toBe(1);
    expect(summary.changed_lines).toBe(1);
    expect(summary.context).toContain('hi there');

    expect(readText('notes.txt')).toBe('line1\nhi there\nline3\n');
  });

  it('returns an error when old_string is not unique and replace_all is false', async () => {
    writeFileSync(path.join(workspaceDir, 'dup.txt'), 'foo foo foo', 'utf8');
    const result = await execute(tool(), {
      path: 'dup.txt',
      old_string: 'foo',
      new_string: 'bar',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('出现 3 次');
    expect(result.output).toContain('replace_all=true');
  });

  it('replaces all occurrences when replace_all is true', async () => {
    writeFileSync(path.join(workspaceDir, 'dup.txt'), 'foo foo foo', 'utf8');
    const result = await execute(tool(), {
      path: 'dup.txt',
      old_string: 'foo',
      new_string: 'bar',
      replace_all: true,
    });
    expect(result.isError).toBeFalsy();
    const summary = JSON.parse(result.output as string);
    expect(summary.replacements).toBe(3);
    expect(readText('dup.txt')).toBe('bar bar bar');
  });

  it('returns an error when old_string is missing', async () => {
    writeFileSync(path.join(workspaceDir, 'a.txt'), 'abc', 'utf8');
    const result = await execute(tool(), {
      path: 'a.txt',
      old_string: 'xyz',
      new_string: '123',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('未找到');
  });

  it('declares a readwrite access on the resolved file', () => {
    const filePath = path.join(workspaceDir, 'x.txt');
    writeFileSync(filePath, 'a', 'utf8');
    const execution = runnable(tool(), { path: 'x.txt', old_string: 'a', new_string: 'b' });
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'readwrite', path: filePath, recursive: undefined },
    ]);
  });

  it('hard-vetoes paths outside the workspace', async () => {
    const result = await execute(tool(), {
      path: '../evil.txt',
      old_string: 'a',
      new_string: 'b',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
  });

  function readText(relativePath: string): string {
    return readFileSync(path.join(workspaceDir, relativePath), 'utf8');
  }
});

describe('glob_files', () => {
  const tool = (): ExecutableTool => createGlobFilesTool(workspace, spec());

  it('finds files with ** recursive patterns', async () => {
    mkdirSync(path.join(workspaceDir, 'src'), { recursive: true });
    writeFileSync(path.join(workspaceDir, 'a.ts'), '1', 'utf8');
    writeFileSync(path.join(workspaceDir, 'b.js'), '2', 'utf8');
    writeFileSync(path.join(workspaceDir, 'src', 'c.ts'), '3', 'utf8');

    const result = await execute(tool(), { pattern: '**/*.ts' });
    expect(result.isError).toBeFalsy();
    const { files } = JSON.parse(result.output as string);
    expect(files).toHaveLength(2);
    expect(files).toContain(path.join(workspaceDir, 'a.ts'));
    expect(files).toContain(path.join(workspaceDir, 'src', 'c.ts'));
  });

  it('supports brace expansion', async () => {
    writeFileSync(path.join(workspaceDir, 'x.ts'), '1', 'utf8');
    writeFileSync(path.join(workspaceDir, 'x.js'), '2', 'utf8');
    writeFileSync(path.join(workspaceDir, 'x.txt'), '3', 'utf8');

    const result = await execute(tool(), { pattern: '*.{ts,js}' });
    expect(result.isError).toBeFalsy();
    const { files } = JSON.parse(result.output as string);
    expect(files).toHaveLength(2);
    expect(files).toContain(path.join(workspaceDir, 'x.ts'));
    expect(files).toContain(path.join(workspaceDir, 'x.js'));
  });

  it('truncates results to 200 files', async () => {
    for (let i = 0; i < 250; i++) {
      writeFileSync(path.join(workspaceDir, `f${i}.txt`), String(i), 'utf8');
    }
    const result = await execute(tool(), { pattern: '*.txt' });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.files).toHaveLength(200);
    expect(parsed.truncated).toBe(true);
  });

  it('sorts results by modification time descending', async () => {
    const first = path.join(workspaceDir, 'first.txt');
    const second = path.join(workspaceDir, 'second.txt');
    writeFileSync(first, '1', 'utf8');
    await new Promise((resolve) => setTimeout(resolve, 20));
    writeFileSync(second, '2', 'utf8');

    const result = await execute(tool(), { pattern: '*.txt' });
    expect(result.isError).toBeFalsy();
    const { files } = JSON.parse(result.output as string);
    expect(files[0]).toBe(second);
    expect(files[1]).toBe(first);
  });

  it('declares a searchTree access on the resolved directory', () => {
    const execution = runnable(tool(), { pattern: '*.ts' });
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'search', path: workspaceDir, recursive: true },
    ]);
  });

  it('hard-vetoes paths outside the workspace', async () => {
    const result = await execute(tool(), { pattern: '*.txt', path: '../outside' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
  });
});

describe('grep_files', () => {
  const tool = (): ExecutableTool => createGrepFilesTool(workspace, spec());

  it('finds lines matching a regex', async () => {
    writeFileSync(path.join(workspaceDir, 'a.ts'), 'const x = 1;\nconst y = 2;\n', 'utf8');
    writeFileSync(path.join(workspaceDir, 'b.ts'), 'const z = 3;\n', 'utf8');

    const result = await execute(tool(), { pattern: 'const .* = 1' });
    expect(result.isError).toBeFalsy();
    const { results } = JSON.parse(result.output as string);
    expect(results).toHaveLength(1);
    expect(results[0]).toMatchObject({
      path: path.join(workspaceDir, 'a.ts'),
      line: 1,
      text: 'const x = 1;',
      match: true,
    });
  });

  it('includes context lines around matches', async () => {
    writeFileSync(
      path.join(workspaceDir, 'lines.txt'),
      'one\ntwo\nthree\nfour\nfive\n',
      'utf8',
    );
    const result = await execute(tool(), { pattern: 'three', context_lines: 1 });
    expect(result.isError).toBeFalsy();
    const { results } = JSON.parse(result.output as string);
    expect(results).toHaveLength(3);
    expect(results.map((r: GrepResult) => r.text)).toEqual(['two', 'three', 'four']);
    expect(results[1].match).toBe(true);
    expect(results[0].match).toBe(false);
  });

  it('filters by include glob', async () => {
    writeFileSync(path.join(workspaceDir, 'a.ts'), 'target\n', 'utf8');
    writeFileSync(path.join(workspaceDir, 'b.js'), 'target\n', 'utf8');

    const result = await execute(tool(), { pattern: 'target', include: '*.ts' });
    expect(result.isError).toBeFalsy();
    const { results } = JSON.parse(result.output as string);
    expect(results).toHaveLength(1);
    expect(results[0].path).toBe(path.join(workspaceDir, 'a.ts'));
  });

  it('skips binary files', async () => {
    writeFileSync(path.join(workspaceDir, 'binary.bin'), Buffer.from([0, 1, 2, 0]), 'utf8');
    writeFileSync(path.join(workspaceDir, 'text.txt'), 'target\n', 'utf8');

    const result = await execute(tool(), { pattern: 'target' });
    expect(result.isError).toBeFalsy();
    const { results } = JSON.parse(result.output as string);
    expect(results).toHaveLength(1);
    expect(results[0].path).toBe(path.join(workspaceDir, 'text.txt'));
  });

  it('skips files larger than 2MB', async () => {
    const big = 'x'.repeat(3 * 1024 * 1024);
    writeFileSync(path.join(workspaceDir, 'big.txt'), big, 'utf8');
    writeFileSync(path.join(workspaceDir, 'small.txt'), 'target\n', 'utf8');

    const result = await execute(tool(), { pattern: 'target' });
    expect(result.isError).toBeFalsy();
    const { results } = JSON.parse(result.output as string);
    expect(results).toHaveLength(1);
    expect(results[0].path).toBe(path.join(workspaceDir, 'small.txt'));
  });

  it('truncates results to 200 lines', async () => {
    for (let i = 0; i < 250; i++) {
      writeFileSync(path.join(workspaceDir, `f${i}.txt`), `target ${i}\n`, 'utf8');
    }
    const result = await execute(tool(), { pattern: 'target' });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.results).toHaveLength(200);
    expect(parsed.truncated).toBe(true);
  });

  it('declares a searchTree access on the resolved directory', () => {
    const execution = runnable(tool(), { pattern: 'foo' });
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'search', path: workspaceDir, recursive: true },
    ]);
  });

  it('hard-vetoes paths outside the workspace', async () => {
    const result = await execute(tool(), { pattern: 'foo', path: '../outside' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
  });
});

interface GrepResult {
  readonly path: string;
  readonly line: number;
  readonly text: string;
  readonly match: boolean;
}
