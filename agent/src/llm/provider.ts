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

/** 默认思考 token 预算(qwen3 chat_template_kwargs.thinking_budget)。 */
const DEFAULT_THINKING_BUDGET = 4096;

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

/**
 * 读取 AGENT_THINKING_BUDGET(默认 4096;<=0 表示不设预算)。
 * qwen3 的 thinking_budget 是思考的独立软预算(vLLM chat_template_kwargs
 * 支持,实测有效):思考到预算即被强制收尾,避免「Let me… Actually…」式
 * 犹豫循环烧穿与输出共享的 maxTokens(对齐 deepseek-harness 的
 * thinkingBudgets 分层思路)。
 */
export function resolveThinkingBudget(env: NodeJS.ProcessEnv = process.env): number | null {
  const raw = env.AGENT_THINKING_BUDGET;
  if (raw === undefined) return DEFAULT_THINKING_BUDGET;
  const parsed = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(parsed)) return DEFAULT_THINKING_BUDGET;
  return parsed > 0 ? parsed : null;
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
      temperature: config.temperature,
      // qwen3 系走 chat_template_kwargs 显式控制思考开关与思考预算
      // (AGENT_ENABLE_THINKING 默认开 / AGENT_THINKING_BUDGET 默认 4096,
      // vLLM 均支持)。预算只在思考开启时下发。
      chat_template_kwargs: (() => {
        const enabled = isThinkingEnabled();
        const budget = enabled ? resolveThinkingBudget() : null;
        return {
          enable_thinking: enabled,
          ...(budget !== null ? { thinking_budget: budget } : {}),
        };
      })(),
    },
  });
  return { provider, model: config.model, config };
}
