/**
 * agent server 入口:npx tsx src/server/main.ts
 * 端口 AGENT_PORT(默认 8602),host AGENT_HOST(默认 127.0.0.1)。
 */
import { createAgentServer } from './app';

const port = Number(process.env.AGENT_PORT ?? 8602);
const host = process.env.AGENT_HOST ?? '127.0.0.1';

const { server } = createAgentServer();

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
