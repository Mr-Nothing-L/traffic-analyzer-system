/**
 * Web fetch tool: read-only HTTP fetch with bounded size and timeout.
 *
 * Only http/https URLs are accepted; the response body is capped at 5MB.
 * text/html is converted to plain text by stripping script/style/tags and
 * collapsing whitespace; text/plain and application/json are returned as-is
 * (truncated to max_chars). No external dependencies beyond Node 18 fetch.
 */
import { lookup } from 'node:dns/promises';

import { z } from 'zod';

import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolResult,
  type ToolExecution,
} from '../contract';
import type { ToolsetEntrySpec } from './videoTools';
import { invalidInputResult } from './utils';

const TIMEOUT_MS = 30_000;
const MAX_BODY_BYTES = 5 * 1024 * 1024;
const DEFAULT_MAX_CHARS = 8_000;
const MAX_MAX_CHARS = 20_000;
const MAX_REDIRECTS = 5;

/** SSRF 豁免开关:纯内网部署需要抓取内网文档时设 AGENT_WEB_FETCH_ALLOW_PRIVATE=1。
 * 调用时读取(非模块加载时),便于测试与运行时切换。 */
function allowPrivate(): boolean {
  return process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE === '1';
}

class WebFetchError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = 'WebFetchError';
  }
}

/** 私网/回环/链路本地(含云元数据端点网段)地址判定。 */
function isPrivateAddress(ip: string): boolean {
  const normalized = ip.toLowerCase().replace(/^\[|\]$/g, '');
  if (normalized.includes(':')) {
    if (normalized === '::1' || normalized === '::') return true;
    // IPv4-mapped ::ffff:a.b.c.d:按 IPv4 规则判
    const mapped = normalized.match(/::ffff:(\d+\.\d+\.\d+\.\d+)$/);
    if (mapped !== null) return isPrivateAddress(mapped[1]!);
    const first = Number.parseInt(normalized.split(':')[0] ?? '0', 16);
    if (Number.isNaN(first)) return true; // 解析不了的按私网保守拒绝
    if ((first & 0xfe00) === 0xfc00) return true; // fc00::/7 唯一本地
    if ((first & 0xffc0) === 0xfe80) return true; // fe80::/10 链路本地
    return false;
  }
  const parts = normalized.split('.').map(Number);
  if (parts.length !== 4 || parts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) return true;
  const [a, b] = parts as [number, number, number, number];
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 169 && b === 254) return true; // 含 169.254.169.254 云元数据
  return false;
}

/** 抓取前的 SSRF 守卫:拒绝回环/私网/链路本地目标(IP 字面量直判,域名解析后判全部地址)。
 * 豁免:AGENT_WEB_FETCH_ALLOW_PRIVATE=1。 */
async function assertPublicUrl(rawUrl: string): Promise<void> {
  if (allowPrivate()) return;
  const host = new URL(rawUrl).hostname.toLowerCase().replace(/^\[|\]$/g, '');
  const block = (): WebFetchError =>
    new WebFetchError(
      `已拒绝访问非公网地址 ${host}(SSRF 防护;确需抓取内网请设置 AGENT_WEB_FETCH_ALLOW_PRIVATE=1)`,
      'WEB_FETCH_PRIVATE_BLOCKED',
    );
  if (
    host === 'localhost' ||
    host.endsWith('.localhost') ||
    host.endsWith('.local') ||
    host.endsWith('.internal')
  ) {
    throw block();
  }
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host.includes(':')) {
    if (isPrivateAddress(host)) throw block();
    return;
  }
  let addresses: { address: string }[];
  try {
    addresses = await lookup(host, { all: true });
  } catch {
    throw new WebFetchError(`域名解析失败:${host}`, 'WEB_FETCH_DNS_FAILED');
  }
  if (addresses.length === 0 || addresses.some((entry) => isPrivateAddress(entry.address))) {
    throw block();
  }
}

const webFetchInputSchema = z.strictObject({
  url: z.string().refine(
    (u) => {
      try {
        const parsed = new URL(u);
        return parsed.protocol === 'http:' || parsed.protocol === 'https:';
      } catch {
        return false;
      }
    },
    { message: 'url 必须是 http 或 https 协议的完整 URL' },
  ),
  max_chars: z.number().int().min(1).max(MAX_MAX_CHARS).optional().default(DEFAULT_MAX_CHARS),
});

/** Read response text, rejecting bodies larger than the byte limit. */
async function readResponseText(response: Response, maxBytes: number): Promise<string> {
  const contentLength = response.headers.get('content-length');
  if (contentLength) {
    const len = Number.parseInt(contentLength, 10);
    if (!Number.isNaN(len) && len > maxBytes) {
      throw new WebFetchError('响应体超过 5MB 上限，拒绝读取', 'WEB_FETCH_BODY_TOO_LARGE');
    }
  }
  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > maxBytes) {
    throw new WebFetchError('响应体超过 5MB 上限，拒绝读取', 'WEB_FETCH_BODY_TOO_LARGE');
  }
  return new TextDecoder('utf-8', { fatal: false }).decode(buffer);
}

/** Very simple HTML-to-text extraction: drop script/style/tags and collapse whitespace. */
function extractHtmlText(html: string): string {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&amp;/gi, '&')
    .replace(/&quot;/gi, '"')
    .replace(/\s+/g, ' ')
    .trim();
}

export function createWebFetchTool(spec: ToolsetEntrySpec): ExecutableTool {
  return {
    name: 'web_fetch',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown): ToolExecution {
      const parsed = webFetchInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('web_fetch', parsed.error);

      const url = parsed.data.url;
      const maxChars = parsed.data.max_chars;

      return {
        accesses: ToolAccesses.none(),
        approvalRule: `web_fetch(${url})`,
        timeoutMs: TIMEOUT_MS + 5_000,
        execute: async (ctx): Promise<ExecutableToolResult> => {
          const controller = new AbortController();
          let timedOut = false;
          const timeoutId = setTimeout(() => {
            timedOut = true;
            controller.abort();
          }, TIMEOUT_MS);
          const onParentAbort = () => controller.abort();
          ctx.signal.addEventListener('abort', onParentAbort, { once: true });

          try {
            // 手动跟随重定向:每一跳都过 SSRF 守卫(follow 模式无法拦截
            // 30x 跳向 127.0.0.1/私网的链路)。
            let response: Response;
            try {
              let currentUrl = url;
              for (let hop = 0; ; hop++) {
                await assertPublicUrl(currentUrl);
                response = await fetch(currentUrl, {
                  signal: controller.signal,
                  redirect: 'manual',
                });
                if (response.status < 300 || response.status >= 400) break;
                const location = response.headers.get('location');
                if (location === null) break;
                if (hop >= MAX_REDIRECTS) {
                  throw new WebFetchError(`重定向超过 ${MAX_REDIRECTS} 跳`, 'WEB_FETCH_TOO_MANY_REDIRECTS');
                }
                currentUrl = new URL(location, currentUrl).toString();
              }
            } catch (error) {
              if (error instanceof WebFetchError) {
                return { output: `${error.message} [${error.code}]`, isError: true };
              }
              throw error;
            }

            if (!response.ok) {
              return {
                output: `web_fetch 请求失败: HTTP ${response.status}`,
                isError: true,
              };
            }

            let text: string;
            try {
              text = await readResponseText(response, MAX_BODY_BYTES);
            } catch (error) {
              if (error instanceof WebFetchError) {
                return { output: `${error.message} [${error.code}]`, isError: true };
              }
              throw error;
            }

            const contentType = response.headers.get('content-type') || 'unknown';
            const normalizedType = contentType.split(';')[0]!.trim().toLowerCase();
            const content = normalizedType === 'text/html' ? extractHtmlText(text) : text;
            const truncated = content.length > maxChars;
            const outputContent = truncated ? content.slice(0, maxChars) : content;

            return {
              output: JSON.stringify({
                url: response.url,
                status: response.status,
                content_type: contentType,
                content: outputContent,
                truncated,
              }),
            };
          } catch (error) {
            if (error instanceof Error && error.name === 'AbortError') {
              if (timedOut) {
                return { output: 'web_fetch 请求超时(30s)', isError: true };
              }
              return { output: 'web_fetch 请求被中止', isError: true };
            }
            return {
              output: `web_fetch 网络请求失败: ${error instanceof Error ? error.message : String(error)}`,
              isError: true,
            };
          } finally {
            clearTimeout(timeoutId);
            ctx.signal.removeEventListener('abort', onParentAbort);
          }
        },
      };
    },
  };
}
