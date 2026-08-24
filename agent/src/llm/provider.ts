/**
 * 从 `.env` 配置构造 kosong `ChatProvider`。
 *
 * 当前只支持 OpenAI 兼容协议的 provider(aliyun 视为 OpenAI 兼容,见
 * traffic_analyzer/core/vlm_provider_clients.py),经 kosong 的
 * `OpenAILegacyChatProvider`(chat-completions 流式)接入。本地 vLLM
 * endpoint 同样走该路径。anthropic/google 等原生协议尚未接入,会抛错。
 */
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { OpenAILegacyChatProvider } from '#/providers/openai-legacy';

import { loadEnvLLMProviders, type EnvLLMProviderConfig } from './env.ts';

/** 按 OpenAI 兼容协议处理的 provider 名(小写)。 */
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

/** 默认 `.env` 路径:仓库根的 traffic_analyzer/config/.env。 */
export function defaultEnvPath(): string {
  return resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../traffic_analyzer/config/.env',
  );
}

/**
 * 读取 `envPath` 的 `LLM_PROVIDER_*` 配置,取 primary(第 0 个)构造 provider。
 * failover 候选的调度不在本层,由上层 loop 自行遍历 `loadEnvLLMProviders`。
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
  const provider = new OpenAILegacyChatProvider({
    // 本地 vLLM 不校验 key,但 openai SDK 要求非空;缺省时用占位值
    apiKey: config.apiKey === '' ? 'EMPTY' : config.apiKey,
    baseUrl: config.baseUrl,
    model: config.model,
    maxTokens: config.maxTokens,
    generationKwargs: { temperature: config.temperature },
  });
  return { provider, model: config.model, config };
}
