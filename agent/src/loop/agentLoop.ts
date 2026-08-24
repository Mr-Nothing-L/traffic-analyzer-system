/**
 * agent 多轮循环:generate → 权限裁决 → 冲突调度执行 → 结果回灌。
 *
 * 把已有的 kosong(LLM 抽象)、tools(契约/注册表/调度器)与
 * permissions(权限门)串成 docs/agent_refactor_plan.md 描述的
 * 简化版 Turn/Step:`while (tool_calls) { ... }`,带 maxStepsPerTurn
 * 上限与比率触发压缩(compaction.ts)。
 */
import { isAbortError } from '#/errors';
import { generate } from '#/generate';
import { createToolMessage, type Message, type ToolCall } from '#/message';
import type { ChatProvider } from '#/provider';

import type { PermissionGate } from '../permissions/gate';
import {
  isRunnableToolExecution,
  type ExecutableToolResult,
  type RunnableToolExecution,
} from '../tools/contract';
import type { ToolRegistry } from '../tools/registry';
import { ToolScheduler, type ToolCallTask } from '../tools/scheduler';

import {
  compactMessages,
  createCompactionConfig,
  type CompactionConfig,
  type CompactionOverrides,
} from './compaction';

export const DEFAULT_MAX_STEPS_PER_TURN = 30;
export const DEFAULT_TOOL_TIMEOUT_MS = 120_000;

export type AgentLoopDoneReason =
  | 'completed'
  | 'stop_turn'
  | 'max_steps'
  | 'cancelled'
  | 'error';

export type AgentLoopEvent =
  | { readonly type: 'text_delta'; readonly text: string }
  | { readonly type: 'think_delta'; readonly text: string }
  | { readonly type: 'tool_call_start'; readonly call: ToolCall }
  | {
      readonly type: 'tool_result';
      readonly toolCallId: string;
      readonly name: string;
      readonly result: ExecutableToolResult;
      readonly isError: boolean;
    }
  | { readonly type: 'step_done'; readonly step: number }
  | {
      readonly type: 'done';
      readonly reason: AgentLoopDoneReason;
      readonly stopResult?: ExecutableToolResult;
    };

export interface AgentLoopOptions {
  /** kosong chat provider(由 llm/provider.ts 的 createProviderFromEnv 构造)。 */
  readonly provider: ChatProvider;
  /** 模型名(冗余于 provider.modelName,供调用方日志/事件使用)。 */
  readonly model: string;
  readonly systemPrompt: string;
  readonly registry: ToolRegistry;
  readonly gate: PermissionGate;
  /** 初始对话历史(不含 system;system 经 systemPrompt 单独传给 generate)。 */
  readonly messages: Message[];
  /** 单个 turn 内 generate 步数上限,默认 30。 */
  readonly maxStepsPerTurn?: number;
  /** 单次工具执行超时(ms),默认 120s;超时合成 isError 结果回灌。 */
  readonly toolTimeoutMs?: number;
  /** 比率触发压缩配置;缺省不压缩。 */
  readonly compaction?: { readonly maxContextTokens: number } & CompactionOverrides;
  readonly signal?: AbortSignal;
  readonly onEvent?: (event: AgentLoopEvent) => void | Promise<void>;
}

export interface AgentLoopResult {
  readonly reason: AgentLoopDoneReason;
  /** 循环结束时的完整对话历史(含回灌的 assistant / tool 消息)。 */
  readonly messages: Message[];
  /** 实际执行的 generate 步数。 */
  readonly steps: number;
  /** reason === 'stop_turn' 时携带触发停止的工具结果。 */
  readonly stopResult?: ExecutableToolResult;
  /** reason === 'error' 时的错误信息。 */
  readonly error?: string;
}

export async function runAgentLoop(options: AgentLoopOptions): Promise<AgentLoopResult> {
  const maxSteps = options.maxStepsPerTurn ?? DEFAULT_MAX_STEPS_PER_TURN;
  const toolTimeoutMs = options.toolTimeoutMs ?? DEFAULT_TOOL_TIMEOUT_MS;
  const tools = options.registry.list();
  const compaction: CompactionConfig | undefined =
    options.compaction === undefined
      ? undefined
      : createCompactionConfig(options.compaction.maxContextTokens, options.compaction);

  let messages: Message[] = [...options.messages];
  let steps = 0;

  const emit = async (event: AgentLoopEvent): Promise<void> => {
    await options.onEvent?.(event);
  };
  // 经函数读取以避免 TS 对可选链的窄化在 await 后残留(abort 随时可能发生)。
  const isAborted = (): boolean => options.signal?.aborted === true;
  const finish = async (
    reason: AgentLoopDoneReason,
    extra: { stopResult?: ExecutableToolResult; error?: string } = {},
  ): Promise<AgentLoopResult> => {
    await emit({ type: 'done', reason, stopResult: extra.stopResult });
    return {
      reason,
      messages,
      steps,
      stopResult: extra.stopResult,
      error: extra.error,
    };
  };

  /** 未注册 / resolve 失败 / deny 等场景:合成 isError 结果,直接回灌给模型。 */
  const synthesizeTask = (
    result: ExecutableToolResult,
  ): ToolCallTask<ExecutableToolResult> => ({
    accesses: [],
    start: () => Promise.resolve({ result: Promise.resolve(result) }),
  });

  const prepareTask = async (call: ToolCall): Promise<ToolCallTask<ExecutableToolResult>> => {
    const tool = options.registry.resolve(call.name);
    if (tool === undefined) {
      return synthesizeTask({
        output: `Tool "${call.name}" is not registered.`,
        isError: true,
      });
    }

    let execution;
    try {
      execution = await tool.resolveExecution(parseToolArguments(call.arguments));
    } catch (error) {
      return synthesizeTask({
        output: `Tool "${call.name}" failed to resolve execution: ${errorMessage(error)}`,
        isError: true,
      });
    }
    // resolveExecution 直接返回错误结果(如沙盒 veto),原样回灌。
    if (!isRunnableToolExecution(execution)) {
      return synthesizeTask(execution);
    }

    let decision;
    try {
      decision = await options.gate.authorize({
        toolCall: { id: call.id, name: call.name, arguments: call.arguments },
        execution,
      });
    } catch (error) {
      return synthesizeTask({
        output: `Authorization for tool "${call.name}" failed: ${errorMessage(error)}`,
        isError: true,
      });
    }
    if (decision.kind === 'deny') {
      let output = `Tool call "${call.name}" was denied by permission policy "${decision.policyName}".`;
      if (decision.message !== undefined) output += ` ${decision.message}`;
      if (decision.feedback !== undefined) output += ` Feedback: ${decision.feedback}`;
      return synthesizeTask({ output, isError: true });
    }

    return {
      accesses: execution.accesses ?? [],
      start: () =>
        Promise.resolve({
          result: executeWithTimeout(execution, call, options.signal, toolTimeoutMs),
        }),
    };
  };

  for (;;) {
    if (isAborted()) return finish('cancelled');
    if (steps >= maxSteps) return finish('max_steps');
    if (compaction !== undefined) {
      const outcome = compactMessages(messages, compaction);
      if (outcome.compacted) messages = outcome.messages;
    }
    steps += 1;

    let assistant: Message;
    try {
      const generated = await generate(
        options.provider,
        options.systemPrompt,
        tools,
        messages,
        {
          onMessagePart: async (part) => {
            if (part.type === 'text') {
              await emit({ type: 'text_delta', text: part.text });
            } else if (part.type === 'think') {
              await emit({ type: 'think_delta', text: part.think });
            }
          },
        },
        { signal: options.signal },
      );
      assistant = generated.message;
    } catch (error) {
      if (isAbortError(error) || isAborted()) {
        return finish('cancelled');
      }
      return finish('error', { error: errorMessage(error) });
    }

    messages = [...messages, assistant];

    if (assistant.toolCalls.length === 0) {
      await emit({ type: 'step_done', step: steps });
      return finish('completed');
    }

    // 同批 tool calls:先逐个 resolve + 权限裁决(串行,审批 UX 不并发),
    // 再交给 ToolScheduler 按 accesses 冲突并行执行,runBatch 保序。
    const scheduler = new ToolScheduler<ExecutableToolResult>();
    const tasks: ToolCallTask<ExecutableToolResult>[] = [];
    for (const call of assistant.toolCalls) {
      await emit({ type: 'tool_call_start', call });
      tasks.push(await prepareTask(call));
    }
    const results = await scheduler.runBatch(tasks);

    let stopResult: ExecutableToolResult | undefined;
    for (const [index, result] of results.entries()) {
      const call = assistant.toolCalls[index];
      if (call === undefined) continue;
      await emit({
        type: 'tool_result',
        toolCallId: call.id,
        name: call.name,
        result,
        isError: result.isError === true,
      });
      messages.push(createToolMessage(call.id, result.output));
      if (result.stopTurn === true && stopResult === undefined) {
        stopResult = result;
      }
    }
    await emit({ type: 'step_done', step: steps });
    if (stopResult !== undefined) return finish('stop_turn', { stopResult });
  }
}

/** 超时与异常统一合成 isError 结果;超时会 abort 工具 ctx.signal(工具是否响应取决于自身)。 */
async function executeWithTimeout(
  execution: RunnableToolExecution,
  call: ToolCall,
  signal: AbortSignal | undefined,
  timeoutMs: number,
): Promise<ExecutableToolResult> {
  const controller = new AbortController();
  const onAbort = (): void => controller.abort();
  signal?.addEventListener('abort', onAbort, { once: true });

  const run = (async (): Promise<ExecutableToolResult> => {
    try {
      return await execution.execute({ toolCallId: call.id, signal: controller.signal });
    } catch (error) {
      return {
        output: `Tool "${call.name}" execution failed: ${errorMessage(error)}`,
        isError: true,
      };
    }
  })();

  let timer: ReturnType<typeof setTimeout> | undefined;
  const raced =
    timeoutMs > 0
      ? Promise.race([
          run,
          new Promise<ExecutableToolResult>((resolve) => {
            timer = setTimeout(() => {
              controller.abort();
              resolve({
                output: `Tool "${call.name}" execution timed out after ${timeoutMs}ms.`,
                isError: true,
              });
            }, timeoutMs);
          }),
        ])
      : run;

  try {
    return await raced;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}

/** tool call arguments 是 JSON 字符串;解析失败时把原文交给工具的 schema 校验自行报错。 */
function parseToolArguments(args: string | null): unknown {
  if (args === null || args.trim() === '') return {};
  try {
    return JSON.parse(args) as unknown;
  } catch {
    return args;
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
