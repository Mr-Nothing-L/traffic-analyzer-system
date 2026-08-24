<script setup lang="ts">
/** 统一对话整卡(问答 + 检测,后端统一走 /api/agent/*):
 * 左侧历史会话栏(列表 title + 相对时间 / 点击切换重建时间线 / 删除(optimistic + 确认)/ 新建)
 * + 右侧对话卡:顶条(状态 chip + 权限模式)/ 时间线(user 气泡(图片附件 + 视频路径 chip)/
 * assistant 流式气泡(思考折叠 + markdown)/ 工具气泡(参数摘要 + 结果折叠,失败标红)/
 * 审批卡(批准/本会话都批准/拒绝;历史未决显示「已失效」)/ 检测结果卡(11 位编码等宽高亮 +
 * markdown 报告))/ 失败条(错误 + 重试)/ 输入区(图片附件:粘贴/选择/拖拽 ≤4 张,缩略图可移除;
 * 视频路径(可选)+ 指令;Enter 发送 / Shift+Enter 换行 / 进行中可停止)。
 * 图片画廊:点击气泡图放大,左右切换(按钮/键盘 ←→)。
 * 状态在 stores/agentchat.ts,组件只接线。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  NButton,
  NInput,
  NModal,
  NPopconfirm,
  NRadioButton,
  NRadioGroup,
  NScrollbar,
  useMessage,
} from 'naive-ui'
import { useAgentChatStore } from '../stores/agentchat'
import type { AgentAccess, AgentMode, AgentSessionInfo, DetectionPayload } from '../stores/agentchat'
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

/* ---- 历史会话栏:按最近活跃倒序;相对时间(后端为 epoch ms) ---- */
const sortedSessions = computed(() =>
  [...agent.sessions].sort((a, b) => (b.lastActiveAt ?? 0) - (a.lastActiveAt ?? 0)),
)

function relTime(ts?: number): string {
  if (!ts) return ''
  const m = Math.floor((Date.now() - ts) / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d} 天前`
  const dt = new Date(ts)
  return `${dt.getMonth() + 1}月${dt.getDate()}日`
}

async function onSelect(id: string) {
  resetFolds()
  await agent.selectSession(id)
  await nextTick()
  scrollToBottom()
}

async function onDelete(id: string) {
  try {
    await agent.deleteSession(id)
  } catch (e) {
    message.error(`删除会话失败:${(e as Error).message}`)
  }
}

/* ---- 折叠状态:思考过程 / 工具结果,均按条目下标记,默认收起;切换/新建会话时重置 ---- */
const thinkOpen = reactive(new Set<number>())
const toolOpen = reactive(new Set<number>())
function toggle(set: Set<number>, i: number) {
  if (set.has(i)) set.delete(i)
  else set.add(i)
}
function resetFolds() {
  thinkOpen.clear()
  toolOpen.clear()
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
  cancelled: '已取消',
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

/* ---- 图片附件(≤4 张):「+」选择 / Ctrl+V 粘贴 / 拖拽均走 stageImages;
 * 暂存给缩略图 objectURL,发送时 FileReader 转 dataURL 随消息上传 ---- */
const MAX_IMAGES = 4
interface PendingImg {
  file: File
  url: string
}
const pending = ref<PendingImg[]>([])
const fileInput = ref<HTMLInputElement | null>(null)

function stageImages(files: Iterable<File>): boolean {
  const list = Array.from(files)
  if (!list.length) return false
  const imgs = list.filter((f) => f.type.startsWith('image/'))
  if (!imgs.length) {
    message.warning('仅支持图片附件,视频请在上方填路径')
    return false
  }
  const room = MAX_IMAGES - pending.value.length
  if (room <= 0) {
    message.warning(`图片最多 ${MAX_IMAGES} 张`)
    return false
  }
  if (imgs.length > room) message.warning(`图片最多 ${MAX_IMAGES} 张,超出部分已忽略`)
  for (const f of imgs.slice(0, room)) {
    pending.value.push({ file: f, url: URL.createObjectURL(f) })
  }
  return true
}

function onFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = '' // 允许重复选择同一文件再次触发 change
  stageImages(files)
}

/* Ctrl+V 粘贴:有文件(如截图)才拦截,纯文本粘贴不受影响 */
function onPaste(ev: ClipboardEvent) {
  const files = ev.clipboardData?.files
  if (!files?.length) return
  ev.preventDefault()
  if (stageImages(files)) message.success('已添加附件')
}

/* 拖拽:计数器防子元素进出导致的 dragenter/dragleave 抖动 */
const dragOver = ref(false)
let dragDepth = 0

const hasDragFiles = (ev: DragEvent) =>
  Array.from(ev.dataTransfer?.types || []).includes('Files')

function onDragEnter(ev: DragEvent) {
  if (!hasDragFiles(ev)) return
  ev.preventDefault()
  dragDepth += 1
  dragOver.value = true
}

function onDragOver(ev: DragEvent) {
  if (!hasDragFiles(ev)) return
  ev.preventDefault() // 必须,否则浏览器不会触发 drop
}

function onDragLeave(ev: DragEvent) {
  if (!hasDragFiles(ev)) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragOver.value = false
}

function onDrop(ev: DragEvent) {
  ev.preventDefault()
  dragDepth = 0
  dragOver.value = false
  const files = ev.dataTransfer?.files
  if (files?.length && stageImages(files)) message.success('已添加附件')
}

/** 卡片外区域拖放文件时阻止浏览器默认的导航打开行为。 */
function preventWindowDrop(ev: Event) {
  ev.preventDefault()
}

function removeAttachment(i: number) {
  const [a] = pending.value.splice(i, 1)
  if (a) URL.revokeObjectURL(a.url)
}

function clearPending() {
  for (const a of pending.value) URL.revokeObjectURL(a.url)
  pending.value = []
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(new Error(`读取 ${file.name} 失败`))
    r.readAsDataURL(file)
  })
}

/* ---- 图片画廊:全对话 user 气泡图按时间序进画廊;点击放大,左右切换(按钮/键盘) ---- */
const previewIndex = ref<number | null>(null)

const galleryImages = computed(() => {
  const urls: string[] = []
  for (const e of agent.entries) {
    if (e.kind === 'user' && e.images?.length) urls.push(...e.images)
  }
  return urls
})
const previewUrl = computed(() =>
  previewIndex.value != null ? (galleryImages.value[previewIndex.value] ?? null) : null,
)

/* 画廊打开期间列表收缩(切换会话):收敛或关闭,防越界空 modal */
watch(galleryImages, (imgs) => {
  if (previewIndex.value == null) return
  if (!imgs.length) previewIndex.value = null
  else if (previewIndex.value >= imgs.length) previewIndex.value = imgs.length - 1
})

function openPreview(u: string) {
  const i = galleryImages.value.indexOf(u)
  if (i >= 0) previewIndex.value = i
}

function previewNav(delta: number) {
  if (previewIndex.value == null) return
  const next = previewIndex.value + delta
  if (next < 0 || next >= galleryImages.value.length) return // 边界不循环
  previewIndex.value = next
}

/** 仅画廊打开时响应 ←/→(挂在 window,组件卸载移除;关闭时不拦截任何按键)。 */
function onGalleryKey(ev: KeyboardEvent) {
  if (previewIndex.value == null) return
  if (ev.key === 'ArrowLeft') previewNav(-1)
  else if (ev.key === 'ArrowRight') previewNav(1)
}

/* ---- 滚动:不跟随流式滚动,仅进入页面/切换会话/自己发送时主动滚底一次 ---- */
const scrollbar = ref<InstanceType<typeof NScrollbar> | null>(null)
function scrollToBottom() {
  scrollbar.value?.scrollTo({ top: Number.MAX_SAFE_INTEGER })
}

async function onSend() {
  const q = question.value.trim()
  if (!q || !canSend.value) return
  const vp = videoPath.value.trim()
  let images: string[] | undefined
  if (pending.value.length) {
    try {
      images = await Promise.all(pending.value.map((p) => fileToDataUrl(p.file)))
    } catch (e) {
      message.error(`图片读取失败:${(e as Error).message}`)
      return
    }
    clearPending()
  }
  question.value = ''
  await nextTick()
  scrollToBottom()
  await agent.send(q, vp || undefined, images)
  // 失败由失败条呈现(含重试),不打断式 toast
}

async function onRetry() {
  await agent.retry()
}

async function onModeChange(m: string | number) {
  resetFolds()
  await agent.setMode(m as AgentMode)
}

async function onNewSession() {
  resetFolds()
  await agent.newSession()
}

function sessionTitle(s: AgentSessionInfo): string {
  return s.title?.trim() || '新会话'
}

onMounted(async () => {
  window.addEventListener('dragover', preventWindowDrop)
  window.addEventListener('drop', preventWindowDrop)
  window.addEventListener('keydown', onGalleryKey)
  try {
    await agent.fetchSessions()
    const latest = sortedSessions.value[0]
    if (latest) await agent.selectSession(latest.id) // 有历史:续上最近会话
    else await agent.createSession()
    await nextTick()
    scrollToBottom() // 进入页面初始滚到底(仅这一次主动滚动)
  } catch (e) {
    message.error(`加载会话列表失败:${(e as Error).message}`)
  }
})

onUnmounted(() => {
  window.removeEventListener('dragover', preventWindowDrop)
  window.removeEventListener('drop', preventWindowDrop)
  window.removeEventListener('keydown', onGalleryKey)
  agent.stop() // 离开页面中断在途流
  clearPending() // 释放全部 objectURL
})
</script>

<template>
  <div class="chat-page">
    <!-- 历史会话栏:title + 相对时间,点击切换,悬停出删除(确认后 optimistic 移除) -->
    <aside class="session-col">
      <div class="session-head">
        <UiIcon name="chat" :size="14" />
        <span class="session-head-title">历史会话</span>
        <span class="session-spacer" />
        <n-button size="small" :disabled="busy" @click="onNewSession">新建</n-button>
      </div>
      <n-scrollbar class="session-scroll">
        <div v-if="!sortedSessions.length" class="session-empty">暂无历史会话</div>
        <div
          v-for="s in sortedSessions"
          :key="s.id"
          class="session-item"
          :class="{ active: s.id === agent.sessionId }"
          tabindex="0"
          @click="onSelect(s.id)"
          @keydown.enter="onSelect(s.id)"
        >
          <div class="session-item-title" :title="sessionTitle(s)">{{ sessionTitle(s) }}</div>
          <div class="session-item-meta">
            <span class="session-item-time">{{ relTime(s.lastActiveAt) }}</span>
            <n-popconfirm @positive-click="onDelete(s.id)">
              <template #trigger>
                <button class="session-del" title="删除会话" @click.stop>
                  <UiIcon name="close" :size="10" />
                </button>
              </template>
              删除该会话及其全部记录?
            </n-popconfirm>
          </div>
        </div>
      </n-scrollbar>
    </aside>

    <section
      class="chat-card"
      @dragenter="onDragEnter"
      @dragover="onDragOver"
      @dragleave="onDragLeave"
      @drop="onDrop"
    >
      <!-- 拖拽提示覆盖层(pointer-events: none,不干扰 dragleave 计数) -->
      <div v-show="dragOver" class="drop-overlay">松开鼠标,将图片添加为附件</div>

      <!-- 顶条:标题 + 状态 chip + 权限模式 -->
      <div class="chat-bar">
        <UiIcon name="chat" :size="14" />
        <span class="chat-title">对话</span>
        <span class="status-chip" :class="`st-${agent.status}`">{{ statusLabel }}</span>
        <span class="chat-spacer" />
        <n-radio-group
          size="small"
          :value="agent.mode"
          :disabled="busy"
          @update:value="onModeChange"
        >
          <n-radio-button value="manual">权限审核</n-radio-button>
          <n-radio-button value="yolo">YOLO</n-radio-button>
        </n-radio-group>
      </div>

      <!-- 时间线 -->
      <n-scrollbar ref="scrollbar" class="chat-scroll">
        <div class="chat-scroll-inner">
          <div v-if="!agent.entries.length" class="chat-empty">
            输入问题或检测指令,如:检测这段视频的交通事件
          </div>
          <template v-for="(e, i) in agent.entries" :key="i">
            <!-- user 气泡(右):图片附件 + 视频路径 chip + 指令文本 -->
            <div v-if="e.kind === 'user'" class="row user">
              <div class="bubble">
                <div v-if="e.images?.length" class="img-group">
                  <img
                    v-for="u in e.images"
                    :key="u"
                    :src="u"
                    alt=""
                    loading="lazy"
                    @click="openPreview(u)"
                  />
                </div>
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

            <!-- 审批卡片:工具名 + 规则 + 资源访问 + 三键回执;历史未决显示「已失效」 -->
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
              <div v-if="!e.decision && !e.stale" class="approval-actions">
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
              <div v-else-if="e.decision" class="approval-decided">
                {{ DECISION_LABEL[e.decision] ?? e.decision }}
              </div>
              <div v-else class="approval-decided">已失效</div>
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

      <!-- 输入区:视频路径(可选)+ 暂存附件区 + 「+」/输入框/停止或发送;
           粘贴事件冒泡到此处统一处理 -->
      <div class="chat-composer" @paste="onPaste">
        <n-input
          v-model:value="videoPath"
          size="small"
          placeholder="视频路径:工作区内相对/绝对路径,如 演示区/xxx.mp4(可选)"
          :disabled="busy"
        />
        <div v-if="pending.length" class="attach-list">
          <div v-for="(a, i) in pending" :key="a.url" class="attach-item">
            <img class="attach-thumb" :src="a.url" :alt="a.file.name" />
            <button class="attach-remove" title="移除" @click="removeAttachment(i)">
              <UiIcon name="close" :size="10" />
            </button>
          </div>
        </div>
        <div class="chat-input-row">
          <n-button
            size="small"
            quaternary
            title="添加图片(最多 4 张)"
            :disabled="busy"
            @click="fileInput?.click()"
          >
            <template #icon><UiIcon name="plus" :size="14" /></template>
          </n-button>
          <n-input
            v-model:value="question"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 4 }"
            placeholder="输入问题或检测指令,Enter 发送,Shift+Enter 换行"
            :disabled="busy"
            @keydown.enter.exact.prevent="onSend"
          />
          <n-button v-if="busy" size="small" @click="agent.stop()">停止</n-button>
          <n-button v-else type="primary" size="small" :disabled="!canSend" @click="onSend">
            <template #icon><UiIcon name="send" :size="12" /></template>
            发送
          </n-button>
        </div>
        <input
          ref="fileInput"
          type="file"
          hidden
          multiple
          accept="image/*"
          @change="onFiles"
        />
      </div>
    </section>

    <!-- 图片画廊:单图放大 + 左右切换(按钮/键盘 ←→,边界禁用) -->
    <n-modal :show="previewIndex != null" @update:show="previewIndex = null">
      <div v-if="previewUrl" class="gallery-wrap">
        <img class="preview-img" :src="previewUrl" alt="" @click="previewIndex = null" />
        <button
          type="button"
          class="gallery-nav gallery-prev"
          title="上一张 (←)"
          :disabled="previewIndex === 0"
          @click.stop="previewNav(-1)"
        >
          <UiIcon name="left" :size="18" />
        </button>
        <button
          type="button"
          class="gallery-nav gallery-next"
          title="下一张 (→)"
          :disabled="previewIndex === galleryImages.length - 1"
          @click.stop="previewNav(1)"
        >
          <UiIcon name="right" :size="18" />
        </button>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
/* 对话需要内部滚动,页面占满主区可视高度 */
.chat-page {
  height: 100%;
  display: flex;
  gap: var(--space-md);
  min-height: 0;
}

/* ---- 历史会话栏 ---- */
.session-col {
  width: 220px;
  flex: 0 0 220px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--color-card);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

.session-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text2);
}

.session-head-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
}

.session-spacer {
  flex: 1;
}

.session-scroll {
  flex: 1;
  min-height: 0;
}

.session-empty {
  padding: var(--space-xl) var(--space-md);
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.session-item {
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--color-border);
  cursor: pointer;
}

.session-item:hover {
  background: var(--color-hover-bg);
}

.session-item:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: -2px;
}

.session-item.active {
  background: var(--color-accent-soft);
}

.session-item-title {
  font-size: var(--text-md);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item-meta {
  margin-top: 2px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-xs);
}

.session-item-time {
  font-size: var(--text-xs);
  color: var(--color-text2);
}

/* 删除按钮:默认淡隐,悬停/聚焦条目或自身时出现 */
.session-del {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: none;
  color: var(--color-text2);
  cursor: pointer;
  opacity: 0;
}

.session-item:hover .session-del,
.session-item:focus-within .session-del {
  opacity: 1;
}

.session-del:hover {
  color: var(--color-red);
  background: var(--color-red-soft);
}

.session-del:focus-visible {
  opacity: 1;
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

/* ---- 对话卡 ---- */
.chat-card {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative; /* 拖拽覆盖层定位基准 */
  background: var(--color-card);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

/* ---- 拖拽提示覆盖层 ---- */
.drop-overlay {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px dashed var(--color-accent);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--color-card) 82%, transparent);
  color: var(--color-accent);
  font-size: var(--text-md);
  font-weight: 600;
  pointer-events: none;
}

/* ---- 顶条 ---- */
.chat-bar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text2);
}

.chat-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
}

.chat-spacer {
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
.chat-scroll {
  flex: 1;
  min-height: 0;
}

.chat-scroll-inner {
  padding: var(--space-md) var(--space-lg);
}

.chat-empty {
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

/* ---- user 气泡内的图片附件(文字上方,点击进画廊) ---- */
.img-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-bottom: var(--space-sm);
}

.img-group img {
  width: 160px;
  height: 107px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* ---- 思考过程折叠 ---- */
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

/* ---- markdown 正文(mdToHtml 输出的 .md 容器) ---- */
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
.chat-composer {
  border-top: 1px solid var(--color-border);
  padding: var(--space-sm) var(--space-lg);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.chat-input-row {
  display: flex;
  align-items: flex-end;
  gap: var(--space-sm);
}

/* ---- 暂存附件区 ---- */
.attach-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
}

.attach-item {
  position: relative;
  display: flex;
  align-items: center;
}

.attach-thumb {
  width: 64px;
  height: 48px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
}

.attach-remove {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 16px;
  height: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  background: var(--color-card);
  color: var(--color-text2);
  cursor: pointer;
  padding: 0;
}

.attach-remove:hover {
  color: var(--color-accent);
}

/* ---- 图片放大(画廊);深色遮罩用 stage-bg token 的 color-mix,不写 inline hex ---- */
.gallery-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.preview-img {
  max-width: calc(100vw - 96px);
  max-height: calc(100vh - 96px);
  border-radius: var(--radius-sm);
  cursor: zoom-out;
  display: block;
}

.gallery-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 50%;
  background: color-mix(in srgb, var(--color-stage-bg) 45%, transparent);
  color: var(--color-on-accent);
  cursor: pointer;
}

.gallery-nav:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-stage-bg) 65%, transparent);
}

.gallery-nav:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.gallery-nav:disabled {
  opacity: 0.3;
  cursor: default;
}

.gallery-prev {
  left: -48px; /* 按钮放图外两侧,不遮图 */
}

.gallery-next {
  right: -48px;
}
</style>
