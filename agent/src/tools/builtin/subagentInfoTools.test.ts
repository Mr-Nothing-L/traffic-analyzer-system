/**
 * 子代理运行注册表 + subagent_list / subagent_report 工具单元测试。
 *
 * 覆盖:注册表增删改查、spawn_subagent 执行时登记、list/report 工具渲染、
 * 结论从 payload 通道读取(D1 之后不再解析 note)。
 */
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ChatProvider } from '../../llm/kosong';
import { CallbackApprovalService } from '../../permissions/approval';
import { PermissionGate } from '../../permissions/gate';
import type { ApprovalResponse } from '../../permissions/types';
import {
  isRunnableToolExecution,
  type ExecutableTool,
  type ExecutableToolContext,
  type ExecutableToolResult,
  type ToolExecution,
} from '../contract';
import { ToolRegistry } from '../registry';
import {
  createSpawnSubagentTool,
  createSubagentRunRegistry,
  SUBAGENT_MAX_STEPS,
  type SpawnSubagentDeps,
  type SubagentRunRegistry,
} from './spawnSubagent';
import {
  createSubagentListTool,
  createSubagentReportTool,
} from './subagentInfoTools';
import { ScriptedProvider, text, toolCall } from '../../testkit/scriptedProvider';

let workspaceDir: string;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'subagent-info-test-'));
});

afterEach(() => {
  rmSync(workspaceDir, { recursive: true, force: true });
});

const autoApprove = (): Promise<ApprovalResponse> => Promise.resolve({ decision: 'approved' });

function yoloGate(): PermissionGate {
  return new PermissionGate({
    mode: 'yolo',
    approvalService: new CallbackApprovalService(autoApprove),
  });
}

function echoTool(): ExecutableTool {
  return {
    name: 'echo',
    description: 'fake echo tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: 'echo()',
      execute: () => Promise.resolve({ output: 'echo-ok' }),
    }),
  };
}

function submitTool(payload: unknown): ExecutableTool {
  return {
    name: 'submit_detection',
    description: 'fake submit tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: 'submit_detection',
      execute: (): Promise<ExecutableToolResult> =>
        Promise.resolve({ output: '检测结果已提交', stopTurn: true, payload }),
    }),
  };
}

function makeSpawnTool(
  registry: SubagentRunRegistry,
  provider: ChatProvider,
  extraTools: ExecutableTool[] = [],
): ExecutableTool {
  const parentRegistry = new ToolRegistry();
  parentRegistry.register(echoTool());
  for (const tool of extraTools) parentRegistry.register(tool);
  const deps: SpawnSubagentDeps = {
    parentRegistry,
    workspace: { workspaceDir, additionalDirs: [] },
    providerFactory: () => ({ provider, model: provider.modelName }),
    gate: yoloGate(),
    systemPrompt: 'sys',
    registry,
  };
  return createSpawnSubagentTool(deps);
}

function resolveExecutionSync(tool: ExecutableTool, input: unknown): ToolExecution {
  return (tool.resolveExecution as (i: unknown) => ToolExecution)(input);
}

async function executeSpawn(
  tool: ExecutableTool,
  input: unknown,
  ctx: ExecutableToolContext = { toolCallId: 'parent-call-1', signal: new AbortController().signal },
): Promise<ExecutableToolResult> {
  const execution = resolveExecutionSync(tool, input);
  if (!isRunnableToolExecution(execution)) return execution;
  return execution.execute(ctx);
}

async function execute(tool: ExecutableTool, input: unknown): Promise<ExecutableToolResult> {
  const execution = resolveExecutionSync(tool, input);
  if (!isRunnableToolExecution(execution)) return execution;
  return execution.execute({ toolCallId: 'test-call', signal: new AbortController().signal });
}

function spec(name: string): { description: string; parameters: Record<string, unknown> } {
  return { description: `desc:${name}`, parameters: { type: 'object' } };
}

describe('createSubagentRunRegistry', () => {
  it('register 后状态为 running', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: 'a'.repeat(200) });
    const record = registry.get('r1');
    expect(record).toBeDefined();
    expect(record?.status).toBe('running');
    expect(record?.task).toHaveLength(120);
  });

  it('complete 把 completed 结果标记为 done', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: 't' });
    registry.complete('r1', {
      reason: 'completed',
      messages: [],
      steps: 3,
      truncated: false,
    });
    const record = registry.get('r1');
    expect(record?.status).toBe('done');
    expect(record?.steps).toBe(3);
    expect(record?.reason).toBe('completed');
    expect(record?.conclusion).toBe('子代理已完成,但未产生文本结论。');
  });

  it('complete 从 stop_turn 的 payload 生成结论', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: '检测' });
    registry.complete('r1', {
      reason: 'stop_turn',
      messages: [],
      steps: 2,
      truncated: false,
      stopResult: {
        output: 'ok',
        stopTurn: true,
        payload: {
          binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
          normal: false,
          report_markdown: '检测到抛洒物。',
        },
      },
    });
    const record = registry.get('r1');
    expect(record?.status).toBe('done');
    expect(record?.conclusion).toContain('1_0_0_0_0_0_0_0_0_0_0');
    expect(record?.conclusion).toContain('检测到抛洒物');
  });

  it('complete 把 error 结果标记为 failed', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: 't' });
    registry.complete('r1', {
      reason: 'error',
      messages: [],
      steps: 1,
      truncated: false,
      error: 'LLM boom',
    });
    const record = registry.get('r1');
    expect(record?.status).toBe('failed');
    expect(record?.error).toBeUndefined();
    expect(record?.conclusion).toContain('LLM boom');
  });

  it('fail 记录错误与原因', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: 't' });
    registry.fail('r1', { error: '用户取消', reason: 'cancelled', steps: 5 });
    const record = registry.get('r1');
    expect(record?.status).toBe('failed');
    expect(record?.error).toBe('用户取消');
    expect(record?.reason).toBe('cancelled');
    expect(record?.steps).toBe(5);
  });

  it('timeout 设置超时状态', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: 't' });
    registry.timeout('r1', 12);
    const record = registry.get('r1');
    expect(record?.status).toBe('timeout');
    expect(record?.reason).toBe('timeout');
    expect(record?.steps).toBe(12);
  });

  it('list 返回所有记录', () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'r1', task: 't1' });
    registry.register({ id: 'r2', task: 't2' });
    registry.complete('r1', { reason: 'completed', messages: [], steps: 1, truncated: false });
    expect(registry.list()).toHaveLength(2);
    expect(registry.list().map((r) => r.id).sort()).toEqual(['r1', 'r2']);
  });
});

describe('spawn_subagent with registry', () => {
  it('执行前后在注册表中留下 running → done 记录', async () => {
    const registry = createSubagentRunRegistry();
    const provider = new ScriptedProvider({ script: [[text('子代理结论')]] });
    const tool = makeSpawnTool(registry, provider);

    expect(registry.list()).toHaveLength(0);

    const result = await executeSpawn(tool, { task: '分析视频' });

    expect(result.isError).toBeUndefined();
    expect(registry.list()).toHaveLength(1);
    const record = registry.list()[0];
    expect(record?.status).toBe('done');
    expect(record?.task).toBe('分析视频');
    expect(record?.conclusion).toBe('子代理结论');
    expect(record?.steps).toBe(1);
    expect(record?.reason).toBe('completed');
  });

  it('stop_turn 结论走 payload 通道', async () => {
    const registry = createSubagentRunRegistry();
    const payload = {
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: true,
      report_markdown: '全片正常。',
    };
    const provider = new ScriptedProvider({ script: [[toolCall('s1', 'submit_detection', {})]] });
    const tool = makeSpawnTool(registry, provider, [submitTool(payload)]);

    const result = await executeSpawn(tool, { task: '检测' });

    expect(result.isError).toBeUndefined();
    expect(registry.list()).toHaveLength(1);
    const record = registry.list()[0];
    expect(record?.status).toBe('done');
    expect(record?.reason).toBe('stop_turn');
    expect(record?.conclusion).toContain('0_0_0_0_0_0_0_0_1_0_0');
    expect(record?.conclusion).toContain('全片正常');
  });

  it('max_steps 记录步数与原因', async () => {
    const registry = createSubagentRunRegistry();
    const provider = new ScriptedProvider({
      script: Array.from({ length: SUBAGENT_MAX_STEPS }, (_, i) => [
        toolCall(`c${i}`, 'echo', {}),
      ]),
    });
    const tool = makeSpawnTool(registry, provider);

    await executeSpawn(tool, { task: '循环任务' });

    const record = registry.list()[0];
    expect(record?.status).toBe('done');
    expect(record?.reason).toBe('max_steps');
    expect(record?.steps).toBe(SUBAGENT_MAX_STEPS);
  });

  it('provider 异常导致 failed', async () => {
    const registry = createSubagentRunRegistry();
    const provider = new ScriptedProvider({ script: [new Error('boom')] });
    const tool = makeSpawnTool(registry, provider);

    await executeSpawn(tool, { task: '会炸的任务' });

    const record = registry.list()[0];
    expect(record?.status).toBe('failed');
    expect(record?.reason).toBe('error');
    expect(record?.conclusion).toContain('boom');
  });
});

describe('subagent_list', () => {
  it('返回注册表中所有记录的紧凑列表', async () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'run-1', task: '任务一' });
    registry.complete('run-1', { reason: 'completed', messages: [], steps: 2, truncated: false });

    const tool = createSubagentListTool({ registry }, spec('subagent_list'));
    const result = await execute(tool, {});

    expect(result.isError).toBeUndefined();
    expect(result.output).toContain('run-1');
    expect(result.output).toContain('[done]');
    expect(result.output).toContain('任务一');
  });

  it('空注册表返回提示语', async () => {
    const tool = createSubagentListTool({ registry: createSubagentRunRegistry() }, spec('subagent_list'));
    const result = await execute(tool, {});
    expect(result.output).toBe('当前会话尚未派生任何子代理。');
  });

  it('accesses 为空且 approvalRule 固定', () => {
    const tool = createSubagentListTool({ registry: createSubagentRunRegistry() }, spec('subagent_list'));
    const execution = resolveExecutionSync(tool, {});
    if (!isRunnableToolExecution(execution)) throw new Error('expected runnable');
    expect(execution.accesses).toEqual([]);
    expect(execution.approvalRule).toBe('subagent_list');
  });
});

describe('subagent_report', () => {
  it('按 id 返回子代理结论、步数与停止原因', async () => {
    const registry = createSubagentRunRegistry();
    registry.register({ id: 'run-x', task: '深度检测' });
    registry.complete('run-x', {
      reason: 'stop_turn',
      messages: [],
      steps: 4,
      truncated: false,
      stopResult: {
        output: 'ok',
        stopTurn: true,
        payload: {
          binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
          normal: false,
          report_markdown: '检测到异常停车。',
        },
      },
    });

    const tool = createSubagentReportTool({ registry }, spec('subagent_report'));
    const result = await execute(tool, { id: 'run-x' });

    expect(result.isError).toBeUndefined();
    expect(result.output).toContain('id: run-x');
    expect(result.output).toContain('status: done');
    expect(result.output).toContain('steps: 4');
    expect(result.output).toContain('reason: stop_turn');
    expect(result.output).toContain('检测到异常停车');
  });

  it('id 不存在返回 isError', async () => {
    const tool = createSubagentReportTool({ registry: createSubagentRunRegistry() }, spec('subagent_report'));
    const result = await execute(tool, { id: 'missing' });
    expect(result.isError).toBe(true);
    expect(String(result.output)).toContain('未找到');
  });

  it('id 为空字符串被拒绝', async () => {
    const tool = createSubagentReportTool({ registry: createSubagentRunRegistry() }, spec('subagent_report'));
    const result = await execute(tool, { id: '' });
    expect(result.isError).toBe(true);
  });

  it('approvalRule 包含 id', () => {
    const tool = createSubagentReportTool({ registry: createSubagentRunRegistry() }, spec('subagent_report'));
    const execution = resolveExecutionSync(tool, { id: 'run-1' });
    if (!isRunnableToolExecution(execution)) throw new Error('expected runnable');
    expect(execution.approvalRule).toBe('subagent_report(run-1)');
  });
});
