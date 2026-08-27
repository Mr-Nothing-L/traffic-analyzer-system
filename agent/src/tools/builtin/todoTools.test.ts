/**
 * todo_write 单元测试:整表替换、状态渲染、参数校验、权限元数据。
 */
import { describe, expect, it } from 'vitest';

import { isRunnableToolExecution, type ExecutableToolResult } from '../contract';
import type { ToolsetEntrySpec } from './videoTools';
import { createTodoWriteTool } from './todoTools';

function spec(): ToolsetEntrySpec {
  return {
    description: '写入(整表替换)当前会话的任务清单,供模型组织多步工作。',
    parameters: {
      type: 'object',
      properties: {
        todos: {
          type: 'array',
          description: '任务清单,整表替换式写入。',
          items: {
            type: 'object',
            properties: {
              content: { type: 'string', description: '任务内容' },
              status: {
                type: 'string',
                enum: ['pending', 'in_progress', 'done'],
                description: '任务状态',
              },
            },
            required: ['content', 'status'],
          },
        },
      },
      required: ['todos'],
    },
  };
}

async function execute(rawInput: unknown): Promise<ExecutableToolResult> {
  const tool = createTodoWriteTool(spec());
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(rawInput);
  if (!isRunnableToolExecution(execution as never)) {
    return execution as ExecutableToolResult;
  }
  return execution.execute({ toolCallId: 'test-call', signal: new AbortController().signal });
}

describe('todo_write', () => {
  it('整表替换任务清单并返回紧凑渲染', async () => {
    const tool = createTodoWriteTool(spec());

    const first = await (tool.resolveExecution as (i: unknown) => unknown)({
      todos: [
        { content: '加载视频', status: 'done' },
        { content: '提取关键帧', status: 'in_progress' },
        { content: '提交检测结果', status: 'pending' },
      ],
    });
    if (!isRunnableToolExecution(first as never)) throw new Error('expected runnable');
    const firstResult = await first.execute({
      toolCallId: 'c1',
      signal: new AbortController().signal,
    });
    expect(firstResult.isError).toBeUndefined();
    expect(firstResult.output).toBe(
      '1. [x] 加载视频\n2. [~] 提取关键帧\n3. [ ] 提交检测结果',
    );

    // 第二次调用整表替换,旧状态不保留
    const second = await (tool.resolveExecution as (i: unknown) => unknown)({
      todos: [{ content: '收尾', status: 'done' }],
    });
    if (!isRunnableToolExecution(second as never)) throw new Error('expected runnable');
    const secondResult = await second.execute({
      toolCallId: 'c2',
      signal: new AbortController().signal,
    });
    expect(secondResult.output).toBe('1. [x] 收尾');
  });

  it('空清单渲染为提示语', async () => {
    const result = await execute({ todos: [] });
    expect(result.isError).toBeUndefined();
    expect(result.output).toBe('当前任务清单为空。');
  });

  it('参数不合法返回 isError', async () => {
    const result = await execute({ todos: [{ content: '', status: 'done' }] });
    expect(result.isError).toBe(true);
    expect(String(result.output)).toContain('参数不合法');
  });

  it('未知状态被拒绝', async () => {
    const result = await execute({ todos: [{ content: 'x', status: 'unknown' }] });
    expect(result.isError).toBe(true);
  });

  it('声明 accesses 为空且 approvalRule 固定', () => {
    const tool = createTodoWriteTool(spec());
    const execution = (tool.resolveExecution as (i: unknown) => unknown)({
      todos: [{ content: 'x', status: 'pending' }],
    });
    if (!isRunnableToolExecution(execution as never)) throw new Error('expected runnable');
    expect(execution.accesses).toEqual([]);
    expect(execution.approvalRule).toBe('todo_write');
  });

  it('不同工具实例的状态相互隔离', async () => {
    const a = createTodoWriteTool(spec());
    const b = createTodoWriteTool(spec());

    const execA = (a.resolveExecution as (i: unknown) => unknown)({
      todos: [{ content: 'a-task', status: 'done' }],
    });
    const execB = (b.resolveExecution as (i: unknown) => unknown)({
      todos: [{ content: 'b-task', status: 'in_progress' }],
    });
    if (!isRunnableToolExecution(execA as never) || !isRunnableToolExecution(execB as never)) {
      throw new Error('expected runnable');
    }

    expect((await execA.execute({ toolCallId: 'c1', signal: new AbortController().signal })).output).toBe(
      '1. [x] a-task',
    );
    expect((await execB.execute({ toolCallId: 'c2', signal: new AbortController().signal })).output).toBe(
      '1. [~] b-task',
    );
  });
});
