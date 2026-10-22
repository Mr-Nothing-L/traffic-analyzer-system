// 分析链路流程图节点组件的共享展示模型:由 ChatAnalysisFlow 从推导层
// (utils/analysisFlow 的 FlowNode)规约而来,节点子组件(think/text/step/
// par/sub)按 t 判别消费。纯类型,无运行时。
import type { AgentToolEntry } from '../../../stores/agentchat'

/** 节点行摘要(状态点/标签/耗时/重试标记的展示参数)。 */
export interface Row {
  label: string
  dur: string
  state: string
  retried: boolean
}

/** 规约后的扁平节点:宿主按 t 分发给对应子组件渲染。 */
export type Item =
  | { t: 'think'; id: string; text: string; live: boolean; dur: string }
  | { t: 'text'; id: string; text: string; live: boolean }
  | { t: 'step'; row: Row; entry?: AgentToolEntry }
  | { t: 'par'; rows: Row[]; entries: Array<AgentToolEntry | undefined> }
  | { t: 'sub'; row: Row; task: string; conclusion: string; entry?: AgentToolEntry }

/** 节点统一 emit:工具节点按条目 id 折叠/展开,思考/说明按条目 id,
 * 复制携带 (kind, id, text) 三元组(宿主统一管理 copiedKey)。 */
export type NodeCopyEmit = [kind: 'think' | 'text', id: string, text: string]
