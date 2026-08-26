/**
 * agent server 入口:npx tsx src/server/main.ts
 * 端口 AGENT_PORT(默认 8602),host AGENT_HOST(默认 127.0.0.1)。
 * AGENT_RESTORE_WORKSPACES:逗号分隔的 workspace 目录,启动时从这些目录的
 * .agent/sessions.db 恢复历史 session(可选)。
 * AGENT_WORKSPACE_REGISTRY_PATH:web 层工作区登记表路径;启动与 GET /sessions
 * 前 agent 据此自查恢复,缺省不启用。
 */
import { defaultEnvPath, mergeDotenvIntoProcessEnv } from '../llm/env.ts';

import { createAgentServer } from './app';

// 启动最早处把 `.env` 合并进 process.env(只补缺,不覆盖已导出的 shell 变量),
// 让 web 拉起/独立 tsx/shell 导出三条路径对 AGENT_* / LLM_* 等配置行为一致。
// runtime.py 显式注入的 AGENT_PORT / TOOLSERVER_URL / AGENT_RESTORE_WORKSPACES
// 会作为覆盖生效。
mergeDotenvIntoProcessEnv(defaultEnvPath());

const port = Number(process.env.AGENT_PORT ?? 8602);
const host = process.env.AGENT_HOST ?? '127.0.0.1';
const restoreWorkspaceDirs = (process.env.AGENT_RESTORE_WORKSPACES ?? '')
  .split(',')
  .map((dir) => dir.trim())
  .filter((dir) => dir !== '');
const workspaceRegistryPath = process.env.AGENT_WORKSPACE_REGISTRY_PATH;

const serverOptions = {
  ...(restoreWorkspaceDirs.length > 0 ? { restoreWorkspaceDirs } : {}),
  ...(workspaceRegistryPath !== undefined ? { workspaceRegistryPath } : {}),
};

const { server } = createAgentServer(serverOptions);

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
