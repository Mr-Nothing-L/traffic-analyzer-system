/**
 * subagent_list / subagent_report: 查询本会话派生的子代理运行记录。
 *
 * 这两个工具共用同一个 SubagentRunRegistry 实例(由 server 装配处按 session
 * 构造并注入到 spawn_subagent 与本工具族)。本文件不持有任何全局态。
 */
import { z } from 'zod';

import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolContext,
  type ExecutableToolResult,
} from '../contract';
import { invalidInputResult } from './utils';
import type { SubagentRunRegistry } from './spawnSubagent';
import type { ToolsetEntrySpec } from './videoTools';

export const SUBAGENT_LIST_TOOL_NAME = 'subagent_list';
export const SUBAGENT_REPORT_TOOL_NAME = 'subagent_report';

export interface SubagentInfoToolsDeps {
  readonly registry: SubagentRunRegistry;
}

function renderList(registry: SubagentRunRegistry): string {
  const runs = registry.list();
  if (runs.length === 0) return '当前会话尚未派生任何子代理。';
  return runs
    .map((run) => {
      const task = run.task.length > 80 ? `${run.task.slice(0, 80)}…` : run.task;
      return `- ${run.id}: [${run.status}] ${task}`;
    })
    .join('\n');
}

export function createSubagentListTool(
  deps: SubagentInfoToolsDeps,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: SUBAGENT_LIST_TOOL_NAME,
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(_rawInput: unknown) {
      return {
        accesses: ToolAccesses.none(),
        approvalRule: 'subagent_list',
        execute: async (_ctx: ExecutableToolContext): Promise<ExecutableToolResult> => {
          return { output: renderList(deps.registry) };
        },
      };
    },
  };
}

const reportInputSchema = z.strictObject({
  id: z.string().min(1),
});

export function createSubagentReportTool(
  deps: SubagentInfoToolsDeps,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: SUBAGENT_REPORT_TOOL_NAME,
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = reportInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult(SUBAGENT_REPORT_TOOL_NAME, parsed.error);
      return {
        accesses: ToolAccesses.none(),
        approvalRule: `subagent_report(${parsed.data.id})`,
        execute: async (_ctx: ExecutableToolContext): Promise<ExecutableToolResult> => {
          const run = deps.registry.get(parsed.data.id);
          if (run === undefined) {
            return {
              output: `未找到 id 为 ${parsed.data.id} 的子代理运行记录。`,
              isError: true,
            };
          }
          const lines = [
            `id: ${run.id}`,
            `task: ${run.task}`,
            `status: ${run.status}`,
            ...(run.conclusion !== undefined ? [`conclusion: ${run.conclusion}`] : []),
            ...(run.steps !== undefined ? [`steps: ${run.steps}`] : []),
            ...(run.reason !== undefined ? [`reason: ${run.reason}`] : []),
            ...(run.error !== undefined ? [`error: ${run.error}`] : []),
          ];
          return { output: lines.join('\n') };
        },
      };
    },
  };
}
