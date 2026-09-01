<script setup lang="ts">
/** 分析链路流程图(W6):纵向时间线 + 阶段分组,数据由 utils/analysisFlow 推导,
 * 本组件只做展示:
 * - 冻结态(默认):检测卡内一行摘要「N 轮循环 · M 次调用 · K 子代理 · X 秒」
 *   (无数据段静默省略),点击展开完整阶段树;
 * - 实时态(realtime):对话区底部随 entries 生长的完整树,当前步脉冲高亮、
 *   失败步红色 + 「已重试」标记,无摘要行。
 * 链路节点即工具条目:每个节点(步骤/并发 chip/子代理分支)默认折叠
 * (label + 耗时 + 状态),点击展开调用明细(ChatToolDetail:tool_call 参数行 +
 * 结果文本/图片/子代理时间线/取证视频),展开态由宿主按条目 id 记忆(expandedTools,
 * 会话内共享,实时态与冻结态同一份);assistant 思考与正文按时间序交错插为
 * 思考/说明节点(思考折叠=一句话摘要 ThinkLine,说明折叠=前两行预览
 * textPreviewLines;展开=全文,说明走 markdown 渲染 MdStream,均带复制),
 * 展开态同理按条目 id 记忆于 expandedThinks / expandedTexts;面板头部
 * 「全部展开/全部折叠」同时作用于工具、思考与说明节点。
 * 阶段标题/步骤标签/按钮为骨架 → 像素字体;思考摘要/全文与子结论为说明文字
 * → sans;耗时数值 → mono。颜色全部 tokens 变量(design.md §8 禁 inline 色);
 * 图标一律 UiIcon(§8 禁 emoji)。 */
import { computed, reactive, ref } from 'vue'
import UiIcon from '../UiIcon.vue'
import type { AnalysisFlow, FlowNode, FlowStep, FlowSubagent } from '../../utils/analysisFlow'
import type { AgentToolEntry } from '../../stores/agentchat'
import { copyText, textPreviewLines } from '../../utils/chatDisplay'
import ChatToolDetail from './ChatToolDetail.vue'
import MdStream from './MdStream.vue'
import ThinkLine from './ThinkLine.vue'

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

/* ---- 展示模型:把推导层的三种节点规约成模板友好的扁平结构 ---- */
interface Row {
  label: string
  dur: string
  state: string
  retried: boolean
}
type Item =
  | { t: 'think'; id: string; text: string; live: boolean; dur: string }
  | { t: 'text'; id: string; text: string; live: boolean }
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

/* ---- 思考节点折叠/展开:按来源 assistant 条目 id(宿主的 expandedThinks 记忆) ---- */
function isThinkOpen(id: string): boolean {
  return props.expandedThinks?.has(id) ?? false
}

function toggleThink(id: string) {
  emit('toggle-think', id)
}

/* ---- 说明节点折叠/展开:按来源 assistant 条目 id(宿主的 expandedTexts 记忆),
 * 与思考节点同机制但各自独立(同一条目的思考与说明互不联动)。 ---- */
function isTextOpen(id: string): boolean {
  return props.expandedTexts?.has(id) ?? false
}

function toggleText(id: string) {
  emit('toggle-text', id)
}

/** 节点全文复制(思考/说明共用):面板内自管(与 ChatToolDetail 取证目录复制
 * 同款低调交互),成功图标变 ✓ 一秒,失败静默。copiedKey 以 kind:id 区分同一条目
 * 的思考与说明两处复制按钮。 */
const copiedKey = ref<string | null>(null)
let copiedTimer: ReturnType<typeof setTimeout> | null = null
async function copyNode(kind: 'think' | 'text', id: string, text: string) {
  try {
    await copyText(text)
  } catch {
    return
  }
  copiedKey.value = `${kind}:${id}`
  if (copiedTimer !== null) clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => {
    copiedKey.value = null
    copiedTimer = null
  }, 1000)
}

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
            <!-- 思考节点:折叠=一句话摘要(ThinkLine,实时态末行跟随),展开=全文+复制 -->
            <div v-if="it.t === 'think'" class="aflow-node-wrap aflow-think">
              <div class="aflow-think-row">
                <button
                  type="button"
                  class="aflow-node-btn aflow-think-head"
                  :title="isThinkOpen(it.id) ? '收起思考全文' : '展开思考全文'"
                  @click="toggleThink(it.id)"
                >
                  <span class="aflow-caret" :class="{ open: isThinkOpen(it.id) }">▸</span>
                  <span class="aflow-think-lbl">思考过程:</span>
                  <span v-if="it.dur" class="aflow-dur">生成 {{ it.dur }}</span>
                  <ThinkLine v-if="!isThinkOpen(it.id)" :think="it.text" :live="it.live" />
                </button>
                <button
                  type="button"
                  class="aflow-think-copy"
                  title="复制思考内容"
                  @click="copyNode('think', it.id, it.text)"
                >
                  <UiIcon :name="copiedKey === `think:${it.id}` ? 'check' : 'copy'" :size="11" />
                </button>
              </div>
              <div v-if="isThinkOpen(it.id)" class="aflow-think-text">{{ it.text }}</div>
            </div>
            <!-- 说明节点:折叠=前两行预览(textPreviewLines),展开=markdown 全文(MdStream)+复制 -->
            <div v-else-if="it.t === 'text'" class="aflow-node-wrap aflow-text">
              <div class="aflow-text-row">
                <button
                  type="button"
                  class="aflow-node-btn aflow-text-head"
                  :title="isTextOpen(it.id) ? '收起说明全文' : '展开说明全文'"
                  @click="toggleText(it.id)"
                >
                  <span class="aflow-caret" :class="{ open: isTextOpen(it.id) }">▸</span>
                  <span class="aflow-text-lbl">说明:</span>
                  <span v-if="!isTextOpen(it.id)" class="aflow-text-preview">{{
                    textPreviewLines(it.text)
                  }}</span>
                </button>
                <button
                  type="button"
                  class="aflow-text-copy"
                  title="复制说明内容"
                  @click="copyNode('text', it.id, it.text)"
                >
                  <UiIcon :name="copiedKey === `text:${it.id}` ? 'check' : 'copy'" :size="11" />
                </button>
              </div>
              <div v-if="isTextOpen(it.id)" class="aflow-text-text">
                <MdStream :text="it.text" :streaming="it.live" />
              </div>
            </div>
            <!-- 主干步骤:点击展开/收起调用明细 -->
            <div v-else-if="it.t === 'step'" class="aflow-node-wrap">
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

/* ---- 思考/说明节点:折叠头(▸ 标签 + 摘要/预览)+ 悬停显现复制 ---- */
.aflow-think-row,
.aflow-text-row {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
}

/* 折叠头内摘要占满剩余宽度截断;摘要/全文是内容文本 → sans(按钮继承像素只给标签) */
.aflow-think-head,
.aflow-text-head {
  width: auto; /* 覆盖 .aflow-node-btn 的 100%:同行还有悬停显现的复制按钮 */
  min-width: 0;
  flex: 1 1 auto;
}

.aflow-think-lbl,
.aflow-text-lbl {
  flex: 0 0 auto;
}

.aflow-think-head :deep(.think-line) {
  flex: 1 1 auto;
  width: auto;
  min-width: 0;
  font-family: var(--font-sans);
  font-size: var(--text-xs);
}

/* 说明折叠预览:前两行(超两行截断 + 省略号提示),内容文本 → sans */
.aflow-text-preview {
  flex: 1 1 auto;
  min-width: 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-line;
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  line-height: 1.6;
  text-align: left;
}

.aflow-think-copy,
.aflow-text-copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--color-text2);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-out);
}

.aflow-think-row:hover .aflow-think-copy,
.aflow-think-row:focus-within .aflow-think-copy,
.aflow-text-row:hover .aflow-text-copy,
.aflow-text-row:focus-within .aflow-text-copy {
  opacity: 1;
}

.aflow-think-copy:hover,
.aflow-text-copy:hover {
  color: var(--color-accent);
}

/* 思考/说明全文:说明文字 → sans,与节点文字缩进对齐 */
.aflow-think-text {
  margin: var(--space-xs) 0 0 var(--space-sm);
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  line-height: 1.6;
}

/* 说明全文走 markdown(MdStream → .md 容器),样式与气泡正文同款 */
.aflow-text-text {
  margin: var(--space-xs) 0 0 var(--space-sm);
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  line-height: 1.6;
  min-width: 0;
}

.aflow-text-text :deep(.md) > :first-child {
  margin-top: 0;
}

.aflow-text-text :deep(.md) > :last-child {
  margin-bottom: 0;
}

.aflow-text-text :deep(.md p),
.aflow-text-text :deep(.md ul),
.aflow-text-text :deep(.md ol),
.aflow-text-text :deep(.md blockquote),
.aflow-text-text :deep(.md pre),
.aflow-text-text :deep(.md table) {
  margin: var(--space-xs) 0;
}

.aflow-text-text :deep(.md h1),
.aflow-text-text :deep(.md h2),
.aflow-text-text :deep(.md h3),
.aflow-text-text :deep(.md h4) {
  margin: var(--space-sm) 0 var(--space-xs);
  font-size: var(--text-sm);
}

.aflow-text-text :deep(.md ul),
.aflow-text-text :deep(.md ol) {
  padding-left: var(--space-lg);
}

.aflow-text-text :deep(.md code) {
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  font-size: var(--text-sm);
}

.aflow-text-text :deep(.md pre) {
  padding: var(--space-sm);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  overflow-x: auto;
}

.aflow-text-text :deep(.md pre code) {
  padding: 0;
  border: none;
  background: none;
}

.aflow-text-text :deep(.md a) {
  color: var(--color-accent);
}

.aflow-text-text :deep(.md blockquote) {
  padding-left: var(--space-sm);
  border-left: 2px solid var(--color-border);
  color: var(--color-text2);
}

.aflow-text-text :deep(.md table) {
  border-collapse: collapse;
}

.aflow-text-text :deep(.md th),
.aflow-text-text :deep(.md td) {
  padding: 2px var(--space-sm);
  border: 1px solid var(--color-border);
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
