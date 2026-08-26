/**
 * spawn_subagent 工具单元测试:假 ChatProvider 按角色(父/子 loop)脚本
 * 应答,不打真实模型 API、不碰真实 toolserver。
 *
 * 覆盖:子 loop 执行与结论回传、subagent_event 转发(过滤 context_usage/
 * compaction)、禁递归(子 registry 无 spawn_subagent)、并发上限 4(第 5 个
 * 排队)、max_steps / 失败容错、submit_detection 结论提取、video_path 直传
 * 与沙盒硬否决。
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type {
  Message,
  StreamedMessagePart,
  ToolCall,
  ChatProvider,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
  Tool,
  TokenUsage,
} from '../../llm/kosong';
import {
  ScriptedProvider,
  streamOf,
  text,
  toolCall,
} from '../../testkit/scriptedProvider';
import type { AgentLoopEvent } from '../../loop/agentLoop';
import { CallbackApprovalService } from '../../permissions/approval';
import { PermissionGate } from '../../permissions/gate';
import type { ApprovalResponse } from '../../permissions/types';
import {
  isRunnableToolExecution,
  type ExecutableTool,
  type ExecutableToolContext,
  type ExecutableToolResult,
  type RunnableToolExecution,
  type ToolExecution,
} from '../contract';
import { ToolRegistry } from '../registry';
import {
  createSpawnSubagentTool,
  SUBAGENT_MAX_STEPS,
  SUBAGENT_TIMEOUT_MS,
  type SpawnSubagentDeps,
} from './spawnSubagent';

// ---------------------------------------------------------------------------
// 假 provider:按脚本逐轮返回 parts;记录每次 generate 的 tools/history
// ---------------------------------------------------------------------------

/** 阻塞型 provider:每次 generate 计数并挂起,直到 release()。 */
class BlockingProvider implements ChatProvider {
  readonly name = 'blocking';
  readonly modelName = 'blocking-model';
  readonly thinkingEffort = null;
  started = 0;
  private releaseFn!: () => void;
  private readonly gate = new Promise<void>((resolve) => {
    this.releaseFn = resolve;
  });
  private readonly onStarted: () => void;

  constructor(onStarted: () => void) {
    this.onStarted = onStarted;
  }

  generate(): Promise<StreamedMessage> {
    this.started += 1;
    this.onStarted();
    return this.gate.then(() => streamOf([text('子代理结论')]));
  }

  release(): void {
    this.releaseFn();
  }

  withThinking(_effort: ThinkingEffort): ChatProvider {
    return this;
  }
}

function streamOf(parts: StreamedMessagePart[], usage: TokenUsage | null = null): StreamedMessage {
  return {
    async *[Symbol.asyncIterator]() {
      for (const part of parts) yield part;
    },
    id: null,
    usage,
    finishReason: 'completed',
    rawFinishReason: 'stop',
  };
}

function toolCall(id: string, name: string, args: unknown): ToolCall {
  return { type: 'function', id, name, arguments: JSON.stringify(args) };
}

function text(value: string): StreamedMessagePart {
  return { type: 'text', text: value };
}

// ---------------------------------------------------------------------------
// 假工具 / 依赖组装
// ---------------------------------------------------------------------------

function echoTool(name = 'echo'): ExecutableTool {
  return {
    name,
    description: 'fake echo tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: `${name}()`,
      execute: () => Promise.resolve({ output: 'echo-ok' }),
    }),
  };
}

/** 模拟 submit_detection:stopTurn + payload 携带结构化检测载荷。 */
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

/** 模拟只 stopTurn 不带 payload 的退化 stop 工具(契约允许,但消费方应报缺)。 */
function bareStopTool(): ExecutableTool {
  return {
    name: 'submit_detection',
    description: 'fake bare stop tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: 'submit_detection',
      execute: (): Promise<ExecutableToolResult> =>
        Promise.resolve({ output: '已停止', stopTurn: true }),
    }),
  };
}

const autoApprove = (): Promise<ApprovalResponse> => Promise.resolve({ decision: 'approved' });

function yoloGate(): PermissionGate {
  return new PermissionGate({
    mode: 'yolo',
    approvalService: new CallbackApprovalService(autoApprove),
  });
}

let workspaceDir: string;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'spawn-subagent-test-'));
});

afterEach(() => {
  rmSync(workspaceDir, { recursive: true, force: true });
});

interface Harness {
  tool: ExecutableTool;
  parentRegistry: ToolRegistry;
}

function makeHarness(
  provider: ChatProvider,
  parentTools: ExecutableTool[],
  overrides: Partial<SpawnSubagentDeps> = {},
): Harness {
  const parentRegistry = new ToolRegistry();
  for (const tool of parentTools) parentRegistry.register(tool);
  const deps: SpawnSubagentDeps = {
    parentRegistry,
    workspace: { workspaceDir, additionalDirs: [] },
    providerFactory: () => ({ provider, model: provider.modelName }),
    gate: yoloGate(),
    systemPrompt: 'sys',
    ...overrides,
  };
  const tool = createSpawnSubagentTool(deps);
  parentRegistry.register(tool);
  return { tool, parentRegistry };
}

function makeCtx(onSubagentEvent?: (event: unknown) => void): ExecutableToolContext {
  return {
    toolCallId: 'parent-call-1',
    signal: new AbortController().signal,
    ...(onSubagentEvent !== undefined ? { onSubagentEvent } : {}),
  };
}

/** spawn 工具的 resolveExecution 是同步的;按 builtin.test.ts 的惯例收窄类型。 */
function resolveExecutionSync(tool: ExecutableTool, input: unknown): ToolExecution {
  return (tool.resolveExecution as (i: unknown) => ToolExecution)(input);
}

async function executeSpawn(
  tool: ExecutableTool,
  input: unknown,
  ctx: ExecutableToolContext = makeCtx(),
): Promise<ExecutableToolResult> {
  const execution = resolveExecutionSync(tool, input);
  if (!isRunnableToolExecution(execution)) return execution;
  return execution.execute(ctx);
}

function resolveSpawn(tool: ExecutableTool, input: unknown): RunnableToolExecution {
  const execution = resolveExecutionSync(tool, input);
  if (!isRunnableToolExecution(execution)) {
    throw new Error(`expected runnable, got ${JSON.stringify(execution)}`);
  }
  return execution;
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('spawn_subagent', () => {
  it('执行子 loop 并回传最终文本结论;note 记录 reason/steps', async () => {
    const provider = new ScriptedProvider({ script: [[text('子代理结论:视频正常')]] });
    const { tool } = makeHarness(provider, [echoTool()]);

    const result = await executeSpawn(tool, { task: '分析这段视频' });

    expect(result.isError).toBeUndefined();
    expect(result.output).toBe('子代理结论:视频正常');
    expect(JSON.parse(result.note ?? '{}')).toEqual({ reason: 'completed', steps: 1 });

    // 子 loop 首条 user 消息 = task 文本
    const firstHistory = provider.histories[0] ?? [];
    expect(firstHistory).toHaveLength(1);
    expect(firstHistory[0]?.role).toBe('user');
  });

  it('resolveExecution 声明 600s 超时;参数不合法返回 isError', () => {
    const provider = new ScriptedProvider({ script: [] });
    const { tool } = makeHarness(provider, [echoTool()]);

    expect(resolveSpawn(tool, { task: 't' }).timeoutMs).toBe(SUBAGENT_TIMEOUT_MS);
    expect(SUBAGENT_TIMEOUT_MS).toBe(600_000);

    const invalid = resolveExecutionSync(tool, {});
    expect(isRunnableToolExecution(invalid)).toBe(false);
    if (!isRunnableToolExecution(invalid)) {
      expect(invalid.isError).toBe(true);
    }
  });

  it('禁递归:子 loop 的工具集不含 spawn_subagent', async () => {
    const provider = new ScriptedProvider({ script: [[text('done')]] });
    const { tool } = makeHarness(provider, [echoTool()]);

    await executeSpawn(tool, { task: 't' });

    const childTools = provider.toolsPerCall[0] ?? [];
    expect(childTools.map((t) => t.name)).toContain('echo');
    expect(childTools.map((t) => t.name)).not.toContain('spawn_subagent');
  });

  it('子 loop 事件经 ctx.onSubagentEvent 转发,过滤 context_usage', async () => {
    const usage: TokenUsage = { inputOther: 100, output: 10, inputCacheRead: 0, inputCacheCreation: 0 };
    const provider = new ScriptedProvider({ script: [[text('结论')]], usages: [usage] });
    const { tool } = makeHarness(provider, [echoTool()], { contextTokens: 100_000 });

    const forwarded: AgentLoopEvent[] = [];
    const result = await executeSpawn(
      tool,
      { task: 't' },
      makeCtx((ev) => {
        forwarded.push(ev as AgentLoopEvent);
      }),
    );

    expect(result.isError).toBeUndefined();
    const types = forwarded.map((e) => e.type);
    expect(types).toContain('text_delta');
    expect(types).toContain('step_done');
    expect(types).toContain('done');
    expect(types).not.toContain('context_usage');
    expect(types).not.toContain('compaction');
  });

  it('并发上限 4:5 个并发 spawn 时第 5 个排队,释放后补位', async () => {
    let fourthReached!: () => void;
    const fourStarted = new Promise<void>((resolve) => {
      fourthReached = resolve;
    });
    const provider = new BlockingProvider(() => {
      if (provider.started === 4) fourthReached();
    });
    const { tool } = makeHarness(provider, [echoTool()]);

    const runs = Array.from({ length: 5 }, (_, i) =>
      executeSpawn(tool, { task: `task-${i}` }),
    );

    await fourStarted;
    // 给第 5 个一个事件循环窗口:不应启动(信号量排队)
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(provider.started).toBe(4);

    provider.release();
    const results = await Promise.all(runs);
    expect(provider.started).toBe(5);
    for (const result of results) {
      expect(result.isError).toBeUndefined();
      expect(result.output).toBe('子代理结论');
    }
  });

  it('子 loop 失败容错:provider 异常 → isError 结果说明原因,不抛出', async () => {
    const provider = new ScriptedProvider({ script: [new Error('LLM boom')] });
    const { tool } = makeHarness(provider, [echoTool()]);

    const result = await executeSpawn(tool, { task: 't' });

    expect(result.isError).toBe(true);
    expect(String(result.output)).toContain('子代理执行失败');
    expect(String(result.output)).toContain('LLM boom');
    expect(JSON.parse(result.note ?? '{}')).toMatchObject({ reason: 'error' });
  });

  it('子 loop 达到步数上限:output 说明原因,不视为工具错误', async () => {
    // 子代理每轮都调 echo,永不收敛 → max_steps(12)
    const provider = new ScriptedProvider({ script: Array.from({ length: SUBAGENT_MAX_STEPS }, (_, i) => [toolCall(`c${i}`, 'echo', {})]) });
    const { tool } = makeHarness(provider, [echoTool()]);

    const result = await executeSpawn(tool, { task: 't' });

    expect(result.isError).toBeUndefined();
    expect(String(result.output)).toContain('步数上限');
    expect(JSON.parse(result.note ?? '{}')).toEqual({
      reason: 'max_steps',
      steps: SUBAGENT_MAX_STEPS,
    });
  });

  it('子代理调用 submit_detection:output 明确告知编码与结论(读 payload)', async () => {
    const payload = {
      events: [],
      binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '检测到抛洒物,位置在车道中央。',
    };
    const provider = new ScriptedProvider({ script: [[toolCall('s1', 'submit_detection', {})]] });
    const { tool } = makeHarness(provider, [echoTool(), submitTool(payload)]);

    const result = await executeSpawn(tool, { task: '检测事件' });

    expect(result.isError).toBeUndefined();
    expect(String(result.output)).toContain('submit_detection');
    expect(String(result.output)).toContain('1_0_0_0_0_0_0_0_0_0_0');
    expect(String(result.output)).toContain('检测到抛洒物');
    expect(JSON.parse(result.note ?? '{}')).toMatchObject({ reason: 'stop_turn' });
  });

  it('stop_turn 缺 payload:明确报缺失,不再静默回退读 note', async () => {
    const provider = new ScriptedProvider({ script: [[toolCall('s1', 'submit_detection', {})]] });
    const { tool } = makeHarness(provider, [echoTool(), bareStopTool()]);

    const result = await executeSpawn(tool, { task: '检测事件' });

    expect(result.isError).toBeUndefined();
    expect(String(result.output)).toContain('未携带结构化 payload');
  });

  it('video_path:读文件转 video_url dataURL 随首条 user 消息直传', async () => {
    const videoBytes = Buffer.from('fake-video-bytes');
    writeFileSync(path.join(workspaceDir, 'clip.mp4'), videoBytes);
    const provider = new ScriptedProvider({ script: [[text('ok')]] });
    const { tool } = makeHarness(provider, [echoTool()]);

    const result = await executeSpawn(tool, {
      task: '分析',
      video_path: 'clip.mp4',
    });

    expect(result.isError).toBeUndefined();
    const firstMessage = provider.histories[0]?.[0];
    const videoPart = firstMessage?.content.find((p) => p.type === 'video_url');
    expect(videoPart).toBeDefined();
    if (videoPart?.type === 'video_url') {
      expect(videoPart.videoUrl.url).toBe(
        `data:video/mp4;base64,${videoBytes.toString('base64')}`,
      );
    }
  });

  it('video_path 越出沙盒:resolveExecution 硬否决为 isError', () => {
    const provider = new ScriptedProvider({ script: [] });
    const { tool } = makeHarness(provider, [echoTool()]);

    const execution = resolveExecutionSync(tool, { task: 't', video_path: '/etc/passwd' });
    expect(isRunnableToolExecution(execution)).toBe(false);
    if (!isRunnableToolExecution(execution)) {
      expect(execution.isError).toBe(true);
      expect(String(execution.output)).toContain('沙盒');
    }
  });
});
