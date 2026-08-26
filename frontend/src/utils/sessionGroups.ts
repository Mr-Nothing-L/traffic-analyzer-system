/** 历史会话按工作区分组(参考 kimi-code 侧栏:工作区文件夹为组,组下挂会话)。
 * 当前工作区组永远最上(无标题、不可折叠、恒展开);其余工作区各一组
 * (标题 = 目录 basename,悬停 title 显示完整路径,默认折叠),组间按组内
 * 最近活跃倒序;无 workspaceDir 的旧会话归「未分组」组沉底。
 * 每组内按 lastActiveAt 倒序。 */
import type { AgentSessionInfo } from '../stores/agentchat'

export interface SessionGroup {
  /** 组 key:当前组 'own',其他工作区组为 workspaceDir 完整路径,旧会话组 'ungrouped'。 */
  key: string
  /** 组标题:当前组为 ''(省略),其他组为目录 basename,旧会话组为「未分组」。 */
  label: string
  /** 悬停提示:完整路径(仅其他工作区组)。 */
  title?: string
  /** 是否可折叠(当前工作区组不可折叠,始终展开)。 */
  collapsible: boolean
  items: AgentSessionInfo[]
}

/** 目录 basename(兼容 / 与 \ 分隔,尾部分隔符容错)。 */
export function wsBasename(dir: string): string {
  return dir.split(/[\\/]/).filter(Boolean).pop() || dir
}

export function groupSessionsByWorkspace(
  sessions: AgentSessionInfo[],
  currentWs: string | null,
): SessionGroup[] {
  const sorted = [...sessions].sort((a, b) => (b.lastActiveAt ?? 0) - (a.lastActiveAt ?? 0))
  const own: AgentSessionInfo[] = []
  const byDir = new Map<string, AgentSessionInfo[]>()
  const ungrouped: AgentSessionInfo[] = []
  for (const s of sorted) {
    if (currentWs && s.workspaceDir === currentWs) {
      own.push(s)
    } else if (!s.workspaceDir) {
      ungrouped.push(s)
    } else {
      const list = byDir.get(s.workspaceDir) ?? []
      list.push(s)
      byDir.set(s.workspaceDir, list)
    }
  }
  // 组间排序:组内最近活跃倒序(sorted 已保证每组首条即最近一条)
  const others: SessionGroup[] = [...byDir.entries()]
    .sort((a, b) => (b[1][0]?.lastActiveAt ?? 0) - (a[1][0]?.lastActiveAt ?? 0))
    .map(([dir, items]) => ({
      key: dir,
      label: wsBasename(dir),
      title: dir,
      collapsible: true,
      items,
    }))
  return [
    ...(own.length ? [{ key: 'own', label: '', collapsible: false, items: own }] : []),
    ...others,
    ...(ungrouped.length
      ? [{ key: 'ungrouped', label: '未分组', collapsible: true, items: ungrouped }]
      : []),
  ]
}
