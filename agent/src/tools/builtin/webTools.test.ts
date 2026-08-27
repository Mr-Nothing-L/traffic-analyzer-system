/**
 * Unit tests for web_fetch.
 *
 * Starts a local Node HTTP server to exercise HTML extraction, JSON passthrough,
 * the 5MB body limit, and the 30s timeout path.
 */
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it } from 'vitest';

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
