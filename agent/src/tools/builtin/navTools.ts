/**
 * Navigation/search file tools: edit_file / glob_files / grep_files.
 *
 * All path resolution goes through fileTools.resolveWorkspacePath (the single
 * strict workspace boundary). edit_file declares readwrite access on the target
 * file; glob/grep declare searchTree access on the resolved root directory.
 */
import { opendir, readFile, stat, writeFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import { isWithinDirectory } from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolResult,
} from '../contract';
import { resolveWorkspacePath } from './fileTools';
import { invalidInputResult } from './utils';
import type { ToolsetEntrySpec } from './videoTools';

const MAX_GLOB_RESULTS = 200;
const MAX_GREP_RESULTS = 200;
const MAX_GREP_FILE_BYTES = 2 * 1024 * 1024;
const MAX_GREP_CONTEXT_LINES = 5;
const BINARY_CHECK_BYTES = 8192;

const editFileInputSchema = z.strictObject({
  path: z.string(),
  old_string: z.string(),
  new_string: z.string(),
  replace_all: z.boolean().optional(),
});

const globFilesInputSchema = z.strictObject({
  pattern: z.string(),
  path: z.string().optional(),
});

const grepFilesInputSchema = z.strictObject({
  pattern: z.string(),
  path: z.string().optional(),
  include: z.string().optional(),
  context_lines: z.number().int().min(0).max(MAX_GREP_CONTEXT_LINES).optional(),
});

interface GrepResult {
  readonly path: string;
  readonly line: number;
  readonly text: string;
  readonly match: boolean;
}

/** realpathSync that yields undefined when the path does not exist (yet). */
function safeRealpath(filePath: string): string | undefined {
  try {
    return realpathSync(filePath);
  } catch {
    return undefined;
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function createEditFileTool(
  workspace: WorkspaceConfig,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'edit_file',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = editFileInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('edit_file', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.path, workspace, 'write');
      if (!resolved.ok) return resolved.result;
      const filePath = resolved.path;
      return {
        accesses: ToolAccesses.readWriteFile(filePath),
        approvalRule: `edit_file(${filePath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          let content: string;
          try {
            content = await readFile(filePath, 'utf8');
          } catch (error) {
            return {
              output: `读取文件失败: ${errorMessage(error)}`,
              isError: true,
            };
          }

          const { old_string, new_string, replace_all } = parsed.data;
          const occurrences = countOccurrences(content, old_string);
          if (occurrences === 0) {
            return {
              output: 'edit_file: 未找到 old_string 匹配内容',
              isError: true,
            };
          }
          if (occurrences > 1 && !(replace_all ?? false)) {
            return {
              output: `edit_file: old_string 在文件中出现 ${occurrences} 次，请使用 replace_all=true 进行全部替换，或提供更唯一的 old_string`,
              isError: true,
            };
          }

          const newContent = replace_all
            ? content.replaceAll(old_string, new_string)
            : content.replace(old_string, new_string);

          try {
            await writeFile(filePath, newContent, 'utf8');
          } catch (error) {
            return {
              output: `写入文件失败: ${errorMessage(error)}`,
              isError: true,
            };
          }

          return {
            output: JSON.stringify(
              buildEditSummary(filePath, content, newContent, replace_all ? occurrences : 1),
            ),
          };
        },
      };
    },
  };
}

function countOccurrences(text: string, substring: string): number {
  if (substring === '') return 0;
  let count = 0;
  let pos = 0;
  while ((pos = text.indexOf(substring, pos)) !== -1) {
    count++;
    pos += substring.length;
  }
  return count;
}

function buildEditSummary(
  filePath: string,
  oldContent: string,
  newContent: string,
  replacements: number,
): Record<string, unknown> {
  const oldLines = oldContent.split('\n');
  const newLines = newContent.split('\n');
  const changedLines = countChangedLines(oldLines, newLines);
  const context = buildContextLines(oldLines, newLines);
  return {
    path: filePath,
    replacements,
    changed_lines: changedLines,
    context,
  };
}

function countChangedLines(oldLines: readonly string[], newLines: readonly string[]): number {
  const maxLen = Math.max(oldLines.length, newLines.length);
  let count = 0;
  for (let i = 0; i < maxLen; i++) {
    if (oldLines[i] !== newLines[i]) count++;
  }
  return count;
}

function buildContextLines(
  oldLines: readonly string[],
  newLines: readonly string[],
  radius = 2,
): string {
  const firstDiff = findFirstDiffLine(oldLines, newLines);
  if (firstDiff === -1) return '';
  const start = Math.max(0, firstDiff - radius);
  const end = Math.min(newLines.length, firstDiff + radius + 1);
  return newLines.slice(start, end).join('\n');
}

function findFirstDiffLine(oldLines: readonly string[], newLines: readonly string[]): number {
  const maxLen = Math.max(oldLines.length, newLines.length);
  for (let i = 0; i < maxLen; i++) {
    if (oldLines[i] !== newLines[i]) return i;
  }
  return -1;
}

export function createGlobFilesTool(
  workspace: WorkspaceConfig,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'glob_files',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = globFilesInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('glob_files', parsed.error);
      const rawPath = parsed.data.path ?? '.';
      const resolved = resolveWorkspacePath(rawPath, workspace, 'search');
      if (!resolved.ok) return resolved.result;
      const searchRoot = resolved.path;
      return {
        accesses: ToolAccesses.searchTree(searchRoot),
        approvalRule: `glob_files(${searchRoot})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const matches: Array<{ readonly path: string; readonly mtimeMs: number }> = [];
          try {
            await traverseForGlob(searchRoot, searchRoot, parsed.data.pattern, matches, new Set());
          } catch (error) {
            return {
              output: `遍历目录失败: ${errorMessage(error)}`,
              isError: true,
            };
          }
          matches.sort((a, b) => b.mtimeMs - a.mtimeMs);
          const truncated = matches.length > MAX_GLOB_RESULTS;
          const files = matches.slice(0, MAX_GLOB_RESULTS).map((m) => m.path);
          return { output: JSON.stringify({ files, truncated }) };
        },
      };
    },
  };
}

async function traverseForGlob(
  root: string,
  dir: string,
  pattern: string,
  matches: Array<{ readonly path: string; readonly mtimeMs: number }>,
  visited: Set<string>,
): Promise<void> {
  const realDir = safeRealpath(dir);
  if (realDir === undefined || visited.has(realDir)) return;
  visited.add(realDir);

  let dirHandle;
  try {
    dirHandle = await opendir(dir);
  } catch {
    return;
  }

  for await (const entry of dirHandle) {
    const entryPath = path.join(dir, entry.name);
    let targetPath = entryPath;

    if (entry.isSymbolicLink()) {
      const real = safeRealpath(entryPath);
      if (real === undefined || !isWithinDirectory(real, root)) continue;
      targetPath = real;
    }

    if (entry.isDirectory()) {
      await traverseForGlob(root, targetPath, pattern, matches, visited);
    } else if (entry.isFile() || entry.isSymbolicLink()) {
      const relativePath = path.relative(root, targetPath).replaceAll('\\', '/');
      if (relativePath !== '' && matchGlob(relativePath, pattern)) {
        try {
          const fileStat = await stat(targetPath);
          matches.push({ path: targetPath, mtimeMs: fileStat.mtimeMs });
        } catch {
          // Ignore files that disappear or cannot be stated.
        }
      }
    }
  }
}

function matchGlob(filePath: string, pattern: string): boolean {
  const regexes = globToRegexes(pattern);
  return regexes.some((re) => re.test(filePath));
}

function globToRegexes(pattern: string): RegExp[] {
  const alternatives = expandBraces(pattern);
  return alternatives.map((alt) => new RegExp(`^${convertGlobPattern(alt)}$`));
}

function expandBraces(pattern: string): string[] {
  const braceMatch = /\{([^{}]*)\}/.exec(pattern);
  if (!braceMatch) return [pattern];
  const prefix = pattern.slice(0, braceMatch.index);
  const suffix = pattern.slice(braceMatch.index + braceMatch[0].length);
  return braceMatch[1]!
    .split(',')
    .flatMap((option) => expandBraces(prefix + option + suffix));
}

function convertGlobPattern(pattern: string): string {
  if (pattern === '**') return '.*';

  let regex = pattern;
  const placeholders: string[] = [];
  function placeholder(replacement: string): string {
    const token = `<<${placeholders.length}>>`;
    placeholders.push(replacement);
    return token;
  }

  // Replace ** segments with placeholders so their regex fragments survive
  // the later wildcard conversion.
  regex = regex.replace(/^\*\*\//, () => placeholder('(?:[^/]+/)*'));
  regex = regex.replace(/\/\*\*$/, () => placeholder('(?:/[^/]+)*'));
  regex = regex.replace(/\/\*\*\//g, () => placeholder('(?:/[^/]+)*/'));

  // Escape regex metacharacters except glob wildcards and path separators.
  regex = regex.replace(/[.+^$()|[\]\\]/g, '\\$&');

  // Convert remaining single wildcards.
  regex = regex.replace(/\*/g, '[^/]*');
  regex = regex.replace(/\?/g, '[^/]');

  // Restore ** placeholders.
  for (let i = placeholders.length - 1; i >= 0; i--) {
    regex = regex.replaceAll(`<<${i}>>`, placeholders[i]!);
  }

  return regex;
}

export function createGrepFilesTool(
  workspace: WorkspaceConfig,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'grep_files',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = grepFilesInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('grep_files', parsed.error);
      const rawPath = parsed.data.path ?? '.';
      const resolved = resolveWorkspacePath(rawPath, workspace, 'search');
      if (!resolved.ok) return resolved.result;
      const searchRoot = resolved.path;

      let regex: RegExp;
      try {
        regex = new RegExp(parsed.data.pattern);
      } catch (error) {
        return {
          output: `正则表达式不合法: ${errorMessage(error)}`,
          isError: true,
        };
      }

      const includePattern = parsed.data.include;
      const contextLines = parsed.data.context_lines ?? 0;

      return {
        accesses: ToolAccesses.searchTree(searchRoot),
        approvalRule: `grep_files(${searchRoot})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const results: GrepResult[] = [];
          try {
            await traverseForGrep(
              searchRoot,
              searchRoot,
              regex,
              includePattern,
              contextLines,
              results,
              new Set(),
            );
          } catch (error) {
            return {
              output: `遍历目录失败: ${errorMessage(error)}`,
              isError: true,
            };
          }
          const truncated = results.length > MAX_GREP_RESULTS;
          return {
            output: JSON.stringify({
              results: results.slice(0, MAX_GREP_RESULTS),
              truncated,
            }),
          };
        },
      };
    },
  };
}

async function traverseForGrep(
  root: string,
  dir: string,
  regex: RegExp,
  includePattern: string | undefined,
  contextLines: number,
  results: GrepResult[],
  visited: Set<string>,
): Promise<void> {
  const realDir = safeRealpath(dir);
  if (realDir === undefined || visited.has(realDir)) return;
  visited.add(realDir);

  let dirHandle;
  try {
    dirHandle = await opendir(dir);
  } catch {
    return;
  }

  for await (const entry of dirHandle) {
    const entryPath = path.join(dir, entry.name);
    let targetPath = entryPath;

    if (entry.isSymbolicLink()) {
      const real = safeRealpath(entryPath);
      if (real === undefined || !isWithinDirectory(real, root)) continue;
      targetPath = real;
    }

    if (entry.isDirectory()) {
      await traverseForGrep(root, targetPath, regex, includePattern, contextLines, results, visited);
    } else if (entry.isFile() || entry.isSymbolicLink()) {
      if (includePattern !== undefined && !matchGlob(path.basename(targetPath), includePattern)) {
        continue;
      }
      await grepFile(targetPath, regex, contextLines, results);
    }
  }
}

async function grepFile(
  filePath: string,
  regex: RegExp,
  contextLines: number,
  results: GrepResult[],
): Promise<void> {
  let fileStat;
  try {
    fileStat = await stat(filePath);
  } catch {
    return;
  }
  if (!fileStat.isFile() || fileStat.size > MAX_GREP_FILE_BYTES) return;

  let content: string;
  try {
    content = await readFile(filePath, 'utf8');
  } catch {
    return;
  }
  if (isBinaryContent(content)) return;

  const lines = content.split('\n');
  const matchSet = new Set<number>();
  for (let i = 0; i < lines.length; i++) {
    if (regex.test(lines[i]!)) matchSet.add(i);
  }
  if (matchSet.size === 0) return;

  const ranges: Array<{ readonly start: number; readonly end: number }> = [];
  for (const lineIdx of matchSet) {
    const start = Math.max(0, lineIdx - contextLines);
    const end = Math.min(lines.length - 1, lineIdx + contextLines);
    if (ranges.length > 0 && start <= ranges[ranges.length - 1]!.end + 1) {
      ranges[ranges.length - 1] = {
        start: ranges[ranges.length - 1]!.start,
        end,
      };
    } else {
      ranges.push({ start, end });
    }
  }

  for (const range of ranges) {
    for (let i = range.start; i <= range.end; i++) {
      results.push({
        path: filePath,
        line: i + 1,
        text: lines[i]!,
        match: matchSet.has(i),
      });
    }
  }
}

function isBinaryContent(content: string): boolean {
  const limit = Math.min(content.length, BINARY_CHECK_BYTES);
  for (let i = 0; i < limit; i++) {
    if (content.charCodeAt(i) === 0) return true;
  }
  return false;
}

/** Create all three nav tools; description/parameters come from agent/config/toolset.json. */
export function createNavTools(
  workspace: WorkspaceConfig,
  lookup: (name: string) => ToolsetEntrySpec,
): ExecutableTool[] {
  return [
    createEditFileTool(workspace, lookup('edit_file')),
    createGlobFilesTool(workspace, lookup('glob_files')),
    createGrepFilesTool(workspace, lookup('grep_files')),
  ];
}
