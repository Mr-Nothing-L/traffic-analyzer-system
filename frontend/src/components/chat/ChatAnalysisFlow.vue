<script setup lang="ts">
/** 分析链路流程图(W6):纵向时间线 + 阶段分组,数据由 utils/analysisFlow 推导,
 * 本组件只做展示:
 * - 冻结态(默认):检测卡内一行摘要「N 轮循环 · M 次调用 · K 子代理 · X 秒」
 *   (无数据段静默省略),点击展开完整阶段树;
 * - 实时态(realtime):对话区底部随 entries 生长的完整树,当前步脉冲高亮、
 *   失败步红色 + 「已重试」标记,无摘要行。
 * 链路节点即工具条目:每个节点(步骤/并发 chip/子代理分支)默认折叠
 * (label + 耗时 + 状态),点击展开调用明细(ChatToolDetail:参数摘要 + 结果
 * 文本/图片/子代理时间线/取证视频),展开态由宿主按条目 id 记忆(expandedTools,
 * 会话内共享,实时态与冻结态同一份);面板头部提供「全部展开/全部折叠」。
 * 阶段标题/步骤标签/按钮为骨架 → 像素字体;thinking 摘要与子结论为说明文字
 * → sans;耗时数值 → mono。颜色全部 tokens 变量(design.md §8 禁 inline 色);
 * 图标一律 UiIcon(§8 禁 emoji)。 */
import { computed, reactive } from 'vue'
import UiIcon from '../UiIcon.vue'
import type { AnalysisFlow, FlowNode, FlowStep, FlowSubagent } from '../../utils/analysisFlow'
import type { AgentToolEntry } from '../../stores/agentchat'
import ChatToolDetail from './ChatToolDetail.vue'

const props = defineProps<{
  flow: AnalysisFlow
  /** 实时态:恒展开的完整树 + 当前步脉冲,不显示折叠摘要行。 */
  realtime?: boolean
  /** 冻结态展开覆写(SSR 测试直渲染用);缺省用本地点击状态。 */
  open?: boolean
  /** 节点展开态(按工具条目 id,宿主持有;实时态与冻结态共享同一份,会话内记忆)。 */
  expandedTools?: Set<string>
}>()

const emit = defineEmits<{
  'toggle-tool': [id: string]
  'toggle-all-tools': [ids: string[], open: boolean]
  preview: [url: string]
}>()

/** 冻结态默认折叠为一行摘要;open prop 覆写。 */
const localOpen = reactive({ v: false })
const expanded = computed(() => props.realtime === true || props.open === true || localOpen.v)

/* ---- 展示模型:把推导层的三种节点规约成模板友好的扁平结构 ---- */
interface Row {
  label: string
  dur: string
  state: string
  retried: boolean
}
type Item =
  | { t: 'step'; row: Row; entry?: AgentToolEntry }
  | { t: 'par'; rows: Row[]; entries: Array<AgentToolEntry | undefined> }
  | { t: 'sub'; row: Row; task: string; conclusion: string; entry?: AgentToolEntry }

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

function mkRow(s: FlowStep): Row {
  return {
    label: s.label,
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
    note: ph.thinkNote ?? '',
    items: ph.nodes.map((n): Item => {
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

/* ---- 节点折叠/展开:按来源工具条目 id(宿主的 expandedTools 记忆);
 * 无内容可看(未完成且无子代理时间线)时点开也不渲染明细,同旧工具气泡口径。 ---- */
function isOpen(e: AgentToolEntry | undefined): boolean {
  return !!e && (props.expandedTools?.has(e.id) ?? false)
}

function toggleNode(e: AgentToolEntry | undefined) {
  if (e) emit('toggle-tool', e.id)
}

function detailVisible(e: AgentToolEntry | undefined): boolean {
  return isOpen(e) && !!e && (e.done || e.children.length > 0)
}

/** 可展开节点条目 id 清单(全部展开/全部折叠按钮用)。 */
const expandableIds = computed(() => {
  const ids: string[] = []
  for (const ph of props.flow.phases) {
    for (const n of ph.nodes) {
      if ('kind' in n) {
        for (const s of n.steps) if (s.entry) ids.push(s.entry.id)
      } else if (n.entry) {
        ids.push(n.entry.id)
      }
    }
  }
  return ids
})
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
      <!-- 节点全部展开/折叠(有可展开节点才出现) -->
      <div v-if="expandableIds.length" class="aflow-toolbar">
        <button type="button" class="aflow-toolbar-btn" @click="emit('toggle-all-tools', expandableIds, true)">
          全部展开
        </button>
        <span class="aflow-toolbar-sep">·</span>
        <button type="button" class="aflow-toolbar-btn" @click="emit('toggle-all-tools', expandableIds, false)">
          全部折叠
        </button>
      </div>
      <section v-for="(ph, pi) in phaseViews" :key="ph.key" class="aflow-phase">
        <header class="aflow-phase-head">
          <UiIcon :name="ph.icon" :size="13" />
          <span class="aflow-phase-title">{{ ph.title }}</span>
        </header>
        <p v-if="ph.note" class="aflow-phase-note">{{ ph.note }}</p>
        <div class="aflow-nodes">
          <template v-for="(it, ni) in ph.items" :key="`${pi}:${ni}`">
            <!-- 主干步骤:点击展开/收起调用明细 -->
            <div v-if="it.t === 'step'" class="aflow-node-wrap">
              <button
                type="button"
                class="aflow-node aflow-node-btn"
                :class="[it.row.state, { 'is-open': isOpen(it.entry) }]"
                :title="isOpen(it.entry) ? '收起调用明细' : '展开调用明细'"
                @click="toggleNode(it.entry)"
              >
                <span class="aflow-dot" />
                <span class="aflow-lbl">{{ it.row.label }}</span>
                <span v-if="it.row.retried" class="aflow-retried" title="同名工具已在后续重试">
                  <UiIcon name="retry" :size="10" />
                  已重试
                </span>
                <span v-if="it.row.dur" class="aflow-dur">{{ it.row.dur }}</span>
                <span class="aflow-caret" :class="{ open: isOpen(it.entry) }">▸</span>
              </button>
              <ChatToolDetail
                v-if="detailVisible(it.entry) && it.entry"
                class="aflow-detail"
                :entry="it.entry"
                @preview="emit('preview', $event)"
              />
            </div>
            <!-- 并行批:同批并发工具横排 chips,逐 chip 展开明细 -->
            <div v-else-if="it.t === 'par'" class="aflow-node aflow-par">
              <div class="aflow-par-row">
                <span class="aflow-dot dot-par" />
                <span class="aflow-par-tag">并发</span>
                <button
                  v-for="(r, qi) in it.rows"
                  :key="qi"
                  type="button"
                  class="aflow-chip"
                  :class="[r.state, { 'is-open': isOpen(it.entries[qi]) }]"
                  :title="isOpen(it.entries[qi]) ? '收起调用明细' : '展开调用明细'"
                  @click="toggleNode(it.entries[qi])"
                >
                  {{ r.label }}
                  <b v-if="r.dur" class="aflow-dur">{{ r.dur }}</b>
                  <UiIcon v-if="r.retried" name="retry" :size="9" />
                </button>
              </div>
              <template v-for="(e, qi) in it.entries" :key="`det-${qi}`">
                <ChatToolDetail
                  v-if="detailVisible(e) && e"
                  class="aflow-detail"
                  :entry="e"
                  @preview="emit('preview', $event)"
                />
              </template>
            </div>
            <!-- 子代理分支节点(点击展开迷你时间线 + 结果明细) -->
            <div v-else class="aflow-node aflow-sub" :class="it.row.state">
              <button type="button" class="aflow-sub-head" @click="toggleNode(it.entry)">
                <span class="aflow-caret" :class="{ open: isOpen(it.entry) }">▸</span>
                <UiIcon name="branch" :size="11" />
                <span class="aflow-lbl">派生子代理</span>
                <span class="aflow-task" :title="it.task">{{ it.task }}</span>
                <span v-if="it.row.state === 'is-run'" class="aflow-run-word">运行中</span>
                <span v-if="it.row.dur" class="aflow-dur">{{ it.row.dur }}</span>
              </button>
              <p v-if="it.conclusion" class="aflow-sub-concl">{{ it.conclusion }}</p>
              <ChatToolDetail
                v-if="detailVisible(it.entry) && it.entry"
                class="aflow-detail aflow-sub-detail"
                :entry="it.entry"
                @preview="emit('preview', $event)"
              />
            </div>
          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.aflow {
  margin: var(--space-xs) 0;
  min-width: 0;
}

.aflow.realtime {
  margin-top: var(--space-sm);
}

/* ---- 冻结态摘要按钮 ---- */
.aflow-summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-family: var(--font-pixel); /* 骨架文案 → 像素 */
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.aflow-summary:hover {
  color: var(--color-accent);
}

.aflow-summary:focus-visible,
.aflow-node-btn:focus-visible,
.aflow-chip:focus-visible,
.aflow-sub-head:focus-visible,
.aflow-toolbar-btn:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 2px;
}

.aflow-caret {
  flex: 0 0 auto;
  transition: transform var(--dur-fast) var(--ease-out);
}

.aflow-caret.open {
  transform: rotate(90deg);
}

/* ---- 节点全部展开/折叠工具行 ---- */
.aflow-toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  color: var(--color-text2);
}

.aflow-toolbar-btn {
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-family: inherit;
  font-size: inherit;
  cursor: pointer;
}

.aflow-toolbar-btn:hover {
  color: var(--color-accent);
}

.aflow-toolbar-sep {
  color: var(--color-text2);
  opacity: 0.6;
}

/* ---- 阶段(纵向分组,引导线在步骤列上) ---- */
.aflow-body {
  margin-top: var(--space-xs);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  min-width: 0;
}

.aflow-phase-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  color: var(--color-accent);
  font-family: var(--font-pixel);
  font-size: var(--text-sm);
}

/* thinking 一句话摘要:说明文字 → sans,muted 单行截断 */
.aflow-phase-note {
  margin: 2px 0 0 calc(13px + var(--space-xs));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  line-height: 1.5;
}

/* 步骤列:虚线引导线贯穿状态点 */
.aflow-nodes {
  position: relative;
  margin: var(--space-xs) 0 0 calc(6px + var(--space-md));
  padding-left: var(--space-md);
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  min-width: 0;
}

.aflow-nodes::before {
  content: '';
  position: absolute;
  left: 3px;
  top: 5px;
  bottom: 5px;
  border-left: 1px dashed var(--color-border);
}

.aflow-node {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
  font-family: var(--font-pixel); /* 步骤标签 = 骨架 → 像素 */
  font-size: var(--text-sm);
  color: var(--color-text2);
}

/* 主干步骤整行可点(button 复位,视觉与原步骤行一致) */
.aflow-node-btn {
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  font: inherit;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.aflow-node-btn:hover {
  color: var(--color-accent);
}

/* 展开态节点:标签走 accent 提示「当前看的是这条的明细」 */
.aflow-node-btn.is-open .aflow-lbl {
  color: var(--color-accent);
}

/* 节点明细:缩进对齐节点文字,与引导线区隔 */
.aflow-node-wrap {
  min-width: 0;
}

.aflow-detail {
  margin: var(--space-xs) 0 0 var(--space-sm);
}

.aflow-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-sage); /* 默认=完成(sage) */
  box-shadow: 0 0 0 2px var(--color-card); /* 与引导线之间留出纸面间隙 */
}

.is-fail .aflow-dot {
  background: var(--color-red);
}

.is-fail .aflow-lbl {
  color: var(--color-red);
}

.is-run .aflow-dot {
  background: var(--color-blue);
  animation: aflow-pulse 1.2s var(--ease-in-out) infinite;
}

@keyframes aflow-pulse {
  0%,
  100% {
    transform: scale(1);
    opacity: 1;
  }
  50% {
    transform: scale(1.55);
    opacity: 0.45;
  }
}

@media (prefers-reduced-motion: reduce) {
  .is-run .aflow-dot {
    animation: none;
  }
}

.aflow-lbl {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 耗时数值:纯文本 mono(如 96s) */
.aflow-dur {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-text2);
  font-weight: normal;
}

/* 失败步重试标记:低调虚线小标 */
.aflow-retried {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: 0 0 auto;
  padding: 1px var(--space-sm);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text2);
  font-size: var(--text-xs);
}

/* ---- 并行批:引导线分叉点(中性灰)+ 横排小 chips ---- */
.aflow-par {
  display: block;
}

.aflow-par-row {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
  flex-wrap: wrap;
}

.dot-par {
  background: var(--color-line-strong);
}

.aflow-par-tag {
  flex: 0 0 auto;
  padding: 1px var(--space-sm);
  border-radius: var(--radius-sm);
  background: var(--color-blue-soft);
  color: var(--color-blue);
  font-family: var(--font-pixel);
  font-size: var(--text-xs);
}

.aflow-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  padding: 2px var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  color: var(--color-text);
  font-family: var(--font-pixel);
  font-size: var(--text-xs);
  cursor: pointer;
  white-space: nowrap;
}

.aflow-chip:hover {
  border-color: var(--color-accent);
}

.aflow-chip.is-open {
  border-color: var(--color-accent);
}

.aflow-chip.is-fail {
  border-color: var(--color-red);
  background: var(--color-red-soft);
  color: var(--color-red);
}

.aflow-chip.is-run {
  border-color: var(--color-blue);
}

/* ---- 子代理分支节点 ---- */
.aflow-sub {
  display: block; /* 头部按钮 + 结论 + 明细纵向堆叠 */
}

.aflow-sub-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-family: var(--font-pixel);
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.aflow-sub-head:hover {
  color: var(--color-accent);
}

.aflow-task {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text2);
  font-family: var(--font-sans); /* 任务描述是内容文本 → sans */
  font-size: var(--text-xs);
}

.aflow-run-word {
  flex: 0 0 auto;
  color: var(--color-blue);
  font-size: var(--text-xs);
}

/* 子结论:说明文字 → sans */
.aflow-sub-concl {
  margin: 2px 0 0 var(--space-lg);
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  line-height: 1.5;
}

/* 子代理明细:比主干多一层缩进,与结论对齐 */
.aflow-sub-detail {
  margin-left: var(--space-lg);
}
</style>
