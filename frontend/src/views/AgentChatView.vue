<script setup lang="ts">
/** Agent 对话检测整卡:顶条(权限模式切换 / 状态 chip / 新建会话)+ 时间线
 * (user 气泡 / assistant 流式气泡(思考折叠 + markdown 正文)/ 工具气泡(参数摘要 +
 * 结果可折叠,失败标红)/ 审批卡片(批准/拒绝/本会话都批准)/ 检测结果卡(11 位编码
 * 等宽高亮 + report_markdown 渲染))+ 失败条(错误 + 重试)+ 输入区(视频路径 + 指令)。
 * 与 ChatView 同模式:TreeView 子路由整卡;状态在 stores/agentchat.ts,组件只接线。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  NButton,
  NInput,
  NRadioButton,
  NRadioGroup,
  NScrollbar,
  useMessage,
} from 'naive-ui'
import { useAgentChatStore } from '../stores/agentchat'
import type { AgentAccess, AgentMode, DetectionPayload } from '../stores/agentchat'
import { mdToHtml } from '../utils/markdown'
import UiIcon from '../components/UiIcon.vue'

const agent = useAgentChatStore()
const message = useMessage()

const question = ref('')
const videoPath = ref('')

/* ---- 状态四态(+运行中):chip 文案与配色 ---- */
const STATUS_LABEL: Record<string, string> = {
  idle: '待开始',
  connecting: '连接中',
  running: '运行中',
  awaiting_approval: '等待审批',
  done: '已完成',
  failed: '失败',
}
const statusLabel = computed(() => STATUS_LABEL[agent.status] ?? agent.status)

/** 进行中(建会话/跑轮次/等审批):禁用发送与模式切换,显示停止。 */
const busy = computed(
  () =>
    agent.status === 'connecting' ||
    agent.status === 'running' ||
    agent.status === 'awaiting_approval',
)
const canSend = computed(() => !!agent.sessionId && !!question.value.trim() && !busy.value)

/* ---- 折叠状态:思考过程 / 工具结果,均按条目下标记,默认收起 ---- */
const thinkOpen = reactive(new Set<number>())
const toolOpen = reactive(new Set<number>())
function toggle(set: Set<number>, i: number) {
  if (set.has(i)) set.delete(i)
  else set.add(i)
}

const lastThinkLine = (think: string) =>
  think
    .split('\n')
    .filter((l) => l.trim())
    .pop() || ''

/* ---- 工具参数摘要:JSON 解析成 k=v 串,超长截断;非 JSON 原文截断 ---- */
function argsSummary(args: string): string {
  const cut = (s: string) => (s.length > 120 ? `${s.slice(0, 120)}…` : s)
  try {
    const obj = JSON.parse(args) as Record<string, unknown>
    const parts = Object.entries(obj).map(
      ([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`,
    )
    return cut(parts.join(', ') || '(无参数)')
  } catch {
    return cut(args)
  }
}

/* ---- 审批卡:accesses 资源访问摘要(中文操作词) ---- */
const OP_LABEL: Record<string, string> = {
  read: '读取',
  write: '写入',
  readwrite: '读写',
  search: '搜索',
}
function accessLabel(a: AgentAccess): string {
  if (a.kind === 'all') return '全部资源'
  const op = OP_LABEL[a.operation ?? ''] ?? a.operation ?? ''
  return `${op} ${a.path ?? ''}${a.recursive ? '(递归)' : ''}`.trim()
}

const DECISION_LABEL: Record<string, string> = {
  approved: '已批准',
  rejected: '已拒绝',
  approved_session: '本会话已批准',
}

async function onApprove(requestId: string, decision: 'approved' | 'rejected', scope?: 'session') {
  try {
    await agent.respondApproval(requestId, decision, scope)
  } catch (e) {
    message.error(`审批回执失败:${(e as Error).message}`)
  }
}

/* ---- 检测结果卡:data 正常是结构化 payload;后端解析失败时为原始字符串 ---- */
function asPayload(data: unknown): DetectionPayload | null {
  return data && typeof data === 'object' ? (data as DetectionPayload) : null
}
const encodingBits = (enc: string) => enc.split('_')

/* ---- 滚动:与 ChatView 同纪律,不跟随流式滚动,仅自己发送时滚底一次 ---- */
const scrollbar = ref<InstanceType<typeof NScrollbar> | null>(null)
function scrollToBottom() {
  scrollbar.value?.scrollTo({ top: Number.MAX_SAFE_INTEGER })
}

async function onSend() {
  const q = question.value.trim()
  if (!q || !canSend.value) return
  const vp = videoPath.value.trim()
  question.value = ''
  await nextTick()
  scrollToBottom()
  await agent.send(q, vp || undefined)
  // 失败由失败条呈现(含重试),不打断式 toast
}

async function onRetry() {
  await agent.retry()
}

async function onModeChange(m: string | number) {
  await agent.setMode(m as AgentMode)
}

async function onNewSession() {
  await agent.newSession()
}

onMounted(async () => {
  await agent.createSession()
})

onUnmounted(() => {
  agent.stop() // 离开页面中断在途流
})
</script>

<template>
  <div class="agent-page">
    <section class="agent-card">
      <!-- 顶条:标题 + 状态 chip + 权限模式 + 新建会话 -->
      <div class="agent-bar">
        <UiIcon name="chip" :size="14" />
        <span class="agent-title">Agent 检测</span>
        <span class="status-chip" :class="`st-${agent.status}`">{{ statusLabel }}</span>
        <span class="agent-spacer" />
        <n-radio-group
          size="small"
          :value="agent.mode"
          :disabled="busy"
          @update:value="onModeChange"
        >
          <n-radio-button value="manual">权限审核</n-radio-button>
          <n-radio-button value="yolo">YOLO</n-radio-button>
        </n-radio-group>
        <n-button size="small" :disabled="busy" @click="onNewSession">新建会话</n-button>
      </div>

      <!-- 时间线 -->
      <n-scrollbar ref="scrollbar" class="agent-scroll">
        <div class="agent-scroll-inner">
          <div v-if="!agent.entries.length" class="agent-empty">
            输入检测指令,如:检测这段视频的交通事件
          </div>
          <template v-for="(e, i) in agent.entries" :key="i">
            <!-- user 气泡(右):视频路径 chip + 指令文本 -->
            <div v-if="e.kind === 'user'" class="row user">
              <div class="bubble">
                <div v-if="e.videoPath" class="video-chip" :title="e.videoPath">
                  <UiIcon name="video" :size="12" />
                  <span class="video-chip-name">{{ e.videoPath }}</span>
                </div>
                <div class="bubble-text">{{ e.text }}</div>
              </div>
            </div>

            <!-- assistant 气泡(左):思考折叠 + markdown 正文 -->
            <div v-else-if="e.kind === 'assistant'" class="row assistant">
              <div class="avatar"><UiIcon name="chip" :size="18" /></div>
              <div class="bubble">
                <div v-if="e.think" class="think">
                  <button class="think-head" @click="toggle(thinkOpen, i)">
                    <UiIcon
                      name="up"
                      :size="10"
                      class="think-caret"
                      :class="{ open: thinkOpen.has(i) }"
                    />
                    <span>思考过程</span>
                  </button>
                  <div v-if="thinkOpen.has(i)" class="think-text">{{ e.think }}</div>
                  <div v-else class="think-line">{{ lastThinkLine(e.think) }}</div>
                </div>
                <div v-if="e.text" class="bubble-text bubble-md" v-html="mdToHtml(e.text)" />
              </div>
            </div>

            <!-- 工具气泡:头(图标 + 名 + 参数摘要 + 状态)整行点击折叠结果;失败标红 -->
            <div v-else-if="e.kind === 'tool'" class="tool" :class="{ err: e.isError }">
              <button class="tool-head" @click="toggle(toolOpen, i)">
                <UiIcon
                  name="up"
                  :size="10"
                  class="think-caret"
                  :class="{ open: toolOpen.has(i) }"
                />
                <UiIcon name="chip" :size="12" />
                <span class="tool-name">{{ e.name }}</span>
                <span class="tool-args">{{ argsSummary(e.args) }}</span>
                <span v-if="!e.done" class="tool-state">执行中…</span>
                <span v-else-if="e.isError" class="tool-state err">失败</span>
                <span v-else class="tool-state ok">完成</span>
              </button>
              <div v-if="toolOpen.has(i) && e.done" class="tool-result">{{ e.result || '(无输出)' }}</div>
            </div>

            <!-- 审批卡片:工具名 + 规则 + 资源访问 + 三键回执 -->
            <div v-else-if="e.kind === 'approval'" class="approval">
              <div class="approval-head">
                <span class="approval-title">审批请求</span>
                <span class="tool-name">{{ e.toolName }}</span>
              </div>
              <div class="approval-rule">{{ e.approvalRule }}</div>
              <div v-if="e.description" class="approval-desc">{{ e.description }}</div>
              <div v-if="e.accesses.length" class="approval-accesses">
                <div v-for="(a, j) in e.accesses" :key="j" class="approval-access">
                  {{ accessLabel(a) }}
                </div>
              </div>
              <div v-if="!e.decision" class="approval-actions">
                <n-button size="small" type="primary" @click="onApprove(e.requestId, 'approved')">
                  批准
                </n-button>
                <n-button size="small" @click="onApprove(e.requestId, 'approved', 'session')">
                  本会话都批准
                </n-button>
                <n-button size="small" type="error" @click="onApprove(e.requestId, 'rejected')">
                  拒绝
                </n-button>
              </div>
              <div v-else class="approval-decided">{{ DECISION_LABEL[e.decision] ?? e.decision }}</div>
            </div>

            <!-- 检测结果卡:11 位编码等宽高亮 + 检出事件 + markdown 报告 -->
            <div v-else class="detection">
              <template v-if="asPayload(e.data)">
                <div class="detection-head">
                  <span class="detection-title">检测结果</span>
                  <span
                    v-if="asPayload(e.data)!.normal === true"
                    class="detection-badge normal"
                  >正常</span>
                  <span
                    v-else-if="asPayload(e.data)!.normal === false"
                    class="detection-badge abnormal"
                  >检出事件</span>
                </div>
                <div v-if="asPayload(e.data)!.binary_encoding" class="detection-encoding">
                  <template
                    v-for="(bit, j) in encodingBits(asPayload(e.data)!.binary_encoding!)"
                    :key="j"
                  >
                    <span class="bit" :class="{ on: bit === '1' }">{{ bit }}</span>
                    <span v-if="j < 10" class="bit-sep">_</span>
                  </template>
                </div>
                <div
                  v-if="asPayload(e.data)!.events?.some((ev) => ev.detected)"
                  class="detection-events"
                >
                  <span
                    v-for="ev in asPayload(e.data)!.events!.filter((ev) => ev.detected)"
                    :key="ev.event_id"
                    class="detection-event"
                    :title="ev.reasoning"
                  >事件 {{ ev.event_id }}</span>
                </div>
                <div
                  v-if="asPayload(e.data)!.report_markdown"
                  class="detection-report bubble-md"
                  v-html="mdToHtml(asPayload(e.data)!.report_markdown!)"
                />
              </template>
              <pre v-else class="detection-raw">{{ String(e.data ?? '') }}</pre>
            </div>
          </template>
        </div>
      </n-scrollbar>

      <!-- 失败条:错误信息 + 重试 -->
      <div v-if="agent.status === 'failed'" class="fail-bar">
        <span class="fail-text" :title="agent.error ?? ''">{{ agent.error || '运行失败' }}</span>
        <n-button size="small" type="primary" @click="onRetry">
          <template #icon><UiIcon name="retry" :size="12" /></template>
          重试
        </n-button>
      </div>

      <!-- 输入区:视频路径(可选)+ 指令;Enter 发送 / Shift+Enter 换行 / 进行中可停止 -->
      <div class="agent-composer">
        <n-input
          v-model:value="videoPath"
          size="small"
          placeholder="视频路径:工作区内相对/绝对路径,如 演示区/xxx.mp4(可选)"
          :disabled="busy"
        />
        <div class="agent-input-row">
          <n-input
            v-model:value="question"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 4 }"
            placeholder="输入检测指令,Enter 发送,Shift+Enter 换行"
            :disabled="busy"
            @keydown.enter.exact.prevent="onSend"
          />
          <n-button v-if="busy" size="small" @click="agent.stop()">停止</n-button>
          <n-button v-else type="primary" size="small" :disabled="!canSend" @click="onSend">
            <template #icon><UiIcon name="send" :size="12" /></template>
            发送
          </n-button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 与 ChatView 同:对话需要内部滚动,卡片占满主区可视高度 */
.agent-page {
  height: 100%;
  display: flex;
  min-height: 0;
}

.agent-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-card);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

/* ---- 顶条 ---- */
.agent-bar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text2);
}

.agent-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
}

.agent-spacer {
  flex: 1;
}

/* ---- 状态 chip:四态(+运行中)配色全部走 token ---- */
.status-chip {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 600;
  background: var(--color-surface-3);
  color: var(--color-text2);
}

.status-chip.st-connecting,
.status-chip.st-running {
  background: var(--color-blue-soft);
  color: var(--color-blue);
}

.status-chip.st-awaiting_approval {
  background: color-mix(in srgb, var(--color-gold) 20%, var(--color-card));
  color: var(--color-gold);
}

.status-chip.st-done {
  background: var(--color-sage-soft);
  color: var(--color-sage);
}

.status-chip.st-failed {
  background: var(--color-red-soft);
  color: var(--color-red);
}

/* ---- 时间线 ---- */
.agent-scroll {
  flex: 1;
  min-height: 0;
}

.agent-scroll-inner {
  padding: var(--space-md) var(--space-lg);
}

.agent-empty {
  padding: var(--space-2xl) var(--space-md);
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-md);
}

.row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  margin: var(--space-sm) 0;
}

.row.user {
  justify-content: flex-end;
}

.avatar {
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.bubble {
  max-width: 65%;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.row.user .bubble {
  background: var(--color-accent-soft);
  border-color: var(--color-accent-deep);
}

.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-md);
  line-height: 1.6;
}

.video-chip {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 320px;
  margin-bottom: var(--space-xs);
  color: var(--color-text2);
  font-size: var(--text-xs);
}

.video-chip-name {
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 思考过程折叠(与 ChatView 同交互) ---- */
.think {
  margin-bottom: var(--space-xs);
}

.think-head {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  cursor: pointer;
}

.think-head:hover {
  color: var(--color-accent);
}

.think-caret {
  transform: rotate(180deg); /* 收起:向下 */
  transition: transform 0.15s ease;
}

.think-caret.open {
  transform: rotate(0deg); /* 展开:向上 */
}

.think-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.think-line {
  width: min(320px, 60vw);
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- 工具气泡 ---- */
.tool {
  margin: var(--space-sm) 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
}

.tool.err {
  border-color: var(--color-red);
  background: var(--color-red-soft);
}

.tool-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: var(--space-xs) var(--space-sm);
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.tool-head:hover {
  color: var(--color-accent);
}

.tool-name {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--color-text);
}

.tool-args {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-state {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--color-blue);
}

.tool-state.ok {
  color: var(--color-sage);
}

.tool-state.err {
  color: var(--color-red);
}

.tool-result {
  padding: 0 var(--space-sm) var(--space-sm) calc(var(--space-sm) + 22px);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--color-text2);
  max-height: 240px;
  overflow-y: auto;
}

/* ---- 审批卡片 ---- */
.approval {
  margin: var(--space-sm) 0;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-gold);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, var(--color-card));
}

.approval-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.approval-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-gold);
}

.approval-rule {
  margin-top: var(--space-xs);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--color-text);
  word-break: break-all;
}

.approval-desc {
  margin-top: var(--space-xs);
  font-size: var(--text-sm);
  color: var(--color-text2);
}

.approval-accesses {
  margin-top: var(--space-xs);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.approval-access {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-text2);
  word-break: break-all;
}

.approval-actions {
  margin-top: var(--space-sm);
  display: flex;
  gap: var(--space-sm);
}

.approval-decided {
  margin-top: var(--space-sm);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-text2);
}

/* ---- 检测结果卡 ---- */
.detection {
  margin: var(--space-sm) 0;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--color-accent);
  border-radius: var(--radius-sm);
  background: var(--color-card);
  box-shadow: var(--shadow);
}

.detection-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.detection-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-accent);
}

.detection-badge {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 600;
}

.detection-badge.normal {
  background: var(--color-sage-soft);
  color: var(--color-sage);
}

.detection-badge.abnormal {
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

/* 11 位编码:等宽字体,置位高亮 accent */
.detection-encoding {
  margin-top: var(--space-sm);
  font-family: var(--font-mono);
  font-size: var(--text-xl);
  letter-spacing: 0.06em;
  color: var(--color-text2);
  overflow-wrap: anywhere;
}

.detection-encoding .bit.on {
  color: var(--color-accent);
  font-weight: 700;
  background: var(--color-accent-soft);
  border-radius: 4px;
  padding: 0 2px;
}

.detection-encoding .bit-sep {
  color: var(--color-dot-muted);
}

.detection-events {
  margin-top: var(--space-sm);
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
}

.detection-event {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-accent);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-xs);
  font-weight: 600;
}

.detection-report {
  margin-top: var(--space-sm);
  font-size: var(--text-md);
  line-height: 1.6;
}

.detection-raw {
  margin: var(--space-xs) 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--color-text2);
}

/* ---- markdown 正文(mdToHtml 输出的 .md 容器,与 ChatView 同一套) ---- */
.bubble-md {
  white-space: normal;
}

.bubble-md :deep(.md) > :first-child {
  margin-top: 0;
}

.bubble-md :deep(.md) > :last-child {
  margin-bottom: 0;
}

.bubble-md :deep(.md p),
.bubble-md :deep(.md ul),
.bubble-md :deep(.md ol),
.bubble-md :deep(.md blockquote),
.bubble-md :deep(.md pre),
.bubble-md :deep(.md table) {
  margin: var(--space-xs) 0;
}

.bubble-md :deep(.md h1),
.bubble-md :deep(.md h2),
.bubble-md :deep(.md h3),
.bubble-md :deep(.md h4) {
  margin: var(--space-sm) 0 var(--space-xs);
  font-size: var(--text-md);
}

.bubble-md :deep(.md ul),
.bubble-md :deep(.md ol) {
  padding-left: var(--space-lg);
}

.bubble-md :deep(.md code) {
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  font-size: var(--text-sm);
}

.bubble-md :deep(.md pre) {
  padding: var(--space-sm);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  overflow-x: auto;
}

.bubble-md :deep(.md pre code) {
  padding: 0;
  border: none;
  background: none;
}

.bubble-md :deep(.md a) {
  color: var(--color-accent);
}

.bubble-md :deep(.md blockquote) {
  padding-left: var(--space-sm);
  border-left: 2px solid var(--color-border);
  color: var(--color-text2);
}

.bubble-md :deep(.md table) {
  border-collapse: collapse;
}

.bubble-md :deep(.md th),
.bubble-md :deep(.md td) {
  padding: 2px var(--space-sm);
  border: 1px solid var(--color-border);
}

/* ---- 失败条 ---- */
.fail-bar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-xs) var(--space-lg);
  border-top: 1px solid var(--color-red);
  background: var(--color-red-soft);
}

.fail-text {
  flex: 1;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--color-red);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 输入区 ---- */
.agent-composer {
  border-top: 1px solid var(--color-border);
  padding: var(--space-sm) var(--space-lg);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.agent-input-row {
  display: flex;
  align-items: flex-end;
  gap: var(--space-sm);
}
</style>
