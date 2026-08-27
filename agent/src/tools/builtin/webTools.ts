/**
 * Web fetch tool: read-only HTTP fetch with bounded size and timeout.
 *
 * Only http/https URLs are accepted; the response body is capped at 5MB.
 * text/html is converted to plain text by stripping script/style/tags and
 * collapsing whitespace; text/plain and application/json are returned as-is
 * (truncated to max_chars). No external dependencies beyond Node 18 fetch.
 */
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

class WebFetchError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = 'WebFetchError';
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
            const response = await fetch(url, {
              signal: controller.signal,
              redirect: 'follow',
            });

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
