/**
 * 工作区登记表读取:web 层(traffic_analyzer/web/agentproxy/runtime.py)把
 * 历史工作区路径写入 config/agent_workspaces.json,agent server 启动或
 * GET /sessions 前据此自查恢复,不再依赖代理层在列表前做 restore。
 *
 * 路径由环境变量 AGENT_WORKSPACE_REGISTRY_PATH 传入;未配置时缺省不启用。
 * 文件缺失/损坏/非数组均安全返回 []。
 */
import { readFileSync } from 'node:fs';

/**
 * 读取工作区登记表,返回规范化路径列表。文件不存在或内容非法时返回 [],
 * 不抛异常(旁路优化,不应影响会话列表)。
 */
export function readWorkspaceRegistry(registryPath: string): string[] {
  let raw: string;
  try {
    raw = readFileSync(registryPath, 'utf8');
  } catch {
    return [];
  }
  let data: unknown;
  try {
    data = JSON.parse(raw) as unknown;
  } catch {
    return [];
  }
  if (!Array.isArray(data)) return [];
  return data.filter((item): item is string => typeof item === 'string');
}
