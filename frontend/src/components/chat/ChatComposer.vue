<script setup lang="ts">
/** composer 输入区(从 ChatView 拆出,见 P3-29)圆角盒(三行):附件预览行
 * (图片缩略图 + 视频块,视频同一时刻最多一个,可移除)/ 无边框 textarea
 * (自动增高)/ 底部功能行(左:「+」上传图片或视频 + 权限模式选择器(逐条确认/
 * 自动通过/完全自主);右:压缩按钮 + 上下文圆环(点击弹层:百分比 + 总量 +
 * 压缩按钮)+ 发送/停止)。等待审批时输入区整体替换为审批面板(拒绝/批准一次/
 * 本会话都批准),回执后 composer 自动恢复。
 * 图片粘贴/选择 ≤4 张走 dataURL,视频粘贴/选择走 /api/agent/uploads 落盘拿
 * path;Enter 发送 / Shift+Enter 换行,输入法合成态(isComposing)中的 Enter
 * 是选词上屏,不发送。进行中输入框不禁用:发送即 /steer 插话,停止走 /cancel。
 * 拖拽投递区是对话整卡(ChatView 的 drag 处理),落盘经 defineExpose 的
 * stageFiles 复用同一入口;发送时 emit('sent') 由父层把时间线滚到底。 */
import { computed, nextTick, onUnmounted, ref } from 'vue'
import { NButton, NInput, NPopover, useMessage } from 'naive-ui'
import { useAgentChatStore } from '../../stores/agentchat'
import type { AgentAccess, AgentApprovalEntry, AgentMode } from '../../stores/agentchat'
import { mdToHtml } from '../../utils/markdown'
import { shouldSendOnEnter, toolLabel } from '../../utils/chatDisplay'
import UiIcon from '../UiIcon.vue'
import ContextRing from './ContextRing.vue'

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
 * 返回 path 作 videoPath;「+」选择 / Ctrl+V 粘贴均走 stageFiles(拖拽由
 * ChatView 的整卡投递区调 stageFiles) ---- */
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

function removeAttachment(i: number) {
  const [a] = pending.value.splice(i, 1)
  if (a) URL.revokeObjectURL(a.url)
}

function clearPending() {
  for (const a of pending.value) URL.revokeObjectURL(a.url)
  pending.value = []
}

/** 组件卸载释放全部 objectURL(原 ChatView 整页卸载清理随拆分移入)。 */
onUnmounted(clearPending)

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

async function onApprove(requestId: string, decision: 'approved' | 'rejected', scope?: 'session') {
  try {
    await agent.respondApproval(requestId, decision, scope)
  } catch (e) {
    message.error(`审批回执失败:${(e as Error).message}`)
  }
}

/** 待处理的审批请求:仅在等待审批状态下取时间线里最后一个未决审批,
 * composer 据此整体替换为审批面板;回执后状态翻回 running,composer 自动恢复。 */
const pendingApproval = computed<AgentApprovalEntry | null>(() => {
  if (agent.status !== 'awaiting_approval') return null
  for (let i = agent.entries.length - 1; i >= 0; i--) {
    const e = agent.entries[i]
    if (e.kind === 'approval') return !e.decision && !e.stale ? e : null
  }
  return null
})
const approvalPreviewOpen = ref(false)
const approvalPreviewHtml = computed(() => {
  const p = pendingApproval.value?.preview
  if (!p) return ''
  return mdToHtml(`\`\`\`${p.language}\n${p.content}\n\`\`\``)
})

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

defineExpose({ stageFiles })
</script>

<template>
<!-- composer 圆角盒(三行):附件预览行 / 文本输入行(自动增高)/ 底部功能行
     (左:「+」上传 + 权限模式选择器;右:压缩按钮 + 上下文圆环 + 发送/停止)。粘贴冒泡到此处统一处理 -->
<div class="chat-composer" @paste="onPaste">
  <div class="composer-box">
    <!-- 审批接管:等待审批时输入区整体替换为审批面板(警示色顶条 + 工具/理由/
         参数摘要 + 三键回执),回执后 status 翻回,composer 自动恢复;
         时间线里的审批卡片只读留档 -->
    <div v-if="pendingApproval" class="approval-panel">
      <div class="approval-panel-bar">
        <UiIcon name="shield" :size="12" />
        <span>等待审批</span>
      </div>
      <div class="approval-panel-body">
        <div class="approval-panel-tool">
          工具:{{ toolLabel(pendingApproval.toolName) }}
        </div>
        <div class="approval-panel-rule">{{ pendingApproval.approvalRule }}</div>
        <div v-if="pendingApproval.description" class="approval-panel-desc">
          {{ pendingApproval.description }}
        </div>
        <div v-if="pendingApproval.accesses.length" class="approval-accesses">
          <div
            v-for="(a, j) in pendingApproval.accesses"
            :key="j"
            class="approval-access"
          >
            {{ accessLabel(a) }}
          </div>
        </div>
        <div v-if="pendingApproval.preview" class="approval-preview-wrap">
          <button
            class="approval-preview-toggle"
            @click="approvalPreviewOpen = !approvalPreviewOpen"
          >
            <span class="preview-caret" :class="{ open: approvalPreviewOpen }">▸</span>
            <span>内容预览</span>
            <span v-if="pendingApproval.preview.truncated" class="preview-truncated">(已截断)</span>
          </button>
          <div v-if="approvalPreviewOpen" class="approval-preview" v-html="approvalPreviewHtml" />
        </div>
        <div class="approval-panel-actions">
          <n-button
            size="small"
            type="error"
            @click="onApprove(pendingApproval.requestId, 'rejected')"
          >
            拒绝
          </n-button>
          <n-button
            size="small"
            type="primary"
            @click="onApprove(pendingApproval.requestId, 'approved')"
          >
            批准一次
          </n-button>
          <n-button
            size="small"
            @click="onApprove(pendingApproval.requestId, 'approved', 'session')"
          >
            本会话都批准
          </n-button>
        </div>
      </div>
    </div>
    <template v-else>
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
    </template>
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

/* ---- composer 审批面板(等待审批时接管输入区;警示色顶条) ---- */
.approval-panel {
  display: flex;
  flex-direction: column;
}

.approval-panel-bar {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin: calc(-1 * var(--space-sm)) calc(-1 * var(--space-sm)) 0;
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius) var(--radius) 0 0;
  background: color-mix(in srgb, var(--color-gold) 18%, var(--color-card));
  color: var(--color-gold);
  font-size: var(--text-xs);
  font-weight: 700;
  font-family: var(--font-pixel); /* 状态顶条(chips 同类)→ 像素 */
}

.approval-panel-body {
  padding-top: var(--space-xs);
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
}

.approval-panel-tool {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--color-text);
  font-family: var(--font-pixel); /* 工具条目头 → 像素 */
}

.approval-panel-rule {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--color-text);
  word-break: break-all;
}

.approval-panel-desc {
  font-size: var(--text-xs);
  color: var(--color-text2);
}

.approval-panel-actions {
  display: flex;
  gap: var(--space-sm);
  margin-top: var(--space-xs);
}

.approval-preview-wrap {
  margin-top: var(--space-xs);
}

.approval-preview-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 2px var(--space-xs);
  border: none;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-gold) 10%, transparent);
  color: var(--color-gold);
  font-size: var(--text-xs);
  font-weight: 600;
  font-family: var(--font-pixel); /* 按钮 → 像素 */
  cursor: pointer;
}

.approval-preview-toggle:hover {
  background: color-mix(in srgb, var(--color-gold) 18%, transparent);
}

.preview-caret {
  display: inline-block;
  transition: transform var(--dur-fast) var(--ease-out);
}

.preview-caret.open {
  transform: rotate(90deg);
}

.preview-truncated {
  color: var(--color-text2);
  font-weight: 400;
}

.approval-preview {
  margin-top: var(--space-xs);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-2);
  overflow-x: auto;
}

.approval-preview :deep(.md) > pre {
  margin: 0;
  padding: var(--space-sm);
  background: transparent;
  border: none;
}

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

/* Naive 按钮(审批回执)字体走主题,覆盖为像素体(原 ChatView 页级规则随拆分带入) */
.chat-composer :deep(.n-button) {
  font-family: var(--font-pixel);
}
</style>
