<script setup lang="ts">
/** 输入区(composer):附件预览行 / 文本输入行 / 底部功能行(「+」上传、权限
 * 模式选择器、压缩按钮、上下文圆环、发送/停止);等待审批时整体替换为审批面板。
 * 自 ChatView 拆出,store 直连;发送清空输入后 emit('sent') 通知父视图滚底,
 * 拖拽落文件的暂存入口 stageFiles 与离开页面的 objectURL 释放(clearPending)
 * 经 defineExpose 暴露给父视图(拖拽区绑在卡片层)。 */
import { computed, nextTick, ref } from 'vue'
import { NButton, NInput, NPopover, useMessage } from 'naive-ui'
import type { AgentAccess, AgentApprovalEntry, AgentMode } from '../../stores/agentchat'
import { useAgentChatStore } from '../../stores/agentchat'
import { mdToHtml } from '../../utils/markdown'
import { shouldSendOnEnter, toolLabel } from '../../utils/chatDisplay'
import UiIcon from '../UiIcon.vue'
import ContextRing from './ContextRing.vue'
import ChatApprovalPanel from './ChatApprovalPanel.vue'

const emit = defineEmits<{ sent: [] }>()
const agent = useAgentChatStore()
const message = useMessage()

const question = ref('')

/** 进行中(建会话/跑轮次/等审批):显示停止;发送仍可用(走 steer 插话)。 */
const busy = computed(
  () =>
    agent.status === 'connecting' ||
    agent.status === 'running' ||
    agent.status === 'awaiting_approval',
)
/* 无会话也可点发送:send 内按当前工作区惰性建会话(工作区切换后 sessionId 已清空);
 * 进行中也可发送(走 /steer 插话),仅 connecting(建会话/切会话在途)禁用 */
const canSend = computed(
  () => !!question.value.trim() && agent.status !== 'connecting' && !videoUploading.value,
)


/* ---- 附件:图片(≤4 张)暂存本地,发送时 FileReader 转 dataURL 随消息上传;
 * 视频(同一时刻最多一个)粘贴/选择/拖拽后先 POST /api/agent/uploads 落盘,
 * 返回 path 作 videoPath;「+」选择 / Ctrl+V 粘贴 / 拖拽均走 stageFiles ---- */
const MAX_IMAGES = 4
interface PendingImg {
  file: File
  url: string
}
const pending = ref<PendingImg[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const videoUploading = ref(false)

function stageImages(imgs: File[]): boolean {
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

/** 视频落盘为待发送附件;已有视频时替换(同一时刻最多一个)。 */
async function stageVideo(file: File) {
  if (videoUploading.value) return
  videoUploading.value = true
  try {
    await agent.uploadVideo(file)
  } catch (e) {
    message.error(`视频上传失败:${(e as Error).message}`)
  } finally {
    videoUploading.value = false
  }
}

async function stageFiles(files: Iterable<File>): Promise<boolean> {
  const list = Array.from(files)
  if (!list.length) return false
  const imgs = list.filter((f) => f.type.startsWith('image/'))
  const vids = list.filter((f) => f.type.startsWith('video/'))
  if (!imgs.length && !vids.length) {
    message.warning('仅支持图片或视频附件')
    return false
  }
  let staged = imgs.length > 0 && stageImages(imgs)
  if (vids.length) {
    if (vids.length > 1) message.warning('视频附件最多一个,已取第一个')
    await stageVideo(vids[0])
    staged = true
  }
  return staged
}

function onFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = '' // 允许重复选择同一文件再次触发 change
  stageFiles(files)
}

/* Ctrl+V 粘贴:有文件(截图/视频)才拦截,纯文本粘贴不受影响 */
function onPaste(ev: ClipboardEvent) {
  const files = ev.clipboardData?.files
  if (!files?.length) return
  ev.preventDefault()
  stageFiles(files).then((ok) => {
    if (ok) message.success('已添加附件')
  })
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

/* ---- composer 键盘:Enter 发送 / Shift+Enter 换行;输入法合成态(isComposing/
 * keyCode 229)中的 Enter 是选词上屏,不发送(判定抽 utils/chatDisplay 便于直测) ---- */
function onComposerEnter(ev: KeyboardEvent) {
  if (!shouldSendOnEnter(ev)) return
  ev.preventDefault()
  onSend()
}

async function onSend() {
  const q = question.value.trim()
  if (!q || !canSend.value) return
  const video = agent.pendingVideo
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
  agent.clearPendingVideo()
  question.value = ''
  await nextTick()
  emit('sent')
  try {
    await agent.send(q, {
      ...(video ? { videoPath: video.path, ...(video.src ? { videoSrc: video.src } : {}) } : {}),
      ...(images ? { images } : {}),
    })
  } catch (e) {
    // 正常轮次失败由失败条呈现;steer 插话失败(非 409)直接提示
    message.error(`发送失败:${(e as Error).message}`)
  }
}

/** 停止:显式终止服务端轮次(断连不再杀轮次,仅 abort 本地流停不掉服务端)。 */
async function onStop() {
  try {
    await agent.cancelTurn()
  } catch (e) {
    message.error(`停止失败:${(e as Error).message}`)
  }
}

/* ---- 上下文用量:圆环 + 压缩按钮(>60% 出现,>85% 警示文案) ---- */
const contextRatio = computed(() =>
  agent.maxTokens > 0 ? (agent.usedTokens ?? 0) / agent.maxTokens : 0,
)
const compacting = ref(false)
const showCompactBtn = computed(() => contextRatio.value > 0.6)
const compactLabel = computed(() =>
  contextRatio.value > 0.85 ? '上下文即将溢出,建议压缩' : '压缩上下文',
)

async function onCompact() {
  if (compacting.value || busy.value) return
  compacting.value = true
  try {
    const r = await agent.compactContext()
    if (r.compacted) message.success('上下文已压缩')
    else message.info('暂无可压缩的上下文')
  } catch (e) {
    message.error(`压缩失败:${(e as Error).message}`)
  } finally {
    compacting.value = false
  }
}

/* ---- 权限模式:composer 底栏下拉选择器(三档);切换走 POST /sessions/{id}/mode 就地生效 ---- */
const MODE_OPTIONS: Array<{ value: AgentMode; label: string; desc: string }> = [
  { value: 'manual', label: '逐条确认', desc: '每个工具操作都需要你手动确认' },
  { value: 'auto', label: '自动通过', desc: '自动批准工具操作,但遇到关键问题仍会询问' },
  { value: 'yolo', label: '完全自主', desc: '完全自主运行,智能体自己做决定,不再询问' },
]
const modeMenuOpen = ref(false)
const modeLabel = computed(
  () => MODE_OPTIONS.find((o) => o.value === agent.mode)?.label ?? agent.mode,
)

async function onModeSelect(m: AgentMode) {
  modeMenuOpen.value = false
  if (m === agent.mode) return
  try {
    await agent.setMode(m)
  } catch (e) {
    message.error(`切换权限模式失败:${(e as Error).message}`)
  }
}

defineExpose({ stageFiles, clearPending })
</script>

<template>
  <div class="chat-composer" @paste="onPaste">
    <div class="composer-box">
      <!-- 审批接管:等待审批时输入区整体替换为审批面板(子组件
           ChatApprovalPanel;回执后 status 翻回,composer 自动恢复);
           时间线里的审批卡片只读留档 -->
      <ChatApprovalPanel />
      <!-- 附件预览行:图片缩略图 + 视频块(小预览或图标块 + 文件名),均可移除 -->
      <div v-if="pending.length || agent.pendingVideo" class="attach-list">
        <div v-for="(a, i) in pending" :key="a.url" class="attach-item">
          <img class="attach-thumb" :src="a.url" :alt="a.file.name" />
          <button class="attach-remove" title="移除" @click="removeAttachment(i)">
            <UiIcon name="close" :size="10" />
          </button>
        </div>
        <div v-if="agent.pendingVideo" class="attach-video">
          <video
            v-if="agent.pendingVideo.src"
            class="attach-video-preview"
            :src="agent.pendingVideo.src"
            preload="metadata"
            muted
          />
          <span v-else class="attach-video-icon"><UiIcon name="video" :size="16" /></span>
          <span class="attach-video-name" :title="agent.pendingVideo.path">
            {{ agent.pendingVideo.name }}
          </span>
          <button class="attach-remove" title="移除" @click="agent.clearPendingVideo()">
            <UiIcon name="close" :size="10" />
          </button>
        </div>
      </div>
      <!-- 文本输入行:无边框 textarea,自动增高时底部行跟随;
           进行中不禁用(发送即 steer 插话),仅 connecting 禁用 -->
      <n-input
        v-model:value="question"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 4 }"
        :bordered="false"
        placeholder="输入问题或检测指令,Enter 发送,Shift+Enter 换行"
        :disabled="agent.status === 'connecting'"
        @keydown.enter="onComposerEnter"
      />
      <!-- 底部功能行 -->
      <div class="composer-bar">
        <button
          class="bar-btn"
          title="添加图片或视频附件"
          :disabled="busy || videoUploading"
          @click="fileInput?.click()"
        >
          <UiIcon name="plus" :size="14" />
        </button>
        <!-- 权限模式选择器:当前模式名 + 盾牌图标,点击弹菜单(当前项打勾) -->
        <n-popover v-model:show="modeMenuOpen" trigger="click" placement="top-start">
          <template #trigger>
            <button class="mode-btn" title="权限模式" :disabled="busy">
              <UiIcon name="shield" :size="12" />
              <span class="mode-btn-label">{{ modeLabel }}</span>
            </button>
          </template>
          <div class="mode-menu">
            <button
              v-for="opt in MODE_OPTIONS"
              :key="opt.value"
              class="mode-item"
              @click="onModeSelect(opt.value)"
            >
              <span class="mode-item-check">
                <UiIcon v-if="opt.value === agent.mode" name="check" :size="12" />
              </span>
              <span class="mode-item-text">
                <span class="mode-item-title">{{ opt.label }}</span>
                <span class="mode-item-desc">{{ opt.desc }}</span>
              </span>
            </button>
          </div>
        </n-popover>
        <span v-if="videoUploading" class="bar-hint">视频上传中…</span>
        <span class="bar-spacer" />
        <button
          v-if="showCompactBtn"
          class="compact-btn"
          :class="{ danger: contextRatio > 0.85 }"
          :disabled="busy || compacting"
          @click="onCompact"
        >
          {{ compacting ? '压缩中…' : compactLabel }}
        </button>
        <ContextRing
          :used="agent.usedTokens"
          :max="agent.maxTokens"
          :can-compact="showCompactBtn"
          :compacting="compacting || busy"
          @compact="onCompact"
        />
        <!-- 停止(显式 cancel 服务端轮次)与发送并存:进行中发送即 steer 插话 -->
        <button v-if="busy" class="bar-btn" title="停止" @click="onStop">
          <UiIcon name="stop" :size="12" />
        </button>
        <button
          class="send-btn"
          :title="busy ? '插话(进行中注入)' : '发送'"
          :disabled="!canSend"
          @click="onSend"
        >
          <UiIcon name="send" :size="13" />
        </button>
      </div>
    </div>
    <input
      ref="fileInput"
      type="file"
      hidden
      multiple
      accept="image/*,video/*"
      @change="onFiles"
    />
  </div>
</template>

<style scoped>
/* ---- 输入区(composer 圆角盒:附件预览行 / 文本输入行 / 底部功能行) ---- */
.chat-composer {
  border-top: 1px solid var(--color-border);
  padding: var(--space-sm) var(--space-lg);
}

.composer-box {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  background: var(--color-surface-2);
  padding: var(--space-sm);
  transition: border-color var(--dur-fast) var(--ease-out);
}

.composer-box:focus-within {
  border-color: var(--color-accent);
}

/* 盒内 textarea 无边框无底色(融入盒子),禁用态不动盒子描边 */
.composer-box :deep(.n-input) {
  background: transparent;
}

.composer-box :deep(.n-input__textarea-el) {
  font-size: var(--text-md);
}

/* ---- 底部功能行:左「+」+ 权限模式选择器,右 压缩按钮 + 圆环 + 发送/停止 ---- */
.composer-bar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.bar-spacer {
  flex: 1;
}

.bar-hint {
  font-size: var(--text-xs);
  color: var(--color-text2);
  font-family: var(--font-pixel); /* 状态小标(chips 同类)→ 像素 */
}

.bar-btn,
.send-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border-radius: 50%;
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.bar-btn {
  border: 1px solid var(--color-border);
  background: var(--color-card);
  color: var(--color-text2);
}

.bar-btn:hover:not(:disabled) {
  border-color: var(--color-accent);
  color: var(--color-accent);
  background: var(--color-hover-bg);
}

.bar-btn:active:not(:disabled) {
  background: var(--color-accent-soft);
}

.send-btn {
  border: 1px solid var(--color-accent);
  background: var(--color-accent);
  color: var(--color-on-accent);
}

.send-btn:hover:not(:disabled) {
  background: var(--color-accent-hover);
  border-color: var(--color-accent-hover);
}

.send-btn:active:not(:disabled) {
  filter: brightness(0.95);
}

.bar-btn:focus-visible,
.send-btn:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.bar-btn:disabled,
.send-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

/* ---- 权限模式选择器(触发钮 + 弹层菜单;按钮/菜单均骨架 → 像素) ---- */
.mode-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  height: 30px;
  padding: 0 var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-card);
  color: var(--color-text2);
  font-size: var(--text-xs);
  font-family: var(--font-pixel);
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.mode-btn:hover:not(:disabled) {
  border-color: var(--color-accent);
  color: var(--color-accent);
  background: var(--color-hover-bg);
}

.mode-btn:active:not(:disabled) {
  background: var(--color-accent-soft);
}

.mode-btn:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.mode-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.mode-btn-label {
  font-weight: 600;
}

.mode-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 260px;
  font-family: var(--font-pixel); /* 菜单(按钮簇)→ 像素 */
}

.mode-item {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  padding: var(--space-xs) var(--space-sm);
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  cursor: pointer;
  text-align: left;
}

.mode-item:hover {
  background: var(--color-hover-bg);
}

.mode-item:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: -2px;
}

/* 勾位固定宽,未选中项与选中项标题对齐 */
.mode-item-check {
  flex: 0 0 14px;
  display: inline-flex;
  justify-content: center;
  padding-top: 1px;
  color: var(--color-accent);
}

.mode-item-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mode-item-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-text);
}

.mode-item-desc {
  font-size: var(--text-xs);
  color: var(--color-text2);
}

.compact-btn {
  padding: 2px var(--space-sm);
  border: 1px solid var(--color-gold);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, var(--color-card));
  color: var(--color-gold);
  font-size: var(--text-xs);
  font-weight: 600;
  font-family: var(--font-pixel); /* 按钮 → 像素 */
  cursor: pointer;
}

.compact-btn.danger {
  border-color: var(--color-red);
  background: var(--color-red-soft);
  color: var(--color-red);
}

.compact-btn:hover:not(:disabled) {
  filter: brightness(0.97);
}

.compact-btn:disabled {
  opacity: 0.5;
  cursor: default;
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

/* ---- 视频附件块(composer 预览行):小预览或图标块 + 文件名 ---- */
.attach-video {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  max-width: 280px;
  padding: var(--space-xs) var(--space-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-card);
}

.attach-video-preview {
  width: 72px;
  height: 48px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--color-stage-bg);
}

.attach-video-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 48px;
  border-radius: var(--radius-sm);
  background: var(--color-stage-bg);
  color: var(--color-on-accent);
}

.attach-video-name {
  flex: 1;
  min-width: 0;
  /* 文件名精确辨认 → sans(design.md §2;路径悬停 title 展示) */
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  color: var(--color-text2);
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
</style>
