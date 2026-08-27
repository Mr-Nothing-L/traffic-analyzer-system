/**
 * todo_write: 会话级任务清单,模型用来组织多步工作。
 *
 * 语义:
 * - 参数为整表替换:每次调用把会话内的任务清单完全替换为传入的数组。
 * - 状态仅保存在工具工厂闭包内,每会话一份(由 server / toolsFactory(session)
 *   为每个 session 独立构造工具实例)。
 * - 不读写磁盘,accesses 为空,approvalRule 固定为 'todo_write'。
 */
import { z } from 'zod';

import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolContext,
  type ExecutableToolResult,
} from '../contract';
import type { ToolsetEntrySpec } from './videoTools';
import { invalidInputResult } from './utils';

export const TODO_WRITE_TOOL_NAME = 'todo_write';

export type TodoStatus = 'pending' | 'in_progress' | 'done';

export interface TodoItem {
  readonly content: string;
  readonly status: TodoStatus;
}

const inputSchema = z.strictObject({
  todos: z.array(
    z.strictObject({
      content: z.string().min(1),
      status: z.enum(['pending', 'in_progress', 'done']),
    }),
  ),
});

function renderTodoList(todos: readonly TodoItem[]): string {
  if (todos.length === 0) return '当前任务清单为空。';
  const lines = todos.map((todo, index) => {
    const marker =
      todo.status === 'done' ? '[x]' : todo.status === 'in_progress' ? '[~]' : '[ ]';
    return `${index + 1}. ${marker} ${todo.content}`;
  });
  return lines.join('\n');
}

/**
 * 构造一个 todo_write 工具实例。每会话应单独调用一次,以保证任务清单
 * 隔离在不同 session 的闭包中。description / parameters 来自 toolset.json。
 */
export function createTodoWriteTool(spec: ToolsetEntrySpec): ExecutableTool {
  let todos: TodoItem[] = [];
  return {
    name: TODO_WRITE_TOOL_NAME,
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = inputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult(TODO_WRITE_TOOL_NAME, parsed.error);
      return {
        accesses: ToolAccesses.none(),
        approvalRule: 'todo_write',
        execute: async (_ctx: ExecutableToolContext): Promise<ExecutableToolResult> => {
          todos = parsed.data.todos.map((item) => ({
            content: item.content,
            status: item.status,
          }));
          return { output: renderTodoList(todos) };
        },
      };
    },
  };
}
