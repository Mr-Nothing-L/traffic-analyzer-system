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
  createCompactionConfig,
  type CompactionConfig,
  type CompactionOverrides,
} from './compaction';
import { compactMessagesWithSummary } from './summarize';

export const DEFAULT_MAX_STEPS_PER_TURN = 30;
export const DEFAULT_TOOL_TIMEOUT_MS = 120_000;

/** 截断残块 tool call 回灌给模型的提示(要求缩小单次输出重试)。 */
export const TRUNCATED_TOOL_CALL_MESSAGE =
  '输出达到 token 上限被截断,该工具调用参数不完整,请重试并缩小单次输出(如减少事件实例数或分次提交)';

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
      /** 每步 generate 拿到真实 usage 后发出:该次请求的上下文占用与窗口上限。 */
      readonly type: 'context_usage';
      readonly usedTokens: number;
      readonly maxTokens: number;
    }
  | {
      /** 自动压缩实际发生时发出(LLM 摘要或回退占位)。 */
      readonly type: 'compaction';
      readonly compactedToolResults: number;
      /** true = 压缩区被 LLM 摘要替换;false = 回退为占位替换。 */
      readonly summarized: boolean;
      /** 压缩前 token 估算(heuristic)。 */
      readonly beforeTokens: number;
      /** 压缩后 token 估算(heuristic)。 */
      readonly afterTokens: number;
    }
  | {
      /**
       * 子代理嵌套事件:工具(如 spawn_subagent)在执行期间经
       * ctx.onSubagentEvent 上报子 loop 事件,loop 包装成本事件进入父
       * 事件流;server 原样透传 SSE(前端按 toolCallId 归属到对应工具)。
       */
      readonly type: 'subagent_event';
      readonly toolCallId: string;
      readonly event: AgentLoopEvent;
    }
  | {
      readonly type: 'done';
      readonly reason: AgentLoopDoneReason;
      readonly stopResult?: ExecutableToolResult;
      /** reason === 'error' 时的错误信息。 */
      readonly error?: string;
      /** 本轮任一步 generate 因 token 上限被截断(sticky,置位后不再清除)。 */
      readonly truncated?: boolean;
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
  /** 同一工具连续失败熔断阈值,默认 5;达到后以 reason 'error' 终止循环。 */
  readonly maxConsecutiveToolErrors?: number;
  /** 单次工具执行超时(ms),默认 120s;超时合成 isError 结果回灌。 */
  readonly toolTimeoutMs?: number;
  /** 比率触发压缩配置;缺省不压缩。 */
  readonly compaction?: { readonly maxContextTokens: number } & CompactionOverrides;
  readonly signal?: AbortSignal;
  readonly onEvent?: (event: AgentLoopEvent) => void | Promise<void>;
  /**
   * steer 注入回调(可选):每个 step 的 generate 之前调用一次,返回本轮
   * 进行中被排队(POST /sessions/{id}/steer)的 user 消息;非空则按序追加
   * 进 messages 并立即经 onStepPersist 落盘,再继续 generate——模型在下一次
   * 调用时看到新指示。返回 null/空数组表示无排队输入。注入后即视为消费,
   * 回调方负责清空队列(参考 deepseek-harness 的 inbox next-step 语义)。
   */
  readonly shouldSteer?: () => readonly Message[] | null;
  /**
   * 按步持久化回调(可选):每个 step 结束(step_done 事件发出)前调用,
   * 参数为上一步以来回灌进 messages 的增量(assistant / tool 消息),
   * 供 server 同步落盘——崩溃时半截轮次不丢。压缩发生时历史被整体折叠,
   * 增量水位随之重置(压缩后的整体回写由调用方负责)。
   */
  readonly onStepPersist?: (appended: readonly Message[]) => void | Promise<void>;
  /**
   * 子代理事件回调:设置后接管子代理事件的投递(不再自动包装成
   * 'subagent_event' 进入 onEvent 流);缺省时 loop 自动包装转发。
   */
  readonly onSubagentEvent?: (
    parentToolCallId: string,
    event: AgentLoopEvent,
  ) => void | Promise<void>;
}

export interface AgentLoopResult {
  readonly reason: AgentLoopDoneReason;
  /** 循环结束时的完整对话历史(含回灌的 assistant / tool 消息)。 */
  readonly messages: Message[];
  /** 实际执行的 generate 步数。 */
  readonly steps: number;
  /** 本轮任一步 generate 因 token 上限被截断(sticky)。 */
  readonly truncated: boolean;
  /** reason === 'stop_turn' 时携带触发停止的工具结果。 */
  readonly stopResult?: ExecutableToolResult;
  /** reason === 'error' 时的错误信息。 */
  readonly error?: string;
}

export async function runAgentLoop(options: AgentLoopOptions): Promise<AgentLoopResult> {
  const maxSteps = options.maxStepsPerTurn ?? DEFAULT_MAX_STEPS_PER_TURN;
  const maxConsecutiveToolErrors = options.maxConsecutiveToolErrors ?? 5;
  const toolTimeoutMs = options.toolTimeoutMs ?? DEFAULT_TOOL_TIMEOUT_MS;
  const tools = options.registry.list();
  const compaction: CompactionConfig | undefined =
    options.compaction === undefined
      ? undefined
      : createCompactionConfig(options.compaction.maxContextTokens, options.compaction);

  let messages: Message[] = [...options.messages];
  let steps = 0;
  /** 上一步 generate 的真实上下文占用(inputOther + inputCacheRead + output);
   * usage 不可用时保持 undefined,压缩触发回退 heuristic。 */
  let lastUsedTokens: number | undefined;
  let errorStreak: { name: string; count: number; output: ExecutableToolResult['output'] | undefined } =
    { name: '', count: 0, output: undefined };
  /** sticky 截断标记:任一步 finishReason==='truncated' 后整个 turn 的
   * done 事件与返回值都带 truncated(参照 deepseek-harness 的粘性 max-tokens)。 */
  let turnTruncated = false;

  const emit = async (event: AgentLoopEvent): Promise<void> => {
    await options.onEvent?.(event);
  };
  /** 已通知调用方持久化的 messages 水位(下标);压缩整体折叠后重置。 */
  let persistedLength = messages.length;
  const flushStepMessages = async (): Promise<void> => {
    if (messages.length <= persistedLength) return;
    const appended = messages.slice(persistedLength);
    persistedLength = messages.length;
    await options.onStepPersist?.(appended);
  };
  /** 工具上报的子代理事件:options.onSubagentEvent 设置时交给它接管,
   * 否则包装成 'subagent_event' 进入本 loop 的事件流(server 透传 SSE)。 */
  const forwardSubagentEvent = async (toolCallId: string, event: unknown): Promise<void> => {
    const childEvent = event as AgentLoopEvent;
    if (options.onSubagentEvent !== undefined) {
      await options.onSubagentEvent(toolCallId, childEvent);
      return;
    }
    await emit({ type: 'subagent_event', toolCallId, event: childEvent });
  };
  // 经函数读取以避免 TS 对可选链的窄化在 await 后残留(abort 随时可能发生)。
  const isAborted = (): boolean => options.signal?.aborted === true;
  const finish = async (
    reason: AgentLoopDoneReason,
    extra: { stopResult?: ExecutableToolResult; error?: string } = {},
  ): Promise<AgentLoopResult> => {
    await emit({
      type: 'done',
      reason,
      stopResult: extra.stopResult,
      error: extra.error,
      ...(turnTruncated ? { truncated: true } : {}),
    });
    return {
      reason,
      messages,
      steps,
      truncated: turnTruncated,
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
          result: executeWithTimeout(execution, call, options.signal, toolTimeoutMs, (event) =>
            forwardSubagentEvent(call.id, event),
          ),
        }),
    };
  };

  for (;;) {
    if (isAborted()) return finish('cancelled');
    if (steps >= maxSteps) return finish('max_steps');
    if (compaction !== undefined) {
      // 触发判断优先真实 usage(≥ maxTokens × triggerRatio 即视为要爆了),
      // 不可用(force=false)时回退 token 估算 heuristic;压缩内容优先 LLM
      // 摘要,摘要失败回退占位替换(见 summarize.ts)。
      const overByUsage =
        lastUsedTokens !== undefined &&
        lastUsedTokens >= compaction.maxContextTokens * compaction.triggerRatio;
      try {
        const outcome = await compactMessagesWithSummary(
          messages,
          compaction,
          options.provider,
          overByUsage,
          options.signal,
        );
        if (outcome.compacted) {
          messages = outcome.messages;
          // 历史被整体折叠:增量水位重置,压缩前的增量由调用方整体回写覆盖。
          persistedLength = messages.length;
          await emit({
            type: 'compaction',
            compactedToolResults: outcome.compactedToolResults,
            summarized: outcome.summarized,
            beforeTokens: outcome.beforeTokens,
            afterTokens: outcome.afterTokens,
          });
        }
      } catch (error) {
        // 摘要调用期间的父级取消(用户停止/断连):按取消收尾;
        // 其余异常 summarize.ts 已回退兜底,理论不可达,跳过本次压缩继续。
        if (isAbortError(error) || isAborted()) return finish('cancelled');
      }
    }
    steps += 1;

    // steer:每个 step 的 generate 之前检查排队注入;有则按序追加进
    // messages 并立即落盘(flushStepMessages → onStepPersist),模型本次
    // 调用即可看到新指示。
    const steered = options.shouldSteer?.();
    if (steered != null && steered.length > 0) {
      messages = [...messages, ...steered];
      await flushStepMessages();
    }

    let assistant: Message;
    let stepTruncated = false;
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
      // 本步被 token 上限截断:sticky 置位;本步 tool calls 中 arguments
      // 无法 JSON.parse 的残块不执行,合成 isError 结果回灌提示重试。
      stepTruncated = generated.finishReason === 'truncated';
      if (stepTruncated) turnTruncated = true;
      if (generated.usage !== null && compaction !== undefined) {
        lastUsedTokens =
          generated.usage.inputOther +
          generated.usage.inputCacheRead +
          generated.usage.output;
        await emit({
          type: 'context_usage',
          usedTokens: lastUsedTokens,
          maxTokens: compaction.maxContextTokens,
        });
      }
    } catch (error) {
      if (isAbortError(error) || isAborted()) {
        return finish('cancelled');
      }
      return finish('error', { error: errorMessage(error) });
    }

    messages = [...messages, assistant];

    if (assistant.toolCalls.length === 0) {
      await flushStepMessages();
      await emit({ type: 'step_done', step: steps });
      return finish('completed');
    }

    // 同批 tool calls:先逐个 resolve + 权限裁决(串行,审批 UX 不并发),
    // 再交给 ToolScheduler 按 accesses 冲突并行执行,runBatch 保序。
    const scheduler = new ToolScheduler<ExecutableToolResult>();
    const tasks: ToolCallTask<ExecutableToolResult>[] = [];
    for (const call of assistant.toolCalls) {
      await emit({ type: 'tool_call_start', call });
      // 截断步的残块(arguments 无法 JSON.parse)不执行,直接合成提示重试;
      // 可解析的完整调用照常走 resolve + 权限裁决 + 调度执行。
      tasks.push(
        stepTruncated && !isParseableJson(call.arguments)
          ? synthesizeTask({ output: TRUNCATED_TOOL_CALL_MESSAGE, isError: true })
          : await prepareTask(call),
      );
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
      if (result.isError === true) {
        errorStreak =
          errorStreak.name === call.name
            ? { name: call.name, count: errorStreak.count + 1, output: result.output }
            : { name: call.name, count: 1, output: result.output };
      } else {
        errorStreak = { name: '', count: 0, output: undefined };
      }
      if (result.stopTurn === true && stopResult === undefined) {
        stopResult = result;
      }
    }
    await flushStepMessages();
    await emit({ type: 'step_done', step: steps });
    if (stopResult !== undefined) return finish('stop_turn', { stopResult });
    // 熔断:同一工具连续失败达到上限,终止循环防止无效重试烧 token
    // (实测 qwen3 对某类参数错误会无限次原样重试)。
    if (errorStreak.count >= maxConsecutiveToolErrors) {
      const lastOutput =
        typeof errorStreak.output === 'string'
          ? errorStreak.output.slice(0, 500)
          : '(非文本输出)';
      return finish('error', {
        error:
          `工具 "${errorStreak.name}" 连续 ${errorStreak.count} 次失败,` +
          `触发熔断终止。最近一次错误: ${lastOutput}`,
      });
    }
  }
}

/** 超时与异常统一合成 isError 结果;超时会 abort 工具 ctx.signal(工具是否响应取决于自身)。
 * 超时优先级:execution.timeoutMs(工具自声明,如子代理 600s)> loop 级 toolTimeoutMs。 */
async function executeWithTimeout(
  execution: RunnableToolExecution,
  call: ToolCall,
  signal: AbortSignal | undefined,
  timeoutMs: number,
  onSubagentEvent: (event: unknown) => void | Promise<void>,
): Promise<ExecutableToolResult> {
  const effectiveTimeoutMs = execution.timeoutMs ?? timeoutMs;
  const controller = new AbortController();
  const onAbort = (): void => controller.abort();
  signal?.addEventListener('abort', onAbort, { once: true });

  const run = (async (): Promise<ExecutableToolResult> => {
    try {
      return await execution.execute({
        toolCallId: call.id,
        signal: controller.signal,
        onSubagentEvent,
      });
    } catch (error) {
      return {
        output: `Tool "${call.name}" execution failed: ${errorMessage(error)}`,
        isError: true,
      };
    }
  })();

  let timer: ReturnType<typeof setTimeout> | undefined;
  const raced =
    effectiveTimeoutMs > 0
      ? Promise.race([
          run,
          new Promise<ExecutableToolResult>((resolve) => {
            timer = setTimeout(() => {
              controller.abort();
              resolve({
                output: `Tool "${call.name}" execution timed out after ${effectiveTimeoutMs}ms.`,
                isError: true,
              });
            }, effectiveTimeoutMs);
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

/** arguments 能否 JSON.parse(null/空串按 {} 处理,视为可解析)。 */
function isParseableJson(args: string | null): boolean {
  if (args === null || args.trim() === '') return true;
  try {
    JSON.parse(args);
    return true;
  } catch {
    return false;
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
