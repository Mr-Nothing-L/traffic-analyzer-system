/**
 * 从 `.env` 配置构造 kosong `ChatProvider`。
 *
 * 当前只支持 OpenAI 兼容协议的 provider(aliyun 视为 OpenAI 兼容,见
 * traffic_analyzer/core/vlm_provider_clients.py),经 kosong 的
 * `OpenAILegacyChatProvider`(chat-completions 流式)接入。本地 vLLM
 * endpoint 同样走该路径。anthropic/google 等原生协议尚未接入,会抛错。
 */
import { OpenAILegacyChatProvider, type ChatProvider } from './kosong';

import {
  defaultEnvPath,
  loadEnvLLMProviders,
  type EnvLLMProviderConfig,
} from './env.ts';

/** 视为「关闭思考」的 AGENT_ENABLE_THINKING 取值(小写比较)。 */
const THINKING_OFF_VALUES: ReadonlySet<string> = new Set(['0', 'false', 'no', 'off']);

/**
 * 读取 AGENT_ENABLE_THINKING(默认 true;0/false/no/off 为关)。
 * qwen3 类 thinking 模型关掉思考后简单问题的 completion 大幅下降,
 * 摘要等不需要长推理的调用可省一半以上时间。
 */
export function isThinkingEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  const raw = env.AGENT_ENABLE_THINKING;
  if (raw === undefined) return true;
  return !THINKING_OFF_VALUES.has(raw.trim().toLowerCase());
}

/** qwen3.8 思考档位(chat_template_kwargs.reasoning_effort;vLLM 服务端缺省 xhigh)。 */
export type ReasoningEffort = 'low' | 'medium' | 'xhigh';

const REASONING_EFFORT_VALUES: ReadonlySet<string> = new Set(['low', 'medium', 'xhigh']);

/** 默认思考档位(实测 medium 比服务端默认 xhigh 省 ~57% completion tokens)。 */
const DEFAULT_REASONING_EFFORT: ReasoningEffort = 'medium';

/**
 * 读取 AGENT_REASONING_EFFORT(默认 medium;可选 low/medium/xhigh,非法值回退 medium)。
 * qwen3.8 的 reasoning_effort 是模板级档位控制(vLLM 支持,实测生效),
 * 取代 thinking_budget 软预算(模型不遵守,不再默认下发)。
 */
export function resolveReasoningEffort(env: NodeJS.ProcessEnv = process.env): ReasoningEffort {
  const raw = env.AGENT_REASONING_EFFORT?.trim().toLowerCase();
  if (raw === undefined || raw === '') return DEFAULT_REASONING_EFFORT;
  return (REASONING_EFFORT_VALUES.has(raw) ? raw : DEFAULT_REASONING_EFFORT) as ReasoningEffort;
}

/**
 * 读取 AGENT_THINKING_BUDGET(缺省不下发;<=0 表示不设预算)。
 * qwen3 thinking_budget 是思考软预算(模型不遵守,实测基本无效),仅保留为
 * 显式调优手段;常规档位控制用 AGENT_REASONING_EFFORT。
 */
export function resolveThinkingBudget(env: NodeJS.ProcessEnv = process.env): number | null {
  const raw = env.AGENT_THINKING_BUDGET;
  if (raw === undefined) return null;
  const parsed = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(parsed)) return null;
  return parsed > 0 ? parsed : null;
}

/** 默认抽帧步骤思考档位(低档,见 agentLoop.framesThinkingEffort)。 */
const DEFAULT_FRAMES_REASONING_EFFORT: ReasoningEffort = 'low';

/**
 * 读取 AGENT_REASONING_EFFORT_FRAMES(默认 low;0/false/no/off 表示抽帧步不降档)。
 * 抽帧(多图)后的 generate 步逐帧分析倾向强,高档位容易烧穿 maxTokens
 * 造成 think-only 失败(只有思考没有正文/工具调用)。
 */
export function resolveFramesThinkingEffort(
  env: NodeJS.ProcessEnv = process.env,
): ReasoningEffort | null {
  const raw = env.AGENT_REASONING_EFFORT_FRAMES?.trim().toLowerCase();
  if (raw === undefined || raw === '') return DEFAULT_FRAMES_REASONING_EFFORT;
  if (THINKING_OFF_VALUES.has(raw)) return null;
  return (REASONING_EFFORT_VALUES.has(raw)
    ? raw
    : DEFAULT_FRAMES_REASONING_EFFORT) as ReasoningEffort;
}

/**
 * 返回「思考关闭版」provider:OpenAI 兼容(openai-legacy)走
 * chat_template_kwargs:{enable_thinking:false}(本地 vLLM/qwen3 支持);
 * 其余 provider 回退 kosong 通用 withThinking('off')。
 */
export function withThinkingDisabled(provider: ChatProvider): ChatProvider {
  if (provider instanceof OpenAILegacyChatProvider) {
    return provider.withGenerationKwargs({
      chat_template_kwargs: { enable_thinking: false },
    });
  }
  return provider.withThinking('off');
}
export const OPENAI_COMPATIBLE_PROVIDERS: ReadonlySet<string> = new Set([
  'openai',
  'aliyun',
  'vllm',
  'qwen',
  'openai-compatible',
  'openai_compatible',
]);

export interface ProviderFromEnv {
  /** kosong chat provider(OpenAI 兼容 chat-completions,默认流式)。 */
  provider: OpenAILegacyChatProvider;
  /** 实际上游的 model 名(与 provider.modelName 一致,冗余导出便于日志)。 */
  model: string;
  /** 生效的 primary provider 配置(`.env` 解析结果)。 */
  config: EnvLLMProviderConfig;
}

/** 读取数值型采样覆盖(未设置/非法返回 undefined = 不下发)。 */
function samplingOverride(name: string): number | undefined {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return undefined;
  const n = Number(raw);
  return Number.isFinite(n) ? n : undefined;
}

/**
 * 读取 `envPath` 的 `LLM_PROVIDER_*` 配置,取 primary(第 0 个)构造 provider。
 * TS agent 运行时不实现 failover 调度,只消费 configs[0]。
 */
export function createProviderFromEnv(envPath: string = defaultEnvPath()): ProviderFromEnv {
  const configs = loadEnvLLMProviders(envPath);
  const config = configs[0];
  if (config === undefined) {
    throw new Error(`[llm] no LLM provider resolved from env file: ${envPath}`);
  }
  if (!OPENAI_COMPATIBLE_PROVIDERS.has(config.provider.toLowerCase())) {
    throw new Error(
      `[llm] provider "${config.provider}" is not OpenAI-compatible; ` +
        `the TS agent runtime currently supports only: ${[...OPENAI_COMPATIBLE_PROVIDERS].join(', ')}`,
    );
  }
  // qwen3 这类 thinking 模型的推理 token 也计入 max_tokens;检测 agent 需要
  // 长思考 + 大型结构化输出(submit_detection 参数),4096 会把 tool call
  // arguments 截断。AGENT_MAX_TOKENS 显式覆盖;否则在 .env 配置值上兜底 16384。
  const envOverride = Number.parseInt(process.env.AGENT_MAX_TOKENS ?? '', 10);
  const AGENT_MAX_TOKENS_FLOOR = 16384;
  let maxTokens: number;
  if (Number.isFinite(envOverride)) {
    maxTokens = envOverride;
  } else {
    maxTokens = Math.max(config.maxTokens, AGENT_MAX_TOKENS_FLOOR);
    if (maxTokens !== config.maxTokens) {
      console.warn(
        `[llm] LLM_MAX_TOKENS=${config.maxTokens} below agent floor ${AGENT_MAX_TOKENS_FLOOR}; ` +
          `raising maxTokens to ${AGENT_MAX_TOKENS_FLOOR}. Set AGENT_MAX_TOKENS to override explicitly.`,
      );
    }
  }
  const provider = new OpenAILegacyChatProvider({
    // 本地 vLLM 不校验 key,但 openai SDK 要求非空;缺省时用占位值
    apiKey: config.apiKey === '' ? 'EMPTY' : config.apiKey,
    baseUrl: config.baseUrl,
    model: config.model,
    maxTokens,
    generationKwargs: {
      // AGENT_TEMPERATURE 覆盖 .env 的 LLM_PROVIDER_0_TEMPERATURE;
      // AGENT_TOP_P / AGENT_TOP_K 设置时下发(缺省由服务端决定)。
      // 用于采样参数 A/B 实验与防复读调优,不改 .env 即可临时切换。
      temperature: samplingOverride('AGENT_TEMPERATURE') ?? config.temperature,
      ...(samplingOverride('AGENT_TOP_P') !== undefined
        ? { top_p: samplingOverride('AGENT_TOP_P') }
        : {}),
      ...(samplingOverride('AGENT_TOP_K') !== undefined
        ? { top_k: samplingOverride('AGENT_TOP_K') }
        : {}),
      // qwen3 系走 chat_template_kwargs 显式控制思考开关与思考档位
      // (AGENT_ENABLE_THINKING 默认开 / AGENT_REASONING_EFFORT 默认 medium;
      // vLLM 服务端缺省 xhigh 为最高档)。thinking_budget 软预算仅在显式
      // 设置 AGENT_THINKING_BUDGET 时下发。档位只在思考开启时下发。
      chat_template_kwargs: (() => {
        const enabled = isThinkingEnabled();
        const budget = enabled ? resolveThinkingBudget() : null;
        return {
          enable_thinking: enabled,
          ...(enabled ? { reasoning_effort: resolveReasoningEffort() } : {}),
          ...(budget !== null ? { thinking_budget: budget } : {}),
        };
      })(),
    },
  });
  return { provider, model: config.model, config };
}
