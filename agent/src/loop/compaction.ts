/**
 * 比率触发的上下文压缩(占位替换路径 + 切点安全规则)。
 *
 * 触发参数照搬 vendor/kimi-code
 * packages/agent-core-v2/src/agent/fullCompaction/strategy.ts 的
 * DEFAULT_COMPACTION_CONFIG:triggerRatio 0.85、reservedContextSize 50_000、
 * maxRecentMessages 4。
 *
 * 本模块只做「替换老工具结果为占位」的机械压缩;LLM 摘要压缩在
 * summarize.ts(参考 vendor fullCompaction:总结中段、保留最近消息),
 * 摘要失败时回退到本模块的占位替换。消息骨架(含 assistant.toolCalls 与
 * tool 消息的配对)完整保留——OpenAI 兼容 API 要求 tool 消息与 tool call
 * 严格配对,直接丢消息会破坏请求合法性。
 * token 估算用简单 heuristic(字符数 / 4 + 图片/音视频固定高估),不引入
 * tokenizer 依赖。
 * 切点安全规则(splitForCompaction)与 vendor canSplitAfter 同义:保留区
 * 必须以一条 user 消息开头(即切点落在上一轮完整交互结束之后、下一条
 * user 消息之前),保证当前进行中的 user 轮次永远不会被切进压缩区。
 */
import type { Message } from '#/message';

/** 压缩触发参数(默认值对齐 vendor DEFAULT_COMPACTION_CONFIG)。 */
export interface CompactionConfig {
  /** 模型上下文窗口(token)。<= 0 时永不压缩。 */
  readonly maxContextTokens: number;
  /** 用量达到 maxContextTokens * triggerRatio 即触发压缩。 */
  readonly triggerRatio: number;
  /** 预留输出空间:used + reserved >= max 同样触发(与 vendor 一致)。 */
  readonly reservedContextSize: number;
  /** 保留区最少包含的最近消息条数(再向前扩展到 user 边界)。 */
  readonly maxRecentMessages: number;
  /** 工具结果占位文本。 */
  readonly placeholder: string;
}

export const DEFAULT_COMPACTION_PARAMS = {
  triggerRatio: 0.85,
  reservedContextSize: 50_000,
  maxRecentMessages: 4,
  placeholder: '[已压缩]',
} as const;

export type CompactionOverrides = Partial<
  Omit<CompactionConfig, 'maxContextTokens'>
>;

export function createCompactionConfig(
  maxContextTokens: number,
  overrides: CompactionOverrides = {},
): CompactionConfig {
  return {
    maxContextTokens,
    triggerRatio: overrides.triggerRatio ?? DEFAULT_COMPACTION_PARAMS.triggerRatio,
    reservedContextSize:
      overrides.reservedContextSize ?? DEFAULT_COMPACTION_PARAMS.reservedContextSize,
    maxRecentMessages:
      overrides.maxRecentMessages ?? DEFAULT_COMPACTION_PARAMS.maxRecentMessages,
    placeholder: overrides.placeholder ?? DEFAULT_COMPACTION_PARAMS.placeholder,
  };
}

// ---------------------------------------------------------------------------
// token 估算 heuristic
// ---------------------------------------------------------------------------

const CHARS_PER_TOKEN = 4;
/** 图片固定高估(qwen-vl 一类高清图常见 1k+ tokens)。 */
const IMAGE_TOKEN_ESTIMATE = 1024;
/** 音频/视频固定高估。 */
const MEDIA_TOKEN_ESTIMATE = 2048;
/** 每条消息的结构开销(role 等)。 */
const MESSAGE_OVERHEAD_TOKENS = 4;
/** 每个 tool call 的结构开销(id/name 之外的部分)。 */
const TOOL_CALL_OVERHEAD_CHARS = 16;

export function estimateMessageTokens(message: Message): number {
  let chars = 0;
  let mediaTokens = 0;
  for (const part of message.content) {
    switch (part.type) {
      case 'text':
        chars += part.text.length;
        break;
      case 'think':
        chars += part.think.length;
        break;
      case 'image_url':
        mediaTokens += IMAGE_TOKEN_ESTIMATE;
        break;
      case 'audio_url':
      case 'video_url':
        mediaTokens += MEDIA_TOKEN_ESTIMATE;
        break;
    }
  }
  for (const call of message.toolCalls) {
    chars += call.name.length + (call.arguments?.length ?? 0) + TOOL_CALL_OVERHEAD_CHARS;
  }
  return Math.ceil(chars / CHARS_PER_TOKEN) + mediaTokens + MESSAGE_OVERHEAD_TOKENS;
}

export function estimateMessagesTokens(messages: readonly Message[]): number {
  let total = 0;
  for (const message of messages) {
    total += estimateMessageTokens(message);
  }
  return total;
}

// ---------------------------------------------------------------------------
// 触发与压缩
// ---------------------------------------------------------------------------

/** 与 vendor DefaultCompactionStrategy.shouldCompact 同义。 */
export function shouldCompact(
  messages: readonly Message[],
  config: CompactionConfig,
): boolean {
  if (config.maxContextTokens <= 0) return false;
  const used = estimateMessagesTokens(messages);
  if (used >= config.maxContextTokens * config.triggerRatio) return true;
  const reserved = config.reservedContextSize;
  return (
    reserved > 0 &&
    reserved < config.maxContextTokens &&
    used + reserved >= config.maxContextTokens
  );
}

export interface CompactionOutcome {
  /** 压缩后的消息数组(未触发或无可压时原样返回入参)。 */
  readonly messages: Message[];
  /** 是否实际改动了消息。 */
  readonly compacted: boolean;
  /** 被替换为占位的工具结果条数。 */
  readonly compactedToolResults: number;
}

export interface CompactionSplit {
  /** 压缩区起点(头部 system 消息之后)。 */
  readonly firstNonSystem: number;
  /** 保留区起点(压缩区终点,开区间;保证落在 user 消息上)。 */
  readonly keepFrom: number;
}

/**
 * 计算压缩区/保留区切点(安全边界,与 vendor canSplitAfter 同义):
 * 保留区 = 从末尾取 maxRecentMessages 条再向前扩展到最近的 user 消息;
 * 压缩区 = [firstNonSystem, keepFrom)。压缩区为空或找不到 user 边界时
 * 返回 undefined(宁愿不压也不切坏进行中的交互)。
 */
export function splitForCompaction(
  messages: readonly Message[],
  config: CompactionConfig,
): CompactionSplit | undefined {
  let firstNonSystem = 0;
  while (firstNonSystem < messages.length && messages[firstNonSystem]?.role === 'system') {
    firstNonSystem += 1;
  }

  let keepFrom = Math.max(firstNonSystem, messages.length - config.maxRecentMessages);
  while (keepFrom > firstNonSystem && messages[keepFrom]?.role !== 'user') {
    keepFrom -= 1;
  }
  if (messages[keepFrom]?.role !== 'user') return undefined;
  if (keepFrom <= firstNonSystem) return undefined; // 压缩区为空
  return { firstNonSystem, keepFrom };
}

/**
 * 超阈时把压缩区内的工具结果输出替换为占位文本。
 *
 * 保留区 = 最近的 user 轮次(切点规则见 splitForCompaction);找不到 user
 * 边界时放弃压缩(宁愿不压也不切坏进行中的交互)。
 *
 * force = true 时跳过 shouldCompact 的 heuristic 阈值判断(调用方已用真实
 * usage 判定超阈,或是用户手动触发);安全边界与幂等规则不受影响。
 */
export function compactMessages(
  messages: Message[],
  config: CompactionConfig,
  force = false,
): CompactionOutcome {
  const unchanged: CompactionOutcome = {
    messages,
    compacted: false,
    compactedToolResults: 0,
  };
  if (!force && !shouldCompact(messages, config)) return unchanged;

  const split = splitForCompaction(messages, config);
  if (split === undefined) return unchanged;
  const { firstNonSystem, keepFrom } = split;

  let compactedToolResults = 0;
  const next = messages.map((message, index) => {
    if (index < firstNonSystem || index >= keepFrom || message.role !== 'tool') {
      return message;
    }
    const only = message.content.length === 1 ? message.content[0] : undefined;
    if (only?.type === 'text' && only.text === config.placeholder) {
      return message; // 已是占位,幂等跳过
    }
    compactedToolResults += 1;
    return { ...message, content: [{ type: 'text' as const, text: config.placeholder }] };
  });

  if (compactedToolResults === 0) return unchanged;
  return { messages: next, compacted: true, compactedToolResults };
}
