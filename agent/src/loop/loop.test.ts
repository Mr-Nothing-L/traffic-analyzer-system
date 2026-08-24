/**
 * loop 模块单元测试:手写假 ChatProvider 按脚本依次返回流式 parts
 * (第 1 轮 tool_calls、第 2 轮纯文本等),不打真实模型 API。
 */
import { describe, expect, it, vi } from 'vitest';

import {
  createAssistantMessage,
  createToolMessage,
  createUserMessage,
  extractText,
  type Message,
  type StreamedMessagePart,
  type ToolCall,
} from '#/message';
import type {
  ChatProvider,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '#/provider';
import type { Tool } from '#/tool';

import { CallbackApprovalService } from '../permissions/approval';
import { PermissionGate } from '../permissions/gate';
import type { ApprovalResponse } from '../permissions/types';
import type { ExecutableTool, ExecutableToolResult } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import {
  DEFAULT_MAX_STEPS_PER_TURN,
  runAgentLoop,
  type AgentLoopEvent,
  type AgentLoopOptions,
} from './agentLoop';
import {
  compactMessages,
  createCompactionConfig,
  shouldCompact,
} from './compaction';

// ---------------------------------------------------------------------------
// 假 provider:按脚本逐轮返回 parts
// ---------------------------------------------------------------------------

class ScriptedProvider implements ChatProvider {
  readonly name = 'scripted';
  readonly modelName = 'scripted-model';
  readonly thinkingEffort = null;
  /** 每次 generate 调用收到的历史快照。 */
  readonly histories: Message[][] = [];
  private readonly script: StreamedMessagePart[][];

  constructor(script: StreamedMessagePart[][]) {
    this.script = [...script];
  }

  generate(
    _systemPrompt: string,
    _tools: Tool[],
    history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
    this.histories.push(history.map((m) => m));
    const parts = this.script.shift();
    if (parts === undefined) {
      return Promise.reject(new Error('script exhausted'));
    }
    return Promise.resolve(streamOf(parts));
  }

  withThinking(_effort: ThinkingEffort): ChatProvider {
    return this;
  }
}

function streamOf(parts: StreamedMessagePart[]): StreamedMessage {
  return {
    async *[Symbol.asyncIterator]() {
      for (const part of parts) yield part;
    },
    id: null,
    usage: null,
    finishReason: 'completed',
    rawFinishReason: 'stop',
  };
}

function toolCall(id: string, name: string, args: unknown): ToolCall {
  return { type: 'function', id, name, arguments: JSON.stringify(args) };
}

function text(text: string): StreamedMessagePart {
  return { type: 'text', text };
}

// ---------------------------------------------------------------------------
// 假工具 / gate / 选项组装
// ---------------------------------------------------------------------------

function echoTool(
  execute?: () => Promise<ExecutableToolResult>,
  name = 'echo',
): ExecutableTool {
  return {
    name,
    description: 'fake echo tool',
    parameters: { type: 'object' },
    resolveExecution: () => ({
      accesses: [],
      approvalRule: `${name}()`,
      execute: execute ?? (() => Promise.resolve({ output: 'echo-ok' })),
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

interface Harness {
  provider: ScriptedProvider;
  events: AgentLoopEvent[];
  run: (overrides?: Partial<AgentLoopOptions>) => ReturnType<typeof runAgentLoop>;
}

function harness(
  script: StreamedMessagePart[][],
  tools: ExecutableTool[],
  gate: PermissionGate = yoloGate(),
): Harness {
  const registry = new ToolRegistry();
  for (const tool of tools) registry.register(tool);
  const provider = new ScriptedProvider(script);
  const events: AgentLoopEvent[] = [];
  return {
    provider,
    events,
    run: (overrides = {}) =>
      runAgentLoop({
        provider,
        model: provider.modelName,
        systemPrompt: 'sys',
        registry,
        gate,
        messages: [createUserMessage('开始')],
        onEvent: (ev) => {
          events.push(ev);
        },
        ...overrides,
      }),
  };
}

function doneEvent(events: AgentLoopEvent[]): Extract<AgentLoopEvent, { type: 'done' }> {
  const ev = events.find((e) => e.type === 'done');
  if (ev === undefined || ev.type !== 'done') throw new Error('no done event');
  return ev;
}

function toolMessages(messages: readonly Message[]): Message[] {
  return messages.filter((m) => m.role === 'tool');
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('runAgentLoop', () => {
  it('多轮循环:tool_calls → 执行 → 结果回灌 → 纯文本完成', async () => {
    const h = harness(
      [[toolCall('c1', 'echo', { a: 1 })], [text('最终回答')]],
      [echoTool()],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    expect(result.steps).toBe(2);
    expect(h.provider.histories).toHaveLength(2);

    // 工具结果已回灌进第二轮的历史
    const secondHistory = h.provider.histories[1] ?? [];
    const fed = toolMessages(secondHistory);
    expect(fed).toHaveLength(1);
    expect(fed[0]?.toolCallId).toBe('c1');
    expect(extractText(fed[0] ?? createUserMessage(''))).toBe('echo-ok');

    // 完整消息序列:user → assistant(tc) → tool → assistant(text)
    expect(result.messages.map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
    ]);
    expect(extractText(result.messages[3] ?? createUserMessage(''))).toBe('最终回答');

    const types = h.events.map((e) => e.type);
    expect(types).toContain('tool_call_start');
    expect(types).toContain('tool_result');
    expect(types).toContain('text_delta');
    expect(types.filter((t) => t === 'step_done')).toHaveLength(2);
    expect(doneEvent(h.events).reason).toBe('completed');

    const toolResult = h.events.find((e) => e.type === 'tool_result');
    expect(toolResult).toMatchObject({ toolCallId: 'c1', name: 'echo', isError: false });
  });

  it('工具结果 stopTurn=true 时结束循环并携带该结果', async () => {
    const stopTool = echoTool(() =>
      Promise.resolve({ output: 'stop-out', stopTurn: true }),
    );
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('不应到达')]],
      [stopTool],
    );

    const result = await h.run();

    expect(result.reason).toBe('stop_turn');
    expect(result.stopResult?.output).toBe('stop-out');
    // 第二轮不应发生
    expect(h.provider.histories).toHaveLength(1);
    expect(doneEvent(h.events).stopResult?.output).toBe('stop-out');
  });

  it('maxStepsPerTurn 超限后停止', async () => {
    const h = harness(
      [
        [toolCall('c1', 'echo', {})],
        [toolCall('c2', 'echo', {})],
        [toolCall('c3', 'echo', {})],
      ],
      [echoTool()],
    );

    const result = await h.run({ maxStepsPerTurn: 2 });

    expect(result.reason).toBe('max_steps');
    expect(result.steps).toBe(2);
    expect(h.provider.histories).toHaveLength(2);
  });

  it('默认 maxStepsPerTurn 为 30', () => {
    expect(DEFAULT_MAX_STEPS_PER_TURN).toBe(30);
  });

  it('同一工具连续失败触发熔断,以 error 终止而非死循环到 max_steps', async () => {
    const failTool = echoTool(() =>
      Promise.resolve({ output: 'bad params', isError: true }),
    );
    const h = harness(
      [
        [toolCall('c1', 'echo', {})],
        [toolCall('c2', 'echo', {})],
        [toolCall('c3', 'echo', {})],
        [toolCall('c4', 'echo', {})],
        [toolCall('c5', 'echo', {})],
        [toolCall('c6', 'echo', {})],
      ],
      [failTool],
    );

    const result = await h.run();

    expect(result.reason).toBe('error');
    expect(result.error).toContain('连续 5 次失败');
    expect(result.steps).toBe(5);
    const done = doneEvent(h.events);
    expect(done.reason).toBe('error');
    expect(done.error).toContain('bad params');
  });

  it('工具成功后重置连续失败计数', async () => {
    let call = 0;
    const flakyTool = echoTool(() => {
      call += 1;
      return Promise.resolve(
        call % 2 === 0 ? { output: 'ok' } : { output: 'bad', isError: true },
      );
    });
    const h = harness(
      [
        [toolCall('c1', 'echo', {})],
        [toolCall('c2', 'echo', {})],
        [toolCall('c3', 'echo', {})],
        [toolCall('c4', 'echo', {})],
        [text('完成')],
      ],
      [flakyTool],
    );

    const result = await h.run({ maxConsecutiveToolErrors: 2 });

    expect(result.reason).toBe('completed');
  });

  it('权限 deny 时合成 isError 结果回灌,工具不执行', async () => {
    const execute = vi.fn(() => Promise.resolve({ output: 'should-not-run' }));
    const gate = new PermissionGate({
      mode: 'manual',
      approvalService: new CallbackApprovalService(() =>
        Promise.resolve({ decision: 'rejected', feedback: '不允许写' }),
      ),
    });
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('好的')]],
      [
        {
          name: 'echo',
          description: 'fake',
          parameters: { type: 'object' },
          resolveExecution: () => ({
            accesses: [{ kind: 'file' as const, operation: 'write' as const, path: '/out/f' }],
            approvalRule: 'echo(/out/f)',
            execute,
          }),
        },
      ],
      gate,
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    expect(execute).not.toHaveBeenCalled();
    const fed = toolMessages(h.provider.histories[1] ?? []);
    const fedText = extractText(fed[0] ?? createUserMessage(''));
    expect(fedText).toContain('denied');
    expect(fedText).toContain('不允许写');
    const toolResult = h.events.find((e) => e.type === 'tool_result');
    expect(toolResult).toMatchObject({ isError: true });
  });

  it('未注册工具容错:合成 isError 结果回灌,循环继续', async () => {
    const h = harness(
      [[toolCall('c1', 'ghost', {})], [text('收到')]],
      [echoTool()],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    const fed = toolMessages(h.provider.histories[1] ?? []);
    expect(extractText(fed[0] ?? createUserMessage(''))).toContain(
      'Tool "ghost" is not registered.',
    );
  });

  it('resolveExecution 返回错误结果(如沙盒 veto)时原样回灌', async () => {
    const vetoTool: ExecutableTool = {
      name: 'echo',
      description: 'fake',
      parameters: { type: 'object' },
      resolveExecution: () => ({ output: 'sandbox veto: PATH_OUTSIDE_WORKSPACE', isError: true }),
    };
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('明白')]],
      [vetoTool],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    const fed = toolMessages(h.provider.histories[1] ?? []);
    expect(extractText(fed[0] ?? createUserMessage(''))).toContain('sandbox veto');
  });

  it('工具执行超时:合成 isError 结果回灌', async () => {
    const slowTool: ExecutableTool = {
      name: 'echo',
      description: 'fake',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'echo()',
        execute: () =>
          new Promise<ExecutableToolResult>((resolve) => {
            setTimeout(() => resolve({ output: 'late' }), 200);
          }),
      }),
    };
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('超时了')]],
      [slowTool],
    );

    const result = await h.run({ toolTimeoutMs: 20 });

    expect(result.reason).toBe('completed');
    const fed = toolMessages(h.provider.histories[1] ?? []);
    expect(extractText(fed[0] ?? createUserMessage(''))).toContain('timed out');
    const toolResult = h.events.find((e) => e.type === 'tool_result');
    expect(toolResult).toMatchObject({ isError: true });
  });

  it('压缩触发:超阈后最老的工具结果在下一轮历史中被替换为占位', async () => {
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', 'x'.repeat(20_000)),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('结束')]],
      [echoTool(() => Promise.resolve({ output: 'small-ok' }))],
    );

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 1000, maxRecentMessages: 2 },
    });

    expect(result.reason).toBe('completed');
    const secondHistory = h.provider.histories[1] ?? [];
    const old = secondHistory.find((m) => m.toolCallId === 'old-1');
    expect(extractText(old ?? createUserMessage(''))).toBe('[已压缩]');
    // 保留区内的最近工具结果不受影响
    const recent = secondHistory.find((m) => m.toolCallId === 'c1');
    expect(extractText(recent ?? createUserMessage(''))).toBe('small-ok');
  });
});

describe('compaction', () => {
  const bigToolExchange = (id: string, size: number): Message[] => [
    createAssistantMessage([{ type: 'text', text: '调用工具' }], [toolCall(id, 'echo', {})]),
    createToolMessage(id, 'y'.repeat(size)),
  ];

  it('未超阈时不压缩', () => {
    const messages = [createUserMessage('hi'), ...bigToolExchange('t1', 100)];
    const config = createCompactionConfig(1_000_000);

    expect(shouldCompact(messages, config)).toBe(false);
    const outcome = compactMessages(messages, config);
    expect(outcome.compacted).toBe(false);
    expect(outcome.messages).toBe(messages);
  });

  it('超阈时替换压缩区工具结果,保留区以 user 消息开头', () => {
    const messages: Message[] = [
      createUserMessage('u1'),
      ...bigToolExchange('t1', 40_000),
      createAssistantMessage([{ type: 'text', text: 'a1' }]),
      createUserMessage('u2'),
      ...bigToolExchange('t2', 40_000),
      createAssistantMessage([{ type: 'text', text: 'a2' }]),
      createUserMessage('u3'),
      createAssistantMessage([{ type: 'text', text: 'a3' }]),
    ];
    const config = createCompactionConfig(1000, { maxRecentMessages: 4 });

    expect(shouldCompact(messages, config)).toBe(true);
    const outcome = compactMessages(messages, config);

    expect(outcome.compacted).toBe(true);
    // 保留区 = 从 u2 开始(maxRecentMessages=4 再向前扩展到 user 边界),
    // 压缩区 = [u1 .. a1],其中只有 t1 是工具结果。
    expect(outcome.compactedToolResults).toBe(1);
    const t1 = outcome.messages.find((m) => m.toolCallId === 't1');
    expect(extractText(t1 ?? createUserMessage(''))).toBe('[已压缩]');
    const t2 = outcome.messages.find((m) => m.toolCallId === 't2');
    expect(extractText(t2 ?? createUserMessage('')).length).toBe(40_000);
    // 不改动入参数组
    expect(extractText(messages.find((m) => m.toolCallId === 't1') ?? createUserMessage('')).length).toBe(40_000);
  });

  it('保留区边界:切点落在最近 user 消息之前,进行中的轮次不被压缩', () => {
    // 只有一个 user 轮次:即使超阈,压缩区为空,不做任何替换。
    const messages: Message[] = [
      createUserMessage('u1'),
      ...bigToolExchange('t1', 40_000),
      createAssistantMessage([{ type: 'text', text: 'a1' }]),
    ];
    const config = createCompactionConfig(1000, { maxRecentMessages: 4 });

    expect(shouldCompact(messages, config)).toBe(true);
    const outcome = compactMessages(messages, config);
    expect(outcome.compacted).toBe(false);
    expect(outcome.messages).toBe(messages);
  });
});
