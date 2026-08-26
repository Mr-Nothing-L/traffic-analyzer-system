/**
 * 共享假 ChatProvider:按脚本逐轮返回流式 parts,用于 agent 各模块单元测试。
 *
 * 能力(相对旧版的各文件内实现合并):
 * - 普通 generate 调用按 script 队列依次返回;
 * - script 条目可以是 Error,此时 generate 以该 Error reject(测失败容错);
 * - 支持按 systemPrompt === SUMMARY_SYSTEM_PROMPT 嗅探摘要调用,走独立的
 *   summaries 队列(与主 script 互不干扰);
 * - 可选 usages / finishReasons 与主 script 逐步对应;
 * - 记录每次 generate 收到的 history、tools,以及 withThinking 收到的 effort,
 *   便于测试断言。
 */
import type {
  Message,
  StreamedMessagePart,
  ToolCall,
  ChatProvider,
  FinishReason,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
  Tool,
  TokenUsage,
} from '../llm/kosong';
import { SUMMARY_SYSTEM_PROMPT } from '../loop/summarize';

export interface ScriptedProviderOptions {
  /** 普通 generate 调用的应答脚本;条目可以是 Error 以模拟失败。 */
  readonly script: (StreamedMessagePart[] | Error)[];
  /** 与 script 逐步对应的 usage(缺省 null = provider 不上报)。 */
  readonly usages?: (TokenUsage | null)[];
  /** 与 script 逐步对应的 finishReason(缺省 'completed')。 */
  readonly finishReasons?: (FinishReason | null)[];
  /** 摘要调用的应答队列;Error = 摘要失败;队列空 = 默认失败。 */
  readonly summaries?: (StreamedMessagePart[] | Error)[];
}

export class ScriptedProvider implements ChatProvider {
  readonly name = 'scripted';
  readonly modelName = 'scripted-model';
  readonly thinkingEffort = null;
  /** 每次普通 generate 调用收到的历史快照。 */
  readonly histories: Message[][] = [];
  /** 每次 generate 调用收到的 tools(含普通调用与摘要调用)。 */
  readonly toolsPerCall: Tool[][] = [];
  /** 摘要调用收到的 tools(用于断言摘要调用不传 tools)。 */
  readonly summaryTools: Tool[][] = [];
  /** withThinking 收到的 effort 列表。 */
  readonly thinkingEfforts: ThinkingEffort[] = [];
  private readonly script: (StreamedMessagePart[] | Error)[];
  private readonly usages: (TokenUsage | null)[];
  private readonly finishReasons: (FinishReason | null)[];
  private readonly summaries: (StreamedMessagePart[] | Error)[];

  constructor(options: ScriptedProviderOptions) {
    this.script = [...options.script];
    this.usages = [...(options.usages ?? [])];
    this.finishReasons = [...(options.finishReasons ?? [])];
    this.summaries = [...(options.summaries ?? [])];
  }

  generate(
    systemPrompt: string,
    tools: Tool[],
    history: Message[],
    _options?: GenerateOptions,
  ): Promise<StreamedMessage> {
    // 摘要调用(system prompt 为摘要指令)走 summaries 队列。
    if (systemPrompt === SUMMARY_SYSTEM_PROMPT) {
      this.summaryTools.push(tools);
      this.toolsPerCall.push(tools);
      const summary = this.summaries.shift();
      if (summary === undefined) return Promise.reject(new Error('summary not scripted'));
      if (summary instanceof Error) return Promise.reject(summary);
      return Promise.resolve(streamOf(summary));
    }

    this.histories.push(history.map((m) => m));
    this.toolsPerCall.push(tools);
    const parts = this.script.shift();
    if (parts === undefined) return Promise.reject(new Error('script exhausted'));
    if (parts instanceof Error) return Promise.reject(parts);
    return Promise.resolve(
      streamOf(parts, this.usages.shift() ?? null, this.finishReasons.shift() ?? 'completed'),
    );
  }

  withThinking(effort: ThinkingEffort): ChatProvider {
    this.thinkingEfforts.push(effort);
    return this;
  }
}

export function streamOf(
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

export function toolCall(id: string, name: string, args: unknown): ToolCall {
  return { type: 'function', id, name, arguments: JSON.stringify(args) };
}

export function text(value: string): StreamedMessagePart {
  return { type: 'text', text: value };
}
