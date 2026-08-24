/**
 * `.env` 解析与 LLM provider 配置加载。
 *
 * 语义对齐 traffic_analyzer/core/config_manager.py 的
 * `_load_env_llm_providers` / `_build_llm_config_from_env`:
 * - 优先读取 `LLM_PROVIDER_{i}_PROVIDER/_API_KEY/_MODEL/_BASE_URL` 系列(i 从 0 递增,
 *   允许跳号,只构建实际定义的 index);
 * - 没有 indexed 变量时回退到 legacy 单 provider(`VLM_PROVIDER`/`LLM_*`);
 * - `{PROVIDER}_API_KEY/_BASE_URL/_MODEL`(provider 名大写)覆盖同前缀的通用值;
 * - `LLM_AUTO_SWITCH=0/false/no/off` 时只保留第一个 provider;
 * - 否则第一个(primary)永远保留,后续候选按各自
 *   `LLM_PROVIDER_{i}_ENABLED`(0/false/no/off 禁用,其余/未设启用)过滤。
 *
 * 与 Python 版的差异(刻意简化):
 * - 手写简易 dotenv 解析,不引新依赖;支持引号包裹值与整行/行尾注释,
 *   不支持转义序列与多行值;
 * - 只解析单一路径,不做「config/.env 缺失时回退项目根 .env」;
 * - 缓存相关字段(ENABLE_CACHE/CACHE_MAX_SIZE/磁盘缓存)对 TS agent 无意义,未纳入。
 */
import { readFileSync } from 'node:fs';

/** 对齐 traffic_analyzer/models/config.py 的 LLMProviderConfig(裁剪后)。 */
export interface EnvLLMProviderConfig {
  provider: string;
  apiKey: string;
  baseUrl?: string | undefined;
  model: string;
  maxTokens: number;
  temperature: number;
  timeout: number;
  maxRetries: number;
}

/** LLMProviderConfig 的 pydantic 默认值。 */
const DEFAULTS = {
  provider: 'anthropic',
  model: 'claude-sonnet-4-6',
  maxTokens: 4096,
  temperature: 0.2,
  timeout: 300.0,
  maxRetries: 3,
} as const;

/** Python 端统一的 "禁用" 取值集合。 */
const FALSEY = new Set(['0', 'false', 'no', 'off']);

/** 解析 `.env` 文本为键值映射(简易 ini/dotenv 解析)。 */
export function parseDotenv(content: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const rawLine of content.split(/\r?\n/)) {
    let line = rawLine.trim();
    if (line === '' || line.startsWith('#')) continue;
    if (line.startsWith('export ')) line = line.slice('export '.length).trimStart();
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    if (key === '') continue;
    let value = line.slice(eq + 1).trim();
    const quote = value[0];
    if (value.length >= 2 && (quote === '"' || quote === "'") && value.endsWith(quote)) {
      value = value.slice(1, -1);
    } else {
      // 未加引号的值支持行尾注释(空格 + #)
      const commentIdx = value.indexOf(' #');
      if (commentIdx >= 0) value = value.slice(0, commentIdx).trimEnd();
    }
    env[key] = value;
  }
  return env;
}

/** 读取 `.env` 文件;文件缺失/不可读时返回空映射(对齐 dotenv_values 的宽松语义)。 */
export function loadDotenvFile(envPath: string): Record<string, string> {
  let content: string;
  try {
    content = readFileSync(envPath, 'utf8');
  } catch {
    return {};
  }
  return parseDotenv(content);
}

/**
 * 从 env 映射构建单个 provider 配置。
 *
 * @param prefix `null` 读 legacy `LLM_*`(+ `VLM_PROVIDER`);否则读 `{prefix}_*`,
 *   如 `LLM_PROVIDER_0_PROVIDER`。
 */
export function buildProviderConfig(
  env: Record<string, string>,
  prefix: string | null,
): EnvLLMProviderConfig {
  const varName = (base: string): string => (prefix === null ? `LLM_${base}` : `${prefix}_${base}`);

  const provider =
    (prefix === null ? (env['VLM_PROVIDER'] || env['LLM_PROVIDER']) : env[`${prefix}_PROVIDER`]) ||
    undefined;

  // Provider-specific API key 优先,通用/带前缀 key 兜底
  let apiKey = '';
  if (provider !== undefined) {
    apiKey = env[`${provider.toUpperCase()}_API_KEY`] ?? '';
  }
  if (apiKey === '') {
    apiKey = env[varName('API_KEY')] ?? '';
  }

  // 通用 base_url 先取,provider-specific 覆盖
  let baseUrl = env[varName('BASE_URL')] || undefined;
  if (provider !== undefined) {
    baseUrl = env[`${provider.toUpperCase()}_BASE_URL`] || baseUrl;
  }

  // 通用 model 先取,provider-specific 覆盖
  let model = env[varName('MODEL')] || DEFAULTS.model;
  if (provider !== undefined) {
    model = env[`${provider.toUpperCase()}_MODEL`] || model;
  }

  // 数值字段:解析失败时静默回退默认值(Python 端是记日志并跳过)
  const numeric = (base: string, fallback: number): number => {
    const raw = env[varName(base)];
    if (raw === undefined) return fallback;
    const n = Number(raw);
    return Number.isFinite(n) ? n : fallback;
  };

  return {
    provider: provider ?? DEFAULTS.provider,
    apiKey,
    baseUrl,
    model,
    maxTokens: numeric('MAX_TOKENS', DEFAULTS.maxTokens),
    temperature: numeric('TEMPERATURE', DEFAULTS.temperature),
    timeout: numeric('TIMEOUT', DEFAULTS.timeout),
    maxRetries: numeric('MAX_RETRIES', DEFAULTS.maxRetries),
  };
}

/**
 * 解析 `.env` 并返回 provider 配置列表(对齐 `_load_env_llm_providers`)。
 * 返回顺序即优先级顺序:第 0 个为 primary,其余为 failover 候选。
 */
export function loadEnvLLMProviders(envPath: string): EnvLLMProviderConfig[] {
  const env = loadDotenvFile(envPath);

  const indices = new Set<number>();
  for (const key of Object.keys(env)) {
    const match = /^LLM_PROVIDER_(\d+)_PROVIDER$/.exec(key);
    const idx = match?.[1];
    if (idx !== undefined) indices.add(Number(idx));
  }

  let providers: EnvLLMProviderConfig[];
  let origIndices: (number | null)[];
  if (indices.size === 0) {
    providers = [buildProviderConfig(env, null)];
    origIndices = [null];
  } else {
    // 只为实际定义的 index 构建,跳号不补空(对齐 Python 注释的 phantom provider 问题)
    providers = [];
    origIndices = [];
    for (const i of [...indices].sort((a, b) => a - b)) {
      providers.push(buildProviderConfig(env, `LLM_PROVIDER_${i}`));
      origIndices.push(i);
    }
  }

  const first = providers[0];
  if (first === undefined) return [];

  const autoSwitch = (env['LLM_AUTO_SWITCH'] ?? '').trim().toLowerCase();
  if (FALSEY.has(autoSwitch)) {
    return [first];
  }

  // Auto-switch 开:primary 永远保留;候选按原始 index 的 _ENABLED 过滤
  const kept: EnvLLMProviderConfig[] = [first];
  for (let pos = 1; pos < providers.length; pos++) {
    const orig = origIndices[pos];
    const config = providers[pos];
    if (config === undefined) continue;
    const enabled =
      orig === null ||
      !FALSEY.has((env[`LLM_PROVIDER_${orig}_ENABLED`] ?? '').trim().toLowerCase());
    if (enabled) kept.push(config);
  }
  return kept;
}
