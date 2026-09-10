/**
 * Unit tests for web_fetch.
 *
 * Starts a local Node HTTP server to exercise HTML extraction, JSON passthrough,
 * the 5MB body limit, and the 30s timeout path.
 */
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  isRunnableToolExecution,
  type ExecutableTool,
  type ExecutableToolResult,
} from '../contract';
import type { ToolsetEntrySpec } from './videoTools';
import { createWebFetchTool } from './webTools';

function spec(): ToolsetEntrySpec {
  return { description: 'desc:web_fetch', parameters: { type: 'object', properties: {} } };
}

async function execute(tool: ExecutableTool, input: unknown): Promise<ExecutableToolResult> {
  const execution = tool.resolveExecution(input);
  if (!isRunnableToolExecution(execution)) return execution;
  return execution.execute({
    toolCallId: 'test-call',
    signal: new AbortController().signal,
  });
}

async function startServer(handler: (req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse) => void): Promise<{ server: Server; url: string }> {
  const server = createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address() as AddressInfo;
  return { server, url: `http://127.0.0.1:${address.port}` };
}

describe('web_fetch', () => {
  let server: Server | undefined;

  // 本组测试打本地 127.0.0.1 服务器:豁免 SSRF 防护(防护本身由下方专项组覆盖)
  const originalEnv = process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE;
  beforeEach(() => {
    process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE = '1';
  });
  afterEach(() => {
    if (originalEnv === undefined) delete process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE;
    else process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE = originalEnv;
  });

  afterEach(async () => {
    if (server) {
      server.closeAllConnections?.();
      await new Promise<void>((resolve) => server!.close(resolve));
      server = undefined;
    }
  }, 10_000);

  it('extracts text from HTML, dropping script/style/tags and whitespace', async () => {
    const { server: s, url } = await startServer((_, res) => {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(
        '<html><head>' +
          '<script>console.log("ignored")</script>' +
          '<style>.hidden { display: none; }</style>' +
          '</head><body>' +
          '<h1>  Hello   World  </h1>' +
          '<p>Traffic event detection</p>' +
          '</body></html>',
      );
    });
    server = s;

    const result = await execute(createWebFetchTool(spec()), { url });
    expect(result.isError).toBeFalsy();

    const parsed = JSON.parse(result.output as string);
    expect(parsed.status).toBe(200);
    expect(parsed.content_type).toContain('text/html');
    expect(parsed.content).toContain('Hello World');
    expect(parsed.content).toContain('Traffic event detection');
    expect(parsed.content).not.toContain('ignored');
    expect(parsed.content).not.toContain('.hidden');
    expect(parsed.truncated).toBe(false);
  });

  it('returns JSON body verbatim and reports content_type', async () => {
    const payload = { event: 3, confidence: 0.9 };
    const { server: s, url } = await startServer((_, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(payload));
    });
    server = s;

    const result = await execute(createWebFetchTool(spec()), { url });
    expect(result.isError).toBeFalsy();

    const parsed = JSON.parse(result.output as string);
    expect(parsed.content_type).toBe('application/json');
    expect(JSON.parse(parsed.content)).toEqual(payload);
    expect(parsed.truncated).toBe(false);
  });

  it('rejects responses larger than 5MB', async () => {
    const body = 'x'.repeat(6 * 1024 * 1024);
    const { server: s, url } = await startServer((_, res) => {
      res.writeHead(200, {
        'content-type': 'text/plain',
        'content-length': String(Buffer.byteLength(body)),
      });
      res.end(body);
    });
    server = s;

    const result = await execute(createWebFetchTool(spec()), { url });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('5MB');
    expect(result.output).toContain('WEB_FETCH_BODY_TOO_LARGE');
  });

  it('returns an error when the server hangs past the 30s timeout', async () => {
    const { server: s, url } = await startServer(() => {
      // Intentionally never respond so the client times out.
    });
    server = s;

    const result = await execute(createWebFetchTool(spec()), { url });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('超时');
  }, 35_000);

  it('rejects non-http urls at resolve time', async () => {
    const result = await execute(createWebFetchTool(spec()), { url: 'file:///etc/passwd' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('url');
  });
});

describe('web_fetch SSRF 防护(P2-15)', () => {
  const originalEnv = process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE;

  afterEach(() => {
    if (originalEnv === undefined) delete process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE;
    else process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE = originalEnv;
  });

  async function runFetch(url: string): Promise<ExecutableToolResult> {
    const tool = createWebFetchTool(spec());
    return execute(tool, { url });
  }

  it('拦截回环/私网/链路本地 IP 与 localhost 域名(isError,不发起请求)', async () => {
    delete process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE;
    for (const url of [
      'http://127.0.0.1:8601/health',
      'http://localhost/tools',
      'http://10.1.2.3/x',
      'http://192.168.1.1/router',
      'http://169.254.169.254/latest/meta-data',
      'http://[::1]/',
      'http://172.16.0.1/internal',
    ]) {
      const result = await runFetch(url);
      expect(result.isError, url).toBe(true);
      expect(result.output, url).toContain('SSRF');
    }
  });

  it('重定向跳向私网地址同样被拦截(逐跳校验)', async () => {
    delete process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE;
    // 30x 目标是 127.0.0.1:守卫须在跟随前拒绝
    const result = await runFetch('http://127.0.0.1:1/redir');
    expect(result.isError).toBe(true);
  });

  it('AGENT_WEB_FETCH_ALLOW_PRIVATE=1 豁免:回环地址可达(本测试服务器)', async () => {
    process.env.AGENT_WEB_FETCH_ALLOW_PRIVATE = '1';
    const { server, url } = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{"ok":true}');
    });
    try {
      const result = await runFetch(url);
      expect(result.isError).not.toBe(true);
      const payload = JSON.parse(result.output) as { content: string };
      expect(payload.content).toBe('{"ok":true}');
    } finally {
      server.closeAllConnections?.();
      await new Promise<void>((resolve) => server.close(resolve));
    }
  });
});
