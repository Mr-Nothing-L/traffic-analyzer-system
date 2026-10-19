// 工具类节点(step/par/sub)共用的折叠态判定:按宿主 ChatAnalysisFlow
// 持有的 expandedTools 集合(会话内按工具条目 id 记忆)。
import type { AgentToolEntry } from '../../../stores/agentchat'

export function entryOpen(
  expandedTools: Set<string> | undefined,
  e: AgentToolEntry | undefined,
): boolean {
  return !!e && (expandedTools?.has(e.id) ?? false)
}

/** 明细可见 = 已展开且有内容可看(未完成且无子代理时间线时点开也不渲染)。 */
export function detailVisible(
  expandedTools: Set<string> | undefined,
  e: AgentToolEntry | undefined,
): boolean {
  return entryOpen(expandedTools, e) && !!e && (e.done || e.children.length > 0)
}
