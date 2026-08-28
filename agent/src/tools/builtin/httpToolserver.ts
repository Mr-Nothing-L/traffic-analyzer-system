/**
 * HTTP client for the local Python toolserver (traffic_analyzer/toolserver).
 *
 * Base URL defaults to http://127.0.0.1:8601 and can be overridden with the
 * TOOLSERVER_URL environment variable (or the explicit `baseUrl` option).
 * Non-2xx responses are mapped to the toolserver error contract
 * `{error: {code, message}}`; network failures map to `toolserver_unreachable`.
 *
 * 超时:undici 全局 fetch 的默认 headersTimeout 是 300s,而长任务端点
 * (track_suspects 可达 8-15 分钟)会触发 `fetch failed`(HeadersTimeoutError)。
 * 注意 Node 24 的 globalThis.fetch 不接受外部 dispatcher(报
 * "invalid onRequestStart method"),故默认实现改用 undici 包自带 fetch,
 * 统一走长超时 dispatcher(15 分钟);post 另支持 per-call timeoutMs
 * (AbortSignal)作兜底。
 */
import { Agent, fetch as undiciFetch } from 'undici';

export const DEFAULT_TOOLSERVER_URL = 'http://127.0.0.1:8601';

/** 长超时 dispatcher:track_suspects 等长任务端点响应可达 15 分钟。 */
const LONG_TIMEOUT_DISPATCHER = new Agent({
  headersTimeout: 15 * 60 * 1000,
  bodyTimeout: 15 * 60 * 1000,
});

export interface ToolserverErrorInfo {
  readonly code: string;
  readonly message: string;
  readonly status?: number;
}

export type ToolserverResult<T> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly error: ToolserverErrorInfo };

export interface ToolserverClientOptions {
  readonly baseUrl?: string | undefined;
  /** Injectable for tests; defaults to undici fetch(长超时 dispatcher)。 */
  readonly fetchImpl?: typeof fetch | undefined;
}

export class ToolserverClient {
  readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ToolserverClientOptions = {}) {
    const raw = options.baseUrl ?? process.env.TOOLSERVER_URL ?? DEFAULT_TOOLSERVER_URL;
    this.baseUrl = raw.replace(/\/+$/, '');
    this.fetchImpl =
      options.fetchImpl ??
      ((input, init) =>
        undiciFetch(input as Parameters<typeof undiciFetch>[0], {
          ...(init as object),
          // 全局 fetch 默认 headersTimeout=300s,长任务端点必然踩雷;
          // 统一换长超时 dispatcher(undici 包自带 fetch 才接受外部 dispatcher)。
          dispatcher: LONG_TIMEOUT_DISPATCHER,
        }) as unknown as Promise<Response>);
  }

  /** POST JSON to a toolserver endpoint, e.g. post('/tools/video_meta', {...}).
   * timeoutMs 为 per-call 兜底超时(AbortSignal),不传则只靠 dispatcher 长超时。 */
  async post<T>(
    endpoint: string,
    body: unknown,
    timeoutMs?: number,
  ): Promise<ToolserverResult<T>> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${endpoint}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
        ...(timeoutMs !== undefined ? { signal: AbortSignal.timeout(timeoutMs) } : {}),
      });
    } catch (error) {
      return {
        ok: false,
        error: {
          code: 'toolserver_unreachable',
          message: `toolserver 请求失败(${this.baseUrl}): ${errorMessage(error)}`,
        },
      };
    }

    const text = await response.text();
    const payload = tryParseJson(text);

    if (!response.ok) {
      const contract = extractErrorContract(payload);
      return {
        ok: false,
        error: {
          code: contract?.code ?? 'http_error',
          message:
            contract?.message ??
            `toolserver HTTP ${response.status}: ${text.slice(0, 500)}`,
          status: response.status,
        },
      };
    }

    return { ok: true, data: payload as T };
  }

  /** GET /health — used by integration smoke checks. */
  async health(): Promise<ToolserverResult<{ status: string; workspace: string }>> {
    try {
      const response = await this.fetchImpl(`${this.baseUrl}/health`, { method: 'GET' });
      const payload = tryParseJson(await response.text());
      if (!response.ok) {
        return {
          ok: false,
          error: { code: 'http_error', message: `toolserver HTTP ${response.status}`, status: response.status },
        };
      }
      return { ok: true, data: payload as { status: string; workspace: string } };
    } catch (error) {
      return {
        ok: false,
        error: { code: 'toolserver_unreachable', message: `toolserver 请求失败: ${errorMessage(error)}` },
      };
    }
  }
}

function tryParseJson(text: string): unknown {
  if (text === '') return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function extractErrorContract(payload: unknown): { code: string; message: string } | undefined {
  if (typeof payload !== 'object' || payload === null) return undefined;
  const error = (payload as Record<string, unknown>)['error'];
  if (typeof error !== 'object' || error === null) return undefined;
  const { code, message } = error as Record<string, unknown>;
  if (typeof code !== 'string' || typeof message !== 'string') return undefined;
  return { code, message };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
