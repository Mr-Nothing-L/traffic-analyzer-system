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
  FinishReason,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '#/provider';
import type { Tool } from '#/tool';
import type { TokenUsage } from '#/usage';

import { CallbackApprovalService } from '../permissions/approval';
import { PermissionGate } from '../permissions/gate';
import type { ApprovalResponse } from '../permissions/types';
import type { ExecutableTool, ExecutableToolResult } from '../tools/contract';
import { ToolRegistry } from '../tools/registry';

import {
  DEFAULT_MAX_STEPS_PER_TURN,
  TRUNCATED_TOOL_CALL_MESSAGE,
  runAgentLoop,
  type AgentLoopEvent,
  type AgentLoopOptions,
  type StepPersistUpdate,
} from './agentLoop';
import { compactMessages, createCompactionConfig, isOverContextByUsage } from './compaction';
import { SUMMARY_PREFIX, SUMMARY_SYSTEM_PROMPT } from './summarize';

// ---------------------------------------------------------------------------
// 假 provider:按脚本逐轮返回 parts
// ---------------------------------------------------------------------------

class ScriptedProvider implements ChatProvider {
  readonly name = 'scripted';
  readonly modelName = 'scripted-model';
  readonly thinkingEffort = null;
  /** 每次 generate 调用收到的历史快照(不含摘要调用)。 */
  readonly histories: Message[][] = [];
  /** 摘要调用收到的 tools(用于断言不传 tools)。 */
  readonly summaryTools: Tool[][] = [];
  /** withThinking 收到的 effort(摘要关思考回退路径断言用)。 */
  readonly thinkingEfforts: ThinkingEffort[] = [];
  private readonly script: StreamedMessagePart[][];
  /** 与 script 逐步对应的 usage(缺省 null = provider 不上报)。 */
  private readonly usages: (TokenUsage | null)[];
  /** 与 script 逐步对应的 finishReason(缺省 'completed')。 */
  private readonly finishReasons: (FinishReason | null)[];
  /** 摘要调用的应答队列;Error = 摘要失败(测回退);队列空 = 默认失败。 */
  private readonly summaries: (StreamedMessagePart[] | Error)[];

  constructor(
    script: StreamedMessagePart[][],
    usages: (TokenUsage | null)[] = [],
    summaries: (StreamedMessagePart[] | Error)[] = [],
    finishReasons: (FinishReason | null)[] = [],
  ) {
    this.script = [...script];
    this.usages = [...usages];
    this.summaries = [...summaries];
    this.finishReasons = [...finishReasons];
  }

  generate(
    systemPrompt: string,
    tools: Tool[],
    history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
    // 按调用内容分场景:摘要调用(system prompt 为摘要指令)走 summaries 队列。
    if (systemPrompt === SUMMARY_SYSTEM_PROMPT) {
      this.summaryTools.push(tools);
      const summary = this.summaries.shift();
      if (summary === undefined) return Promise.reject(new Error('summary not scripted'));
      if (summary instanceof Error) return Promise.reject(summary);
      return Promise.resolve(streamOf(summary));
    }
    this.histories.push(history.map((m) => m));
    const parts = this.script.shift();
    if (parts === undefined) {
      return Promise.reject(new Error('script exhausted'));
    }
    return Promise.resolve(
      streamOf(parts, this.usages.shift() ?? null, this.finishReasons.shift() ?? 'completed'),
    );
  }

  withThinking(effort: ThinkingEffort): ChatProvider {
    this.thinkingEfforts.push(effort);
    return this;
  }
}

function streamOf(
  parts: StreamedMessagePart[],
  usage: TokenUsage | null = null,
  finishReason: FinishReason | null = 'completed',
): StreamedMessage {
  return {
    async *[Symbol.asyncIterator]() {
      for (const part of parts) yield part;
    },
    id: null,
    usage,
    finishReason,
    rawFinishReason: finishReason === 'truncated' ? 'length' : 'stop',
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
  usages: (TokenUsage | null)[] = [],
  summaries: (StreamedMessagePart[] | Error)[] = [],
  finishReasons: (FinishReason | null)[] = [],
): Harness {
  const registry = new ToolRegistry();
  for (const tool of tools) registry.register(tool);
  const provider = new ScriptedProvider(script, usages, summaries, finishReasons);
  const events: AgentLoopEvent[] = [];
  return {
    provider,
    events,
    run: (overrides = {}) =>
      runAgentLoop({
        provider,
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

  it('压缩触发(真实 usage 超阈):最老的工具结果在下一轮历史中被替换为占位', async () => {
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
      yoloGate(),
      // 第一步真实 usage 900 ≥ 1000 × 0.85 → 第二步前触发压缩
      [{ inputOther: 900, inputCacheRead: 0, inputCacheCreation: 0, output: 0 }],
    );

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 1000, maxRecentMessages: 2 },
    });

    expect(result.reason).toBe('completed');
    // 摘要队列空 → 摘要调用默认失败 → 回退占位替换
    const compaction = h.events.find((e) => e.type === 'compaction');
    expect(compaction).toMatchObject({ compactedToolResults: 1, summarized: false });
    const secondHistory = h.provider.histories[1] ?? [];
    const old = secondHistory.find((m) => m.toolCallId === 'old-1');
    expect(extractText(old ?? createUserMessage(''))).toBe('[已压缩]');
    // 保留区内的最近工具结果不受影响
    const recent = secondHistory.find((m) => m.toolCallId === 'c1');
    expect(extractText(recent ?? createUserMessage(''))).toBe('small-ok');
  });

  it('generate 返回 usage 时 emit context_usage:usedTokens = inputOther + inputCacheRead + output', async () => {
    const h = harness(
      [[text('回答')]],
      [echoTool()],
      yoloGate(),
      [{ inputOther: 100, inputCacheRead: 40, inputCacheCreation: 999, output: 10 }],
    );

    const result = await h.run({ compaction: { maxContextTokens: 1000 } });

    expect(result.reason).toBe('completed');
    const usage = h.events.find((e) => e.type === 'context_usage');
    // inputCacheCreation 不计入上下文占用
    expect(usage).toEqual({ type: 'context_usage', usedTokens: 150, maxTokens: 1000 });
  });

  it('usage 不可用时(usage=null)不发 context_usage,也不压缩(触发单轨:不回退字符 heuristic)', async () => {
    // 历史含 20k 字符大工具结果:字符 heuristic 必然超阈,但无真实 usage
    // 就不压缩——「要不要压」只由真实 usage 回答。
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', 'x'.repeat(20_000)),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    const h = harness([[text('回答')]], [echoTool()]);

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 1000 },
    });

    expect(result.reason).toBe('completed');
    expect(h.events.some((e) => e.type === 'context_usage')).toBe(false);
    expect(h.events.some((e) => e.type === 'compaction')).toBe(false);
    // 历史原样:大工具结果未被替换
    expect(extractText(result.messages.find((m) => m.toolCallId === 'old-1') ?? createUserMessage(''))).toBe(
      'x'.repeat(20_000),
    );
  });

  it('真实 usage 超阈(≥ maxTokens × 0.85)时下一步前自动压缩:LLM 摘要替换压缩区', async () => {
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', '老工具结果'),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    // maxContextTokens=10000,触发线 8500;heuristic 估算远低于阈值
    // (reservedContextSize=0 关闭预留触发),仅靠真实 usage 驱动压缩。
    const summaryText = '视频 演示区/v1.mp4(时长 10s);事件 1 检出,证据帧 f1/f2,置信度高;结论尚未提交';
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('结束')]],
      [echoTool(() => Promise.resolve({ output: 'small-ok' }))],
      yoloGate(),
      [
        { inputOther: 8600, inputCacheRead: 0, inputCacheCreation: 0, output: 100 },
        null,
      ],
      [[text(summaryText)]],
    );

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 10_000, reservedContextSize: 0, maxRecentMessages: 2 },
    });

    expect(result.reason).toBe('completed');
    const compaction = h.events.find((e) => e.type === 'compaction');
    expect(compaction).toMatchObject({ compactedToolResults: 1, summarized: true });
    expect(
      compaction !== undefined && compaction.type === 'compaction'
        ? compaction.afterTokens < compaction.beforeTokens
        : false,
    ).toBe(true);
    // 摘要调用不传 tools
    expect(h.provider.summaryTools).toHaveLength(1);
    expect(h.provider.summaryTools[0]).toEqual([]);

    // 第二步历史:压缩区被一条摘要 user 消息替换,含前缀与关键字段
    const secondHistory = h.provider.histories[1] ?? [];
    const first = secondHistory[0];
    expect(first?.role).toBe('user');
    const firstText = extractText(first ?? createUserMessage(''));
    expect(firstText).toContain(SUMMARY_PREFIX);
    expect(firstText).toContain('演示区/v1.mp4');
    expect(firstText).toContain('证据帧 f1/f2');
    // 老工具结果已随压缩区整体移除(不再是占位)
    expect(secondHistory.some((m) => m.toolCallId === 'old-1')).toBe(false);
    // 保留区不变:当前 user 轮次与新工具结果仍在
    expect(secondHistory.some((m) => m.role === 'user' && extractText(m) === '继续')).toBe(true);
    expect(secondHistory.some((m) => m.toolCallId === 'c1')).toBe(true);
  });

  it('摘要调用失败时回退占位替换:compaction 事件 summarized=false,loop 不崩', async () => {
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', '老工具结果'),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('结束')]],
      [echoTool(() => Promise.resolve({ output: 'small-ok' }))],
      yoloGate(),
      [
        { inputOther: 8600, inputCacheRead: 0, inputCacheCreation: 0, output: 100 },
        null,
      ],
      [new Error('summary boom')],
    );

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 10_000, reservedContextSize: 0, maxRecentMessages: 2 },
    });

    expect(result.reason).toBe('completed');
    const compaction = h.events.find((e) => e.type === 'compaction');
    expect(compaction).toMatchObject({ compactedToolResults: 1, summarized: false });
    // 回退为占位替换:老工具结果仍在骨架中,内容变占位
    const secondHistory = h.provider.histories[1] ?? [];
    const old = secondHistory.find((m) => m.toolCallId === 'old-1');
    expect(extractText(old ?? createUserMessage(''))).toBe('[已压缩]');
  });

  it('摘要估算不短于压缩区时放弃压缩:compaction_abandoned 事件,历史原样保留', async () => {
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', '老工具结果'),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('结束')]],
      [echoTool(() => Promise.resolve({ output: 'small-ok' }))],
      yoloGate(),
      [
        { inputOther: 8600, inputCacheRead: 0, inputCacheCreation: 0, output: 100 },
        null,
      ],
      // 摘要返回超长文本:估算 token 远超压缩区 → 放弃本次压缩
      [[text('冗长摘要'.repeat(2000))]],
    );

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 10_000, reservedContextSize: 0, maxRecentMessages: 2 },
    });

    expect(result.reason).toBe('completed');
    // 不发生压缩:无 compaction 事件,有 abandoned 事件且摘要不短于压缩区
    expect(h.events.some((e) => e.type === 'compaction')).toBe(false);
    const abandoned = h.events.find((e) => e.type === 'compaction_abandoned');
    expect(abandoned).toBeDefined();
    if (abandoned?.type === 'compaction_abandoned') {
      expect(abandoned.summaryTokens).toBeGreaterThanOrEqual(abandoned.zoneTokens);
    }
    // 历史原样:老工具结果未被摘要替换,也未被占位替换
    const secondHistory = h.provider.histories[1] ?? [];
    const old = secondHistory.find((m) => m.toolCallId === 'old-1');
    expect(extractText(old ?? createUserMessage(''))).toBe('老工具结果');
    expect(
      secondHistory.some((m) => m.role === 'user' && extractText(m).includes(SUMMARY_PREFIX)),
    ).toBe(false);
  });

  it('截断步:arguments 残块不执行并回灌重试提示,完整调用照常执行,done.truncated=true', async () => {
    const execute = vi.fn(() => Promise.resolve({ output: 'echo-ok' }));
    // 残块:arguments 无法 JSON.parse(截断在 JSON 中间)
    const broken: ToolCall = {
      type: 'function',
      id: 'c-bad',
      name: 'echo',
      arguments: '{"events":[{"id":',
    };
    const h = harness(
      [
        [toolCall('c1', 'echo', { a: 1 }), broken],
        [text('已缩小输出重试')],
      ],
      [echoTool(execute)],
      yoloGate(),
      [],
      [],
      ['truncated'],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    expect(result.truncated).toBe(true);
    expect(doneEvent(h.events).truncated).toBe(true);
    // 完整调用执行了一次;残块不进入工具(execute 只被 c1 调用)
    expect(execute).toHaveBeenCalledTimes(1);

    const fed = toolMessages(h.provider.histories[1] ?? []);
    expect(fed).toHaveLength(2);
    const byCallId = new Map(fed.map((m) => [m.toolCallId, extractText(m)]));
    expect(byCallId.get('c1')).toBe('echo-ok');
    expect(byCallId.get('c-bad')).toBe(TRUNCATED_TOOL_CALL_MESSAGE);

    const badResult = h.events.find(
      (e) => e.type === 'tool_result' && e.toolCallId === 'c-bad',
    );
    expect(badResult).toMatchObject({ isError: true });
  });

  it('sticky truncated:截断步之后正常完成,done 事件与结果仍带 truncated=true', async () => {
    const h = harness(
      [
        [toolCall('c1', 'echo', {})],
        [toolCall('c2', 'echo', {})],
        [text('完成')],
      ],
      [echoTool()],
      yoloGate(),
      [],
      [],
      ['truncated', 'completed'],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    expect(result.steps).toBe(3);
    expect(result.truncated).toBe(true);
    expect(doneEvent(h.events).truncated).toBe(true);
  });

  it('无截断时:done 事件不带 truncated,result.truncated=false', async () => {
    const h = harness([[text('正常')]], [echoTool()]);

    const result = await h.run();

    expect(result.truncated).toBe(false);
    expect(doneEvent(h.events).truncated).toBeUndefined();
  });

  it('摘要调用走关思考 provider:非 openai-legacy provider 回退 withThinking(off)', async () => {
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', '老工具结果'),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('结束')]],
      [echoTool()],
      yoloGate(),
      // 第一步真实 usage 超阈(8600 ≥ 10000 × 0.85)→ 第二步前触发摘要压缩
      [{ inputOther: 8600, inputCacheRead: 0, inputCacheCreation: 0, output: 100 }, null],
      [[text('摘要内容')]],
    );

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 10_000, reservedContextSize: 0, maxRecentMessages: 2 },
    });

    expect(result.reason).toBe('completed');
    expect(h.events.find((e) => e.type === 'compaction')).toMatchObject({ summarized: true });
    // withThinkingDisabled 对非 openai-legacy provider 回退 withThinking('off')
    expect(h.provider.thinkingEfforts).toContain('off');
  });
});

describe('compaction', () => {
  const bigToolExchange = (id: string, size: number): Message[] => [
    createAssistantMessage([{ type: 'text', text: '调用工具' }], [toolCall(id, 'echo', {})]),
    createToolMessage(id, 'y'.repeat(size)),
  ];

  it('触发判定单轨 isOverContextByUsage:只用真实 usage,消息内容不参与', () => {
    const config = createCompactionConfig(1_000_000);
    expect(isOverContextByUsage(1_000_000 * 0.85, config)).toBe(true);
    expect(isOverContextByUsage(1_000_000 * 0.85 - 1, config)).toBe(false);
    // usage 不可用 → 一律不触发(宁可错过也不用第二把尺子猜)。
    expect(isOverContextByUsage(undefined, config)).toBe(false);
    // maxContextTokens <= 0 → 永不压缩。
    expect(isOverContextByUsage(Number.MAX_SAFE_INTEGER, createCompactionConfig(0))).toBe(false);
  });

  it('预留输出空间同样触发:used + reserved >= max', () => {
    const config = createCompactionConfig(10_000, { reservedContextSize: 2_000 });
    expect(isOverContextByUsage(8_000, config)).toBe(true); // 8000 + 2000 >= 10000
    expect(isOverContextByUsage(7_999, config)).toBe(false); // 7999 + 2000 < 10000,且未过 0.85 线
    // reserved >= max(预留比窗口还大)不参与触发,与 vendor 语义一致。
    const whole = createCompactionConfig(10_000, { reservedContextSize: 10_000 });
    expect(isOverContextByUsage(9_500, whole)).toBe(true); // 9500 ≥ 8500 过 triggerRatio
    expect(isOverContextByUsage(8_400, whole)).toBe(false);
  });

  it('压缩执行:替换压缩区工具结果,保留区以 user 消息开头', () => {
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
    // 只有一个 user 轮次:压缩区为空,不做任何替换。
    const messages: Message[] = [
      createUserMessage('u1'),
      ...bigToolExchange('t1', 40_000),
      createAssistantMessage([{ type: 'text', text: 'a1' }]),
    ];
    const config = createCompactionConfig(1000, { maxRecentMessages: 4 });

    const outcome = compactMessages(messages, config);
    expect(outcome.compacted).toBe(false);
    expect(outcome.messages).toBe(messages);
  });

  it('保留区按 retainTokens 从尾部向前累计:预算越大保留越多(仍以 user 为安全边界)', () => {
    const messages: Message[] = [
      createUserMessage('u1'),
      ...bigToolExchange('t1', 1000),
      createUserMessage('u2'),
      ...bigToolExchange('t2', 1000),
      createUserMessage('u3'),
      ...bigToolExchange('t3', 1000),
      createAssistantMessage([{ type: 'text', text: 'a3' }]),
    ];

    // 预算极小:只保留下限(maxRecentMessages=2 向前扩展到 u3),t1/t2 都被压
    const tight = compactMessages(
      messages,
      createCompactionConfig(1_000_000, { maxRecentMessages: 2, retainTokens: 10 }),
    );
    expect(tight.compactedToolResults).toBe(2);

    // 预算够两轮:保留区扩展到 u2(再加 u1 一轮会超预算),只有 t1 被压
    const loose = compactMessages(
      messages,
      createCompactionConfig(1_000_000, { maxRecentMessages: 2, retainTokens: 1000 }),
    );
    expect(loose.compactedToolResults).toBe(1);
    const t1 = loose.messages.find((m) => m.toolCallId === 't1');
    expect(extractText(t1 ?? createUserMessage(''))).toBe('[已压缩]');
    const t2 = loose.messages.find((m) => m.toolCallId === 't2');
    expect(extractText(t2 ?? createUserMessage('')).length).toBe(1000);
  });

  it('retainTokens 固定时保留区随内容长度变化:最近一轮变长则少保留一轮', () => {
    // 与上一个用例同构,但最近一轮的工具结果更长:同样的 1000 预算下
    // 保留区装不下 u2 一轮,只保留 u3 起 → t2 也被压。
    const messages: Message[] = [
      createUserMessage('u1'),
      ...bigToolExchange('t1', 1000),
      createUserMessage('u2'),
      ...bigToolExchange('t2', 1000),
      createUserMessage('u3'),
      ...bigToolExchange('t3', 3000),
      createAssistantMessage([{ type: 'text', text: 'a3' }]),
    ];
    const outcome = compactMessages(
      messages,
      createCompactionConfig(1_000_000, { maxRecentMessages: 2, retainTokens: 1000 }),
    );
    expect(outcome.compactedToolResults).toBe(2);
    const t3 = outcome.messages.find((m) => m.toolCallId === 't3');
    expect(extractText(t3 ?? createUserMessage('')).length).toBe(3000);
  });
});

describe('子代理事件转发(subagent_event)', () => {
  /** 执行时经 ctx.onSubagentEvent 上报嵌套事件的假工具。 */
  function subEmitTool(childEvents: AgentLoopEvent[]): ExecutableTool {
    return {
      name: 'sub_emit',
      description: 'fake tool emitting nested loop events',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'sub_emit()',
        execute: async (ctx) => {
          for (const ev of childEvents) await ctx.onSubagentEvent?.(ev);
          return { output: 'sub-done' };
        },
      }),
    };
  }

  it('缺省包装成 subagent_event 进入父 loop 事件流', async () => {
    const h = harness(
      [[toolCall('c1', 'sub_emit', {})], [text('结束')]],
      [subEmitTool([{ type: 'text_delta', text: '子代理思考中' }])],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    const nested = h.events.find((e) => e.type === 'subagent_event');
    expect(nested).toMatchObject({
      type: 'subagent_event',
      toolCallId: 'c1',
      event: { type: 'text_delta', text: '子代理思考中' },
    });
  });

  it('onSubagentEvent 已删除:子代理事件统一走 subagent_event 事件流(唯一投递通道)', async () => {
    // 曾有 options.onSubagentEvent 接管投递的旁路,生产无人使用,D5 删除;
    // 这里锁定唯一通道行为——所有子事件都包装成 subagent_event 进 onEvent 流。
    const h = harness(
      [
        [toolCall('c1', 'sub_emit', {})],
        [toolCall('c2', 'sub_emit', {})],
        [text('结束')],
      ],
      [subEmitTool([{ type: 'text_delta', text: 'x' }, { type: 'text_delta', text: 'y' }])],
    );

    const result = await h.run();

    expect(result.reason).toBe('completed');
    const nested = h.events.filter((e) => e.type === 'subagent_event');
    // 两次工具调用 × 每次 2 条子事件,全部经唯一通道到达。
    expect(nested).toHaveLength(4);
    expect(nested[0]).toMatchObject({ toolCallId: 'c1' });
    expect(nested[2]).toMatchObject({ toolCallId: 'c2' });
  });

  it('onStepPersist 更新:appended 按消息确定点即时到达,compacted 在压缩发生时整体重写', async () => {
    const initialMessages: Message[] = [
      createUserMessage('第一轮'),
      createAssistantMessage([{ type: 'text', text: '先看元数据' }], [
        toolCall('old-1', 'echo', {}),
      ]),
      createToolMessage('old-1', '老工具结果'),
      createAssistantMessage([{ type: 'text', text: '看完了' }]),
      createUserMessage('继续'),
    ];
    const summaryText = '视频 演示区/v1.mp4;事件 1 检出,证据帧 f1/f2;结论尚未提交';
    const h = harness(
      [[toolCall('c1', 'echo', {})], [text('结束')]],
      [echoTool(() => Promise.resolve({ output: 'small-ok' }))],
      yoloGate(),
      [
        { inputOther: 8600, inputCacheRead: 0, inputCacheCreation: 0, output: 100 },
        null,
      ],
      [[text(summaryText)]],
    );
    const updates: StepPersistUpdate[] = [];

    const result = await h.run({
      messages: initialMessages,
      compaction: { maxContextTokens: 10_000, reservedContextSize: 0, maxRecentMessages: 2 },
      onStepPersist: (update) => {
        updates.push(update);
      },
    });

    expect(result.reason).toBe('completed');
    // 步骤一:assistant(toolCalls)与 tool 消息同批即时落盘
    expect(updates[0]).toMatchObject({ kind: 'appended' });
    if (updates[0]?.kind === 'appended') {
      expect(updates[0].messages.map((m) => m.role)).toEqual(['assistant', 'tool']);
    }
    // 压缩:整体折叠,一次 compacted 更新携带压缩后的完整消息
    // (摘要 user + 保留区,后续步骤在其基础上继续追加)
    const compacted = updates.find((u) => u.kind === 'compacted');
    expect(compacted).toBeDefined();
    if (compacted?.kind === 'compacted') {
      expect(compacted.messages.length).toBe(result.messages.length - 1);
    }
    // 步骤二:最终 assistant 文本
    expect(updates.at(-1)).toMatchObject({ kind: 'appended' });
    if (updates.at(-1)?.kind === 'appended') {
      expect(updates.at(-1)?.messages.map((m) => m.role)).toEqual(['assistant']);
    }
  });

  it('execution.timeoutMs 覆盖 loop 级 toolTimeoutMs(长任务不被 120s 截断)', async () => {
    const slowTool: ExecutableTool = {
      name: 'slow',
      description: 'fake slow tool',
      parameters: { type: 'object' },
      resolveExecution: () => ({
        accesses: [],
        approvalRule: 'slow()',
        timeoutMs: 60_000,
        execute: () =>
          new Promise<ExecutableToolResult>((resolve) => {
            setTimeout(() => resolve({ output: 'slow-ok' }), 200);
          }),
      }),
    };
    const h = harness(
      [[toolCall('c1', 'slow', {})], [text('结束')]],
      [slowTool],
    );

    // loop 级 50ms 会超时;工具自声明 60s 生效 → 正常完成
    const result = await h.run({ toolTimeoutMs: 50 });

    expect(result.reason).toBe('completed');
    const toolResult = h.events.find((e) => e.type === 'tool_result');
    expect(toolResult).toMatchObject({ name: 'slow', isError: false });
  });
});
