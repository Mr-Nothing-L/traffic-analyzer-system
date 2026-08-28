/**
 * agent 运行时的 HTTP + SSE 路由表。
 *
 * 把原先 app.ts 中顺序排列的正则路由瀑布改为声明式路由数组,保留匹配顺序
 * (/sessions/{id} 的子路由先于 /sessions/{id});请求分发、请求体读取与通用
 * HTTP/SSE 辅助函数也放在这里。app.ts 只负责依赖装配、生命周期与业务处理函数。
 */
import type { IncomingMessage, ServerResponse } from 'node:http';

const MAX_BODY_BYTES = 16 * 1024 * 1024;

export async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    size += buf.length;
    if (size > MAX_BODY_BYTES) throw new Error('request body too large');
    chunks.push(buf);
  }
  const raw = Buffer.concat(chunks).toString('utf8').trim();
  if (raw === '') return {};
  return JSON.parse(raw) as unknown;
}

export function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(payload);
}

export function sendError(res: ServerResponse, status: number, code: string, message: string): void {
  sendJson(res, status, { error: { code, message } });
}

export function writeSseEvent(res: ServerResponse, event: unknown): void {
  res.write(`data: ${JSON.stringify(event)}\n\n`);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/**
 * 路由分发所需的业务处理函数集合,由 app.ts 装配后注入。
 * 每个端点的方法/路径/状态码/错误体/SSE 语义与旧实现保持一致。
 */
export interface RequestContext {
  readonly handleHealth: (res: ServerResponse) => void;
  readonly handleCreateSession: (res: ServerResponse, body: unknown) => void;
  readonly handleListSessions: (res: ServerResponse) => void;
  readonly handleRestoreWorkspace: (res: ServerResponse, body: unknown) => void;
  readonly handleGetHistory: (res: ServerResponse, sessionId: string) => void;
  readonly handleGetEvents: (res: ServerResponse, sessionId: string, url: URL) => void;
  readonly handleGetMedia: (res: ServerResponse, sessionId: string, name: string) => void;
  readonly handleCompact: (res: ServerResponse, sessionId: string) => Promise<void>;
  readonly handleRecall: (res: ServerResponse, sessionId: string, body: unknown) => void;
  readonly handleSetMode: (res: ServerResponse, sessionId: string, body: unknown) => void;
  readonly handleCancel: (res: ServerResponse, sessionId: string) => void;
  readonly handleSteer: (res: ServerResponse, sessionId: string, body: unknown) => void;
  readonly handleDeleteSession: (res: ServerResponse, sessionId: string) => void;
  readonly handleApproval: (res: ServerResponse, body: unknown) => void;
  readonly handleChat: (res: ServerResponse, body: unknown) => Promise<void>;
}

type RouteHandler = (
  ctx: RequestContext,
  res: ServerResponse,
  params: readonly string[],
  url: URL,
  body: unknown,
) => void | Promise<void>;

interface Route {
  readonly method: string;
  readonly pattern: RegExp;
  readonly needsBody: boolean;
  readonly handler: RouteHandler;
}

/**
 * 路由表:顺序敏感。
 * - /sessions 的精确匹配先于 /sessions/{id}。
 * - /sessions/{id} 的各子路由先于 /sessions/{id} 本身(DELETE)。
 * - 未匹配命中末尾 404。
 */
const routes: Route[] = [
  {
    method: 'GET',
    pattern: /^\/health$/,
    needsBody: false,
    handler: (ctx, res) => ctx.handleHealth(res),
  },
  {
    method: 'POST',
    pattern: /^\/sessions$/,
    needsBody: true,
    handler: (ctx, res, _params, _url, body) => ctx.handleCreateSession(res, body),
  },
  {
    method: 'GET',
    pattern: /^\/sessions$/,
    needsBody: false,
    handler: (ctx, res) => ctx.handleListSessions(res),
  },
  {
    method: 'POST',
    pattern: /^\/workspaces\/restore$/,
    needsBody: true,
    handler: (ctx, res, _params, _url, body) => ctx.handleRestoreWorkspace(res, body),
  },
  {
    method: 'GET',
    pattern: /^\/sessions\/([^/]+)\/history$/,
    needsBody: false,
    handler: (ctx, res, params) => ctx.handleGetHistory(res, params[0]!),
  },
  {
    method: 'GET',
    pattern: /^\/sessions\/([^/]+)\/events$/,
    needsBody: false,
    handler: (ctx, res, params, url) => ctx.handleGetEvents(res, params[0]!, url),
  },
  {
    method: 'GET',
    pattern: /^\/sessions\/([^/]+)\/media\/([0-9a-f]{64}\.(?:jpg|png))$/,
    needsBody: false,
    handler: (ctx, res, params) => ctx.handleGetMedia(res, params[0]!, params[1]!),
  },
  {
    method: 'POST',
    pattern: /^\/sessions\/([^/]+)\/compact$/,
    needsBody: false,
    handler: (ctx, res, params) => ctx.handleCompact(res, params[0]!),
  },
  {
    method: 'POST',
    pattern: /^\/sessions\/([^/]+)\/recall$/,
    needsBody: true,
    handler: (ctx, res, params, _url, body) => ctx.handleRecall(res, params[0]!, body),
  },
  {
    method: 'POST',
    pattern: /^\/sessions\/([^/]+)\/mode$/,
    needsBody: true,
    handler: (ctx, res, params, _url, body) => ctx.handleSetMode(res, params[0]!, body),
  },
  {
    method: 'POST',
    pattern: /^\/sessions\/([^/]+)\/cancel$/,
    needsBody: false,
    handler: (ctx, res, params) => ctx.handleCancel(res, params[0]!),
  },
  {
    method: 'POST',
    pattern: /^\/sessions\/([^/]+)\/steer$/,
    needsBody: true,
    handler: (ctx, res, params, _url, body) => ctx.handleSteer(res, params[0]!, body),
  },
  {
    method: 'DELETE',
    pattern: /^\/sessions\/([^/]+)$/,
    needsBody: false,
    handler: (ctx, res, params) => ctx.handleDeleteSession(res, params[0]!),
  },
  {
    method: 'POST',
    pattern: /^\/approval$/,
    needsBody: true,
    handler: (ctx, res, _params, _url, body) => ctx.handleApproval(res, body),
  },
  {
    method: 'POST',
    pattern: /^\/chat$/,
    needsBody: true,
    handler: (ctx, res, _params, _url, body) => ctx.handleChat(res, body),
  },
];

/**
 * 由 RequestContext 创建 node:http request listener。
 * 内部包含路由匹配、按需读取 JSON 请求体、404 回退与全局异常处理。
 */
export function createRouter(ctx: RequestContext) {
  return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    try {
      for (const route of routes) {
        if (route.method !== req.method) continue;
        const match = route.pattern.exec(url.pathname);
        if (match === null) continue;
        const body = route.needsBody ? await readJsonBody(req) : undefined;
        await route.handler(ctx, res, match.slice(1), url, body);
        return;
      }
      sendError(res, 404, 'not_found', `${req.method ?? ''} ${url.pathname}`);
    } catch (error) {
      if (!res.headersSent) {
        sendError(res, 400, 'bad_request', error instanceof Error ? error.message : String(error));
      } else {
        res.end();
      }
    }
  };
}
