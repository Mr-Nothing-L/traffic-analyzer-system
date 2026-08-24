<script setup lang="ts">
/** 快速对话整卡:来源条(会话标题 = 首个问题缩略/清空记忆)+ 消息列表(SSE 流式气泡,
 * 附件随 user 消息进气泡:图片缩略图/视频 chip 在文字上方;assistant 正文 markdown
 * 渲染;思考过程折叠块收起时单行流式指示;带撤回/复制/时间操作栏)
 * + 输入区(「+」/ Ctrl+V 粘贴 / 拖拽三种方式暂存附件随消息一同上传、
 * Enter 发送 / Shift+Enter 换行 / 发送中可停止)。
 * 与数据看板同模式:TreeView 子路由,整卡替换主区;状态在 stores/chat.ts,组件只接线。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  NButton,
  NInput,
  NModal,
  NPopconfirm,
  NScrollbar,
  useMessage,
} from 'naive-ui'
import { useChatStore } from '../stores/chat'
import type { ChatMessage } from '../stores/chat'
import { useAppStore } from '../stores/app'
import { mdToHtml } from '../utils/markdown'
import UiIcon from '../components/UiIcon.vue'

const chat = useChatStore()
const app = useAppStore()
const message = useMessage()

/* ---- 用户头像:登录名首字母大写;未登录显示「我」 ---- */
const userInitial = computed(() => (app.user ? app.user[0].toUpperCase() : '我'))

const question = ref('')
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

/* ---- 暂存附件(随消息一同发出):图片给缩略图 objectURL,视频给图标+文件名 ---- */
interface PendingFile {
  file: File
  url: string
  isVideo: boolean
}
const pending = ref<PendingFile[]>([])
const VIDEO_EXT = ['.mp4', '.avi', '.mov', '.mkv', '.ts']
const isVideoFile = (f: File) => VIDEO_EXT.some((e) => f.name.toLowerCase().endsWith(e))

/** 公共入口(「+」选择 / Ctrl+V 粘贴 / 拖拽均走这里):
 * 校验(视频恰 1 个且不能与图片混选,与后端一致,合并已暂存一起判)→ 加入 pending。
 * 违规 message.warning 且不予暂存;返回是否暂存成功。 */
function stageFiles(files: Iterable<File>): boolean {
  const list = Array.from(files)
  if (!list.length) return false
  const all = [...pending.value.map((p) => p.file), ...list]
  const videos = all.filter(isVideoFile)
  if (videos.length && (videos.length !== 1 || videos.length !== all.length)) {
    message.warning('视频只能选 1 个,且不能与图片混选')
    return false
  }
  for (const f of list) {
    pending.value.push({ file: f, url: URL.createObjectURL(f), isVideo: isVideoFile(f) })
  }
  return true
}

function onFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = '' // 允许重复选择同一文件再次触发 change
  stageFiles(files)
}

/* ---- Ctrl+V 粘贴:有文件(如截图)才拦截,纯文本粘贴不受影响 ---- */
function onPaste(ev: ClipboardEvent) {
  const files = ev.clipboardData?.files
  if (!files?.length) return
  ev.preventDefault()
  if (stageFiles(files)) message.success('已添加附件')
}

/* ---- 拖拽上传:计数器防子元素进出导致的 dragenter/dragleave 抖动 ---- */
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
  if (files?.length && stageFiles(files)) message.success('已添加附件')
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

/* ---- 来源预览:附件随消息进气泡,不再有顶部预览条 ---- */

/* ---- 图片:加载失败隐藏(onerror);点击放大(NModal);视频 chip 点击 modal 播放 ---- */
const broken = reactive(new Set<string>())
const previewUrl = ref<string | null>(null)
const previewVideo = ref<string | null>(null)

const isVideoUrl = (u: string) => VIDEO_EXT.some((e) => u.toLowerCase().endsWith(e))
const imgOnly = (images: string[]) => images.filter((u) => !isVideoUrl(u))
const vidOnly = (images: string[]) => images.filter(isVideoUrl)
/** 落盘名 <uuid>_<原名>:显示时剥掉 uuid 前缀(取第一个 _ 之后,无则全显,兼容历史文件)。 */
const fileName = (u: string) => {
  const base = u.split('/').pop() || u
  const i = base.indexOf('_')
  return i >= 0 ? base.slice(i + 1) : base
}
/** 视频播放走 /api/chat/video/(Range + 按需转码,H.265/.ts 浏览器原生放不了)。 */
const videoStreamUrl = (u: string) => u.replace('/api/chat/files/', '/api/chat/video/')

/* ---- 来源条标题:第一个 user 问题的缩略;无问题时显示「快速对话」 ---- */
const chatTitle = computed(() => {
  const first = chat.messages.find((m) => m.role === 'user')
  if (!first) return '快速对话'
  const t = first.content.trim().replace(/\s+/g, ' ')
  return t.length > 24 ? `${t.slice(0, 24)}…` : t
})

/* ---- 思考过程折叠块:每条消息独立展开状态;折叠时显示最新一行(流式实时) ---- */
const thinkOpen = reactive(new Set<string>())
const thinkKey = (m: ChatMessage, i: number) => (m.id != null ? `id-${m.id}` : `idx-${i}`)
function toggleThink(m: ChatMessage, i: number) {
  const k = thinkKey(m, i)
  if (thinkOpen.has(k)) thinkOpen.delete(k)
  else thinkOpen.add(k)
}
const lastThinkLine = (think: string) =>
  think
    .split('\n')
    .filter((l) => l.trim())
    .pop() || ''

/* ---- 消息列表:历史 + 流式中气泡;新增/增长时自动滚底 ---- */
const scrollbar = ref<InstanceType<typeof NScrollbar> | null>(null)
const displayMessages = computed(() => {
  const arr = [...chat.messages]
  if (chat.current) arr.push(chat.current)
  return arr
})

watch(
  () => [
    chat.messages.length,
    chat.current?.content.length,
    chat.current?.think.length,
    chat.current?.images.length,
  ],
  async () => {
    await nextTick()
    scrollbar.value?.scrollTo({ top: Number.MAX_SAFE_INTEGER })
  },
)

onMounted(async () => {
  window.addEventListener('dragover', preventWindowDrop)
  window.addEventListener('drop', preventWindowDrop)
  try {
    await chat.fetchState()
  } catch (e) {
    message.error(`加载对话状态失败:${(e as Error).message}`)
  }
})

onUnmounted(() => {
  window.removeEventListener('dragover', preventWindowDrop)
  window.removeEventListener('drop', preventWindowDrop)
  chat.stop() // 离开页面中断在途流
  clearPending() // 释放全部 objectURL
})

/* ---- 来源条动作 ---- */
async function onClear() {
  try {
    await chat.clear()
    message.success('已清空对话记忆')
  } catch (e) {
    message.error(`清空失败:${(e as Error).message}`)
  }
}

/* ---- 提问:有暂存附件先上传(失败则保留附件、不发问题),成功后附件 URL 随问题走 SSE ---- */
async function onSend() {
  const q = question.value.trim()
  if (!q || chat.sending || uploading.value) return
  let sentUrls: string[] = []
  if (pending.value.length) {
    uploading.value = true
    try {
      await chat.upload(pending.value.map((p) => p.file))
    } catch (e) {
      message.error(`上传失败:${(e as Error).message}`)
      return
    } finally {
      uploading.value = false
    }
    sentUrls = [...(chat.source?.files || [])] // 本次上传的可预览 URL,随消息进气泡
    clearPending()
  }
  question.value = ''
  try {
    await chat.ask(q, sentUrls)
  } catch (e) {
    message.error((e as Error).message)
  }
}

/* ---- 气泡操作栏:撤回(仅 user 且已落库有 id)/ 复制 / 时间 ---- */
function fmtTime(ts: number) {
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

async function onCopy(m: ChatMessage) {
  try {
    await navigator.clipboard.writeText(m.content)
    message.success('已复制')
  } catch {
    message.warning('复制失败,请手动选择文本复制')
  }
}

async function onRecall(m: ChatMessage) {
  if (m.id == null) return
  try {
    question.value = await chat.recall(m.id) // 原文放回输入框供重新编辑
  } catch (e) {
    message.error(`撤回失败:${(e as Error).message}`)
  }
}
</script>

<template>
  <div class="chat-page">
    <section
      class="chat-card"
      @dragenter="onDragEnter"
      @dragover="onDragOver"
      @dragleave="onDragLeave"
      @drop="onDrop"
    >
      <!-- 拖拽提示覆盖层(pointer-events: none,不干扰 dragleave 计数) -->
      <div v-show="dragOver" class="drop-overlay">
        松开鼠标,将图片/视频添加为附件
      </div>
      <!-- 来源条:标题 = 第一个问题的缩略 -->
      <div class="chat-source">
        <UiIcon name="chat" :size="14" />
        <span class="chat-source-name" :title="chatTitle">{{ chatTitle }}</span>
        <span class="chat-spacer" />
        <n-popconfirm @positive-click="onClear">
          <template #trigger>
            <n-button size="small">清空记忆</n-button>
          </template>
          清空全部对话记忆?
        </n-popconfirm>
      </div>

      <!-- 消息列表:微信式布局(头像 + 气泡);padding 放在 inner 上,不依赖 n-scrollbar 根元素 -->
      <n-scrollbar ref="scrollbar" class="chat-scroll">
        <div class="chat-scroll-inner">
          <div v-if="!displayMessages.length" class="chat-empty">
            上传视频/图片,然后开始提问
          </div>
          <template v-for="(m, i) in displayMessages" :key="i">
            <div v-if="m.role === 'divider'" class="chat-divider">{{ m.content }}</div>
            <div v-else class="chat-row" :class="m.role">
              <div v-if="m.role === 'assistant'" class="avatar avatar-assistant">
                <UiIcon name="chip" :size="18" />
              </div>
              <div class="bubble">
                <!-- 思考过程:自控折叠;收起时单行显示最新思考(流式实时),展开显示全文 -->
                <div v-if="m.role === 'assistant' && m.think" class="think">
                  <button class="think-head" @click="toggleThink(m, i)">
                    <UiIcon
                      name="up"
                      :size="10"
                      class="think-caret"
                      :class="{ open: thinkOpen.has(thinkKey(m, i)) }"
                    />
                    <span>思考过程</span>
                  </button>
                  <div v-if="thinkOpen.has(thinkKey(m, i))" class="think-text">{{ m.think }}</div>
                  <div v-else class="think-line">{{ lastThinkLine(m.think) }}</div>
                </div>
                <!-- user 附件在文字上方(微信式):视频 chip 点击 modal 播放,图片缩略图点击放大 -->
                <template v-if="m.role === 'user'">
                  <div v-if="vidOnly(m.images).length" class="vid-group">
                    <button
                      v-for="u in vidOnly(m.images)"
                      :key="u"
                      class="vid-chip"
                      :title="fileName(u)"
                      @click="previewVideo = videoStreamUrl(u)"
                    >
                      <UiIcon name="video" :size="14" />
                      <span class="vid-name">{{ fileName(u) }}</span>
                    </button>
                  </div>
                  <div v-if="imgOnly(m.images).length" class="img-group img-group-top">
                    <img
                      v-for="u in imgOnly(m.images)"
                      v-show="!broken.has(u)"
                      :key="u"
                      :src="u"
                      alt=""
                      loading="lazy"
                      @error="broken.add(u)"
                      @click="previewUrl = u"
                    />
                  </div>
                </template>
                <!-- assistant 正文渲染 markdown(utils/markdown.ts,esc 先行可安全 v-html);
                     user 保持纯文本;think 区不渲染 -->
                <div
                  v-if="m.role === 'assistant' && m.content"
                  class="bubble-text bubble-md"
                  v-html="mdToHtml(m.content)"
                />
                <div v-else-if="m.content" class="bubble-text">{{ m.content }}</div>
                <div v-if="m.role === 'assistant' && m.images.length" class="img-group">
                  <img
                    v-for="u in m.images"
                    v-show="!broken.has(u)"
                    :key="u"
                    :src="u"
                    alt=""
                    loading="lazy"
                    @error="broken.add(u)"
                    @click="previewUrl = u"
                  />
                </div>
                <!-- 操作栏:user 有撤回(仅已落库消息);复制/时间两侧都有 -->
                <div class="msg-actions">
                  <button
                    v-if="m.role === 'user' && m.id != null"
                    class="msg-action"
                    title="撤回并重新编辑"
                    @click="onRecall(m)"
                  >
                    <UiIcon name="undo" :size="12" />
                  </button>
                  <button class="msg-action" title="复制" @click="onCopy(m)">
                    <UiIcon name="copy" :size="12" />
                  </button>
                  <span class="msg-time">{{ fmtTime(m.created_at) }}</span>
                </div>
              </div>
              <div v-if="m.role === 'user'" class="avatar avatar-user">{{ userInitial }}</div>
            </div>
          </template>
        </div>
      </n-scrollbar>

      <!-- 输入区:暂存附件区 + 「+」选择文件 + 输入框 + 停止/发送;粘贴事件冒泡到此处统一处理 -->
      <div class="chat-composer" @paste="onPaste">
        <div v-if="pending.length" class="attach-list">
          <div v-for="(a, i) in pending" :key="a.url" class="attach-item">
            <img v-if="!a.isVideo" class="attach-thumb" :src="a.url" alt="" />
            <span v-else class="attach-video" :title="a.file.name">
              <UiIcon name="video" :size="14" />
              <span class="attach-name">{{ a.file.name }}</span>
            </span>
            <button class="attach-remove" title="移除" @click="removeAttachment(i)">
              <UiIcon name="close" :size="10" />
            </button>
          </div>
        </div>
        <div class="chat-input">
          <n-button size="small" quaternary title="添加视频/图片" @click="fileInput?.click()">
            <template #icon><UiIcon name="plus" :size="14" /></template>
          </n-button>
          <n-input
            v-model:value="question"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 4 }"
            placeholder="输入问题,Enter 发送,Shift+Enter 换行"
            @keydown.enter.exact.prevent="onSend"
          />
          <n-button v-if="chat.sending" size="small" @click="chat.stop()">停止</n-button>
          <n-button
            v-else
            type="primary"
            size="small"
            :loading="uploading"
            :disabled="!question.trim() || uploading"
            @click="onSend"
          >
            <template v-if="!uploading" #icon><UiIcon name="send" :size="12" /></template>
            {{ uploading ? '上传中…' : '发送' }}
          </n-button>
        </div>
        <input
          ref="fileInput"
          type="file"
          hidden
          multiple
          accept=".mp4,.avi,.mov,.mkv,.ts,.jpg,.jpeg,.png,.webp"
          @change="onFiles"
        />
      </div>
    </section>

    <!-- 图片放大预览 -->
    <n-modal :show="!!previewUrl" @update:show="previewUrl = null">
      <img v-if="previewUrl" class="preview-img" :src="previewUrl" alt="" @click="previewUrl = null" />
    </n-modal>

    <!-- 视频播放预览(user 气泡内的视频 chip) -->
    <n-modal :show="!!previewVideo" @update:show="previewVideo = null">
      <video v-if="previewVideo" class="preview-video" :src="previewVideo" controls autoplay />
    </n-modal>
  </div>
</template>

<style scoped>
/* 与看板不同:对话需要内部滚动,卡片占满主区可视高度(.app-main 高度确定,height:100% 有效) */
.chat-page {
  height: 100%;
  display: flex;
  min-height: 0;
}

.chat-card {
  flex: 1;
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

/* ---- 来源条 ---- */
.chat-source {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text2);
}

.chat-source-name {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-spacer {
  flex: 1;
}

/* ---- 消息列表 ---- */
.chat-scroll {
  flex: 1;
  min-height: 0;
}

/* padding 放 inner:naive-ui 滚动容器结构下根元素 padding 表现不一致,
 * 内层容器保证头像外侧到卡片边缘恒有 24px 留白 */
.chat-scroll-inner {
  padding: var(--space-md) var(--space-lg);
}

.chat-empty {
  padding: var(--space-2xl) var(--space-md);
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-md);
}

.chat-divider {
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-xs);
  margin: var(--space-md) 0 var(--space-sm);
}

.chat-row {
  display: flex;
  align-items: flex-start; /* 头像与气泡顶部对齐,不随气泡拉伸 */
  gap: var(--space-sm);
  margin: var(--space-sm) 0;
}

.chat-row.user {
  justify-content: flex-end;
}

.chat-row.assistant {
  justify-content: flex-start;
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
  font-weight: 650;
  font-size: var(--text-sm);
}

.bubble {
  max-width: 65%;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.chat-row.user .bubble {
  background: var(--color-accent-soft);
  border-color: var(--color-accent-deep);
}

.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--text-md);
  line-height: 1.6;
}

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

/* 收起时的单行流式指示:最新一行,溢出省略号 */
.think-line {
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- assistant 正文 markdown(mdToHtml 输出的 .md 容器) ---- */
.bubble-md {
  white-space: normal; /* 覆盖 .bubble-text 的 pre-wrap,交由 md 标签排版 */
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

.img-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-top: var(--space-sm);
}

/* user 附件在文字上方:上边距改为下边距 */
.img-group-top {
  margin-top: 0;
  margin-bottom: var(--space-sm);
}

.img-group img {
  width: 120px;
  height: 80px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* ---- user 气泡内的视频附件 chip ---- */
.vid-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-bottom: var(--space-sm);
}

.vid-chip {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 220px;
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface-2);
  color: var(--color-text2);
  font-size: var(--text-xs);
  cursor: pointer;
}

.vid-chip:hover {
  color: var(--color-accent);
  border-color: var(--color-accent);
}

.vid-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 气泡操作栏(撤回/复制/时间) ---- */
.msg-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-sm);
  margin-top: var(--space-xs);
  color: var(--color-text2);
  font-size: var(--text-xs);
}

.msg-action {
  display: inline-flex;
  align-items: center;
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  cursor: pointer;
}

.msg-action:hover {
  color: var(--color-accent);
}

.msg-time {
  line-height: 1;
}

/* ---- 输入区 ---- */
.chat-composer {
  border-top: 1px solid var(--color-border);
}

.chat-input {
  display: flex;
  align-items: flex-end;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-lg);
}

/* ---- 暂存附件区 ---- */
.attach-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-lg) 0;
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

.attach-video {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 180px;
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface-2);
  color: var(--color-text2);
  font-size: var(--text-xs);
}

.attach-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

/* ---- 图片放大 ---- */
.preview-img {
  max-width: calc(100vw - 96px);
  max-height: calc(100vh - 96px);
  border-radius: var(--radius-sm);
  cursor: zoom-out;
  display: block;
}

/* ---- 视频播放 ---- */
.preview-video {
  max-width: calc(100vw - 96px);
  max-height: calc(100vh - 96px);
  border-radius: var(--radius-sm);
  background: #000;
  display: block;
}
</style>
