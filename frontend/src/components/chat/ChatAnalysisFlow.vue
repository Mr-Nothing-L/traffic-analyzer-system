<script setup lang="ts">
/** 分析链路流程图(W6):纵向时间线 + 阶段分组,数据由 utils/analysisFlow 推导,
 * 本组件只做展示编排:
 * - 冻结态(默认):检测卡内一行摘要「N 轮循环 · M 次调用 · K 子代理 · X 秒」
 *   (无数据段静默省略),点击展开完整阶段树;
 * - 实时态(realtime):对话区底部随 entries 生长的完整树,当前步脉冲高亮、
 *   失败步红色 + 「已重试」标记,无摘要行。
 * 节点渲染拆分至 aflow/ 子组件(按节点类型:think/text/step/par/sub),
 * 展开态(工具/思考/说明)由宿主按条目 id 记忆并下传;节点全文复制的
 * copiedKey 也在宿主统一管理(与 ChatToolDetail 取证目录复制同款低调交互,
 * 成功图标变 ✓ 一秒,失败静默;kind:id 区分同一条目的思考与说明)。
 * 面板头部「全部展开/全部折叠」同时作用于工具、思考与说明节点。
 * 阶段标题/步骤标签/按钮为骨架 → 像素字体;思考摘要/全文与子结论为说明文字
 * → sans;耗时数值 → mono。颜色全部 tokens 变量(design.md §8 禁 inline 色);
 * 图标一律 UiIcon(§8 禁 emoji)。共享样式在 aflow/aflow.css。 */
import { computed, reactive } from 'vue'
import UiIcon from '../UiIcon.vue'
import type { AnalysisFlow, FlowNode, FlowStep, FlowSubagent } from '../../utils/analysisFlow'
import type { AgentToolEntry } from '../../stores/agentchat'
import { useCopyFeedback } from '../../composables/useCopyFeedback'
import AflowParNode from './aflow/AflowParNode.vue'
import AflowStepNode from './aflow/AflowStepNode.vue'
import AflowSubNode from './aflow/AflowSubNode.vue'
import AflowThinkNode from './aflow/AflowThinkNode.vue'
import AflowTextNode from './aflow/AflowTextNode.vue'
import type { Item, Row } from './aflow/types'
import './aflow/aflow.css'

const props = defineProps<{
  flow: AnalysisFlow
  /** 实时态:恒展开的完整树 + 当前步脉冲,不显示折叠摘要行。 */
  realtime?: boolean
  /** 冻结态展开覆写(SSR 测试直渲染用);缺省用本地点击状态。 */
  open?: boolean
  /** 节点展开态(按工具条目 id,宿主持有;实时态与冻结态共享同一份,会话内记忆)。 */
  expandedTools?: Set<string>
  /** 思考节点展开态(按 assistant 条目 id,宿主持有,机制同 expandedTools)。 */
  expandedThinks?: Set<string>
  /** 说明节点展开态(按 assistant 条目 id,宿主持有,机制同 expandedTools)。 */
  expandedTexts?: Set<string>
}>()

const emit = defineEmits<{
  'toggle-tool': [id: string]
  'toggle-all-tools': [ids: string[], open: boolean]
  'toggle-think': [id: string]
  'toggle-all-thinks': [ids: string[], open: boolean]
  'toggle-text': [id: string]
  'toggle-all-texts': [ids: string[], open: boolean]
  preview: [url: string]
}>()

/** 冻结态默认折叠为一行摘要;open prop 覆写。 */
const localOpen = reactive({ v: false })
const expanded = computed(() => props.realtime === true || props.open === true || localOpen.v)

/** 步骤状态:active 进行中优先(实时态「最新推进位置」可能标在刚完成的步上,
 * 见 buildAnalysisFlow 的末步标记);其次 ok 成功;其余失败。 */
function stateOf(ok: boolean, active?: boolean): string {
  if (active) return 'is-run'
  return ok ? 'is-ok' : 'is-fail'
}

/** 耗时纯文本(mono):96s;<500ms 显示 <1s,不显示 0s 假精度。 */
function fmtDur(ms: number | null): string {
  if (ms === null) return ''
  if (ms < 500) return '<1s'
  return `${Math.round(ms / 1000)}s`
}

/** mkRow 实际读取的最小结构:FlowStep 与子代理分支({inner})的字段子集
 * (后者无 name/label,模板也只渲染 state/dur,见 aflow/AflowSubNode)。 */
type StepRowSource = Pick<FlowStep, 'durationMs' | 'ok'> &
  Partial<Pick<FlowStep, 'label' | 'active' | 'retried'>>

function mkRow(s: StepRowSource): Row {
  return {
    label: s.label ?? '',
    dur: fmtDur(s.durationMs),
    state: stateOf(s.ok, s.active),
    retried: s.retried === true,
  }
}

function asSub(n: FlowNode): FlowSubagent {
  return n as FlowSubagent
}

const phaseViews = computed(() =>
  props.flow.phases.map((ph) => ({
    key: ph.key as string,
    title: ph.title,
    icon: ph.icon,
    items: ph.nodes.map((n): Item => {
      if ('kind' in n && n.kind === 'think') {
        return {
          t: 'think',
          id: n.id,
          text: n.text,
          live: n.live === true,
          dur: n.generateMs !== undefined ? fmtDur(n.generateMs) : '',
        }
      }
      if ('kind' in n && n.kind === 'text') {
        return { t: 'text', id: n.id, text: n.text, live: n.live === true }
      }
      if ('kind' in n) {
        return { t: 'par', rows: n.steps.map(mkRow), entries: n.steps.map((s) => s.entry) }
      }
      if ('inner' in n) {
        const sub = asSub(n)
        return {
          t: 'sub',
          row: mkRow({ ...sub, ok: sub.ok === true }),
          task: sub.task,
          conclusion: sub.conclusion ?? '',
          entry: sub.entry,
        }
      }
      return { t: 'step', row: mkRow(n), entry: n.entry }
    }),
  })),
)

/** 摘要行:N 轮循环 · M 次调用 · K 子代理 · P 次审批 · X 秒(zero/null 段省略)。 */
const summaryText = computed(() => {
  const f = props.flow
  const parts = [`${f.loops} 轮循环`, `${f.toolCalls} 次调用`]
  if (f.subagents > 0) parts.push(`${f.subagents} 子代理`)
  if (f.approvals > 0) parts.push(`${f.approvals} 次审批`)
  if (f.totalMs !== null && f.totalMs >= 0) {
    const sec = Math.round(f.totalMs / 1000)
    parts.push(sec < 1 ? '<1 秒' : `${sec} 秒`)
  }
  return parts.join(' · ')
})

/* ---- 节点全文复制(思考/说明共用,copiedKey 以 kind:id 区分;
 * 反馈状态机共用 composables/useCopyFeedback,与 ChatToolDetail 同款) ---- */
const { copiedKey, copyWithFeedback } = useCopyFeedback()

/** 可展开节点条目 id 清单(全部展开/全部折叠按钮用)。 */
const expandableIds = computed(() => {
  const ids: string[] = []
  for (const ph of props.flow.phases) {
    for (const n of ph.nodes) {
      if ('kind' in n && n.kind === 'parallel') {
        for (const s of n.steps) if (s.entry) ids.push(s.entry.id)
      } else if (!('kind' in n) && n.entry) {
        ids.push(n.entry.id)
      }
    }
  }
  return ids
})

/** 思考节点条目 id 清单(全部展开/全部折叠与工具节点一并批量动作)。 */
const expandableThinkIds = computed(() => {
  const ids: string[] = []
  for (const ph of props.flow.phases) {
    for (const n of ph.nodes) {
      if ('kind' in n && n.kind === 'think') ids.push(n.id)
    }
  }
  return ids
})

/** 说明节点条目 id 清单(与工具、思考节点一并批量动作)。 */
const expandableTextIds = computed(() => {
  const ids: string[] = []
  for (const ph of props.flow.phases) {
    for (const n of ph.nodes) {
      if ('kind' in n && n.kind === 'text') ids.push(n.id)
    }
  }
  return ids
})

/** 面板工具行:批量动作同时下发工具、思考与说明三类节点 id。 */
function toggleAll(open: boolean) {
  emit('toggle-all-tools', expandableIds.value, open)
  emit('toggle-all-thinks', expandableThinkIds.value, open)
  emit('toggle-all-texts', expandableTextIds.value, open)
}
</script>

<template>
  <div class="aflow" :class="{ realtime }">
    <!-- 冻结态折叠头:一行摘要 + 旋转指示 -->
    <button
      v-if="!realtime"
      type="button"
      class="aflow-summary"
      :title="expanded ? '收起分析链路' : '展开分析链路'"
      @click="localOpen.v = !localOpen.v"
    >
      <span class="aflow-caret" :class="{ open: expanded }">▸</span>
      <span class="aflow-sum-text">{{ summaryText }}</span>
    </button>

    <div v-if="expanded && phaseViews.length" class="aflow-body">
      <!-- 节点全部展开/折叠(有可展开节点才出现;同时作用于工具与思考节点) -->
      <div v-if="expandableIds.length" class="aflow-toolbar">
        <button type="button" class="aflow-toolbar-btn" @click="toggleAll(true)">全部展开</button>
        <span class="aflow-toolbar-sep">·</span>
        <button type="button" class="aflow-toolbar-btn" @click="toggleAll(false)">全部折叠</button>
      </div>
      <section v-for="(ph, pi) in phaseViews" :key="ph.key" class="aflow-phase">
        <header class="aflow-phase-head">
          <UiIcon :name="ph.icon" :size="13" />
          <span class="aflow-phase-title">{{ ph.title }}</span>
        </header>
        <div class="aflow-nodes">
          <template v-for="(it, ni) in ph.items" :key="`${pi}:${ni}`">
            <AflowThinkNode
              v-if="it.t === 'think'"
              :item="it"
              :open="expandedThinks?.has(it.id) ?? false"
              :copied="copiedKey === `think:${it.id}`"
              @toggle="emit('toggle-think', it.id)"
              @copy="copyWithFeedback(`think:${it.id}`, it.text)"
            />
            <AflowTextNode
              v-else-if="it.t === 'text'"
              :item="it"
              :open="expandedTexts?.has(it.id) ?? false"
              :copied="copiedKey === `text:${it.id}`"
              @toggle="emit('toggle-text', it.id)"
              @copy="copyWithFeedback(`text:${it.id}`, it.text)"
            />
            <AflowStepNode
              v-else-if="it.t === 'step'"
              :item="it"
              :expanded-tools="expandedTools"
              @toggle="emit('toggle-tool', $event)"
              @preview="emit('preview', $event)"
            />
            <AflowParNode
              v-else-if="it.t === 'par'"
              :item="it"
              :expanded-tools="expandedTools"
              @toggle="emit('toggle-tool', $event)"
              @preview="emit('preview', $event)"
            />
            <AflowSubNode
              v-else
              :item="it"
              :expanded-tools="expandedTools"
              @toggle="emit('toggle-tool', $event)"
              @preview="emit('preview', $event)"
            />
          </template>
        </div>
      </section>
    </div>
  </div>
</template>
