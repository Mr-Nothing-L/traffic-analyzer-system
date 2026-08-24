/**
 * agent server 入口:npx tsx src/server/main.ts
 * 端口 AGENT_PORT(默认 8602),host AGENT_HOST(默认 127.0.0.1)。
 * AGENT_RESTORE_WORKSPACES:逗号分隔的 workspace 目录,启动时从这些目录的
 * .agent/sessions.db 恢复历史 session(可选)。
 */
import { createAgentServer } from './app';

const port = Number(process.env.AGENT_PORT ?? 8602);
const host = process.env.AGENT_HOST ?? '127.0.0.1';
const restoreWorkspaceDirs = (process.env.AGENT_RESTORE_WORKSPACES ?? '')
  .split(',')
  .map((dir) => dir.trim())
  .filter((dir) => dir !== '');

const { server } = createAgentServer(
  restoreWorkspaceDirs.length > 0 ? { restoreWorkspaceDirs } : {},
);

server.listen(port, host, () => {
  console.log(`[agent-server] listening on http://${host}:${port}`);
});

for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    server.close(() => {
      process.exit(0);
    });
  });
}
