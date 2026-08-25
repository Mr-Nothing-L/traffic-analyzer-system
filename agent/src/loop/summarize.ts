/**
 * LLM 摘要压缩(参考 vendor/kimi-code fullCompaction:总结中段、保留最近消息)。
 *
 * 触发判定与切点安全边界完全复用 compaction.ts(只在 user 消息前切,
 * tool_call/tool 配对完整);本模块替换的是压缩内容本身:
 * 把压缩区(安全边界之前的历史)序列化后交给同一个 ChatProvider 做一次
 * 摘要调用(不传 tools、独立 60s 超时、maxCompletionTokens 2048 上限),
 * 摘要作为一条 user 消息(「[此前对话摘要]」前缀)替换整个压缩区,
 * 保留区不变。摘要调用失败/超时/返回空 → 回退 compaction.ts 的占位替换,
 * 绝不让 loop 因压缩而崩。
 *
 * 防递归:摘要走 kosong generate() 直调 provider,不经过 runAgentLoop,
 * 摘要请求自身永远不会再触发压缩。
 */
import { isAbortError } from '#/errors';
import { generate } from '#/generate';
import { createUserMessage, extractText, type Message } from '#/message';
import type { ChatProvider } from '#/provider';

import {
  compactMessages,
  estimateMessagesTokens,
  shouldCompact,
  splitForCompaction,
  type CompactionConfig,
  type CompactionOutcome,
} from './compaction';

/** 摘要调用超时(ms)。 */
export const SUMMARY_TIMEOUT_MS = 60_000;
/** 摘要输出的 completion token 上限(经 provider.withMaxCompletionTokens 施加)。 */
export const SUMMARY_MAX_TOKENS = 2048;
/** 摘要消息正文前缀(user 消息)。 */
export const SUMMARY_PREFIX = '[此前对话摘要]';

/**
 * 摘要 system prompt(中文,对齐交通事件检测场景)。
 * 测试的假 provider 以此判断「这是一次摘要调用」并分场景返回。
 */
export const SUMMARY_SYSTEM_PROMPT = [
  '你是对话上下文压缩助手。用户会给你一段交通事件检测 agent 的多轮对话历史,',
  '请输出一份紧凑的中文摘要,必须保留:',
  '1) 当前正在分析的视频路径与已获取的元信息(时长/分辨率/帧率等);',
  '2) 已做过的工具调用及其关键发现(逐事件:有无事件、证据帧、置信度);',
  '3) 已形成但尚未提交的结论;',
  '4) 用户的原始要求与偏好。',
  '只输出摘要正文,不要复述本指令,不要使用列表以外的格式包装,不超过 1500 字。',
].join('');

export interface SummarizedCompactionOutcome extends CompactionOutcome {
  /** true = LLM 摘要替换压缩区;false = 占位替换或未压缩。 */
  readonly summarized: boolean;
  /** 压缩前的 token 估算(heuristic)。 */
  readonly beforeTokens: number;
  /** 压缩后的 token 估算(heuristic)。 */
  readonly afterTokens: number;
}

/**
 * 压缩入口(loop 自动触发与手动 /compact 共用):
 * 先按 force/阈值判断是否需要压缩,再切出压缩区做 LLM 摘要;
 * 摘要不可用时回退占位替换。父级 signal 取消时向上抛 AbortError。
 */
export async function compactMessagesWithSummary(
  messages: Message[],
  config: CompactionConfig,
  provider: ChatProvider,
  force: boolean,
  signal?: AbortSignal,
): Promise<SummarizedCompactionOutcome> {
  const beforeTokens = estimateMessagesTokens(messages);
  const unchanged: SummarizedCompactionOutcome = {
    messages,
    compacted: false,
    compactedToolResults: 0,
    summarized: false,
    beforeTokens,
    afterTokens: beforeTokens,
  };
  if (!force && !shouldCompact(messages, config)) return unchanged;

  const split = splitForCompaction(messages, config);
  if (split === undefined) return unchanged;

  const zone = messages.slice(split.firstNonSystem, split.keepFrom);
  const summary = await callSummary(zone, provider, signal);
  if (summary !== null) {
    const summaryMessage = createUserMessage(`${SUMMARY_PREFIX}\n\n${summary}`);
    const next: Message[] = [
      ...messages.slice(0, split.firstNonSystem),
      summaryMessage,
      ...messages.slice(split.keepFrom),
    ];
    return {
      messages: next,
      compacted: true,
      compactedToolResults: zone.filter((m) => m.role === 'tool').length,
      summarized: true,
      beforeTokens,
      afterTokens: estimateMessagesTokens(next),
    };
  }

  // 回退:占位替换(compactMessages 内部仍做空区间/幂等保护)
  const fallback = compactMessages(messages, config, true);
  return {
    ...fallback,
    summarized: false,
    beforeTokens,
    afterTokens: estimateMessagesTokens(fallback.messages),
  };
}

/**
 * 摘要调用:带独立 60s 超时(超时 abort 自己的 controller,回退占位);
 * 父级 signal 取消(用户停止/断连)则原样上抛,由 loop 收尾 cancelled。
 * 失败/空响应返回 null。
 */
async function callSummary(
  zone: readonly Message[],
  provider: ChatProvider,
  signal: AbortSignal | undefined,
): Promise<string | null> {
  const capped = provider.withMaxCompletionTokens?.(SUMMARY_MAX_TOKENS) ?? provider;
  const controller = new AbortController();
  const onParentAbort = (): void => controller.abort();
  signal?.addEventListener('abort', onParentAbort, { once: true });
  const timer = setTimeout(() => controller.abort(), SUMMARY_TIMEOUT_MS);
  try {
    const result = await generate(
      capped,
      SUMMARY_SYSTEM_PROMPT,
      [], // 摘要不传 tools
      [createUserMessage(buildSummaryPrompt(zone))],
      undefined,
      { signal: controller.signal },
    );
    const text = extractText(result.message).trim();
    return text === '' ? null : text;
  } catch (error) {
    if (isAbortError(error) && signal?.aborted === true) throw error;
    return null; // 摘要超时/网络错误/空响应等:回退占位压缩
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onParentAbort);
  }
}

/** 每条消息的序列化上限(防超长工具结果把摘要请求本身打爆)。 */
const PER_MESSAGE_MAX_CHARS = 2000;

const ROLE_LABEL: Record<Message['role'], string> = {
  system: '系统',
  user: '用户',
  assistant: '助手',
  tool: '工具结果',
};

/** 把压缩区序列化成纯文本交给摘要模型(截断单条超长内容)。 */
function buildSummaryPrompt(zone: readonly Message[]): string {
  const lines: string[] = [
    '以下是需要压缩的对话历史(按时间序),请按 system 指令输出摘要:',
    '',
  ];
  for (const message of zone) {
    const text = truncate(extractText(message, '\n'));
    lines.push(`【${ROLE_LABEL[message.role]}】${text}`);
    for (const call of message.toolCalls) {
      lines.push(`  调用工具 ${call.name}(${truncate(call.arguments ?? '', 500)})`);
    }
  }
  return lines.join('\n');
}

function truncate(text: string, max: number = PER_MESSAGE_MAX_CHARS): string {
  return text.length > max ? `${text.slice(0, max)}…(已截断)` : text;
}
