<script setup lang="ts">
/** 统一对话整卡(问答 + 检测,后端统一走 /api/agent/*):
 * 左侧历史会话栏(按工作区分组:当前工作区组最上恒展开,其他工作区/未分组组
 * 默认折叠;条目 title + 相对时间 / 点击切换(跨工作区先切工作区)重建时间线 /
 * 删除(optimistic + 确认)/ 新建)
 * + 右侧对话卡:顶条(状态 chip)/ 时间线(user 气泡(图片附件 + 视频预览或路径 chip)/
 * assistant 流式气泡(思考折叠(运行中末行横向跟随/结束后首行)+ 增量 markdown
 * (流式期间冻结已完成块,结束后一次性完整渲染,见 components/chat/MdStream))/ 
 * 消息底部行(HH:MM + hover/focus-within 显现的复制(成功变 ✓ 一秒);user 另有撤回,
 * 调 recall API 从时间线移除该条及其后全部,进行中禁用)/ 工具气泡(参数摘要 + 结果折叠
 * (文本 + 图片缩略图,点击进画廊),失败时折叠态直接显示错误首行,标红)/
 * 审批(approval_request 到达时 composer 输入区整体替换为审批面板
 * (拒绝/批准一次/本会话都批准),时间线里的审批卡只读留档;历史未决显示「已失效」)/
 * 检测结果卡(11 位编码等宽高亮 +
 * 检出事件(逐事件标注图进画廊,无框/画框失败显示降级小字)+ markdown 报告))/ 失败条(错误 + 重试)/
 * 恢复条(刷新/断网后服务端轮次仍在跑:常驻「分析仍在进行中」+ 刷新进度,5s 轮询补齐)/
 * composer 圆角盒(三行:附件预览行(图片缩略图 +
 * 视频块,视频同一时刻最多一个,可移除)/ 无边框 textarea(自动增高)/ 底部功能行(左:「+」
 * 上传图片或视频 + 权限模式选择器(逐条确认/自动通过/完全自主);右:压缩按钮 + 上下文圆环
 * (点击弹层:百分比 + 总量 + 压缩按钮)+ 发送/停止);图片粘贴/选择/拖拽 ≤4 张走 dataURL,
 * 视频粘贴/选择/拖拽走 /api/agent/uploads 落盘拿 path;Enter 发送 / Shift+Enter 换行,
 * 输入法合成态(isComposing)中的 Enter 是选词上屏,不发送)。
 * 进行中输入框不禁用:发送即 /steer 插话(气泡带「已插话」标记),停止走 /cancel 显式终止。
 * 工作区视频(无 src)气泡内按 path 确定性推 /api/workspace/stream 小播放器预览;
 * 工具条目为思考过程同款弱化样式(无卡片,工具名走中文映射),点击展开结果。
 * 对话进行中时间线底部常驻一行「分析中…」,超过 15s 追加秒表。
 * 图片画廊:点击气泡图放大,左右切换(按钮/键盘 ←→)。
 * 状态在 stores/agentchat.ts,组件只接线。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  NButton,
  NInput,
  NModal,
  NPopconfirm,
  NPopover,
  NScrollbar,
  useMessage,
} from 'naive-ui'
import { useAgentChatStore } from '../stores/agentchat'
import type { AgentAccess, AgentApprovalEntry, AgentMode, AgentSessionInfo, AgentUserEntry, DetectionPayload } from '../stores/agentchat'
import { useWorkspaceStore } from '../stores/workspace'
import { ApiError } from '../api/client'
import { mdToHtml } from '../utils/markdown'
import { groupSessionsByWorkspace } from '../utils/sessionGroups'
import { copyText, shouldSendOnEnter, toolLabel, workspaceVideoSrc } from '../utils/chatDisplay'
import UiIcon from '../components/UiIcon.vue'
import ContextRing from '../components/chat/ContextRing.vue'
import ChatEntryUser from '../components/chat/ChatEntryUser.vue'
import ChatEntryAssistant from '../components/chat/ChatEntryAssistant.vue'
import ChatEntryTool from '../components/chat/ChatEntryTool.vue'
import ChatEntryApproval from '../components/chat/ChatEntryApproval.vue'
import ChatEntrySystem from '../components/chat/ChatEntrySystem.vue'
import ChatEntryDetection from '../components/chat/ChatEntryDetection.vue'

const agent = useAgentChatStore()
const ws = useWorkspaceStore()
const message = useMessage()

const question = ref('')

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

/* ---- 历史会话栏:按最近活跃倒序;相对时间(后端为 epoch ms) ---- */
const sortedSessions = computed(() =>
  [...agent.sessions].sort((a, b) => (b.lastActiveAt ?? 0) - (a.lastActiveAt ?? 0)),
)

/* 会话按工作区分组(utils/sessionGroups,参考 kimi-code 侧栏:工作区文件夹为组,
 * 组下挂会话):当前工作区组最上(无标题、恒展开);其余工作区各一组(标题 =
 * basename,悬停显示完整路径,默认折叠、点击展开);无 workspaceDir 的旧会话
 * 归「未分组」组沉底;组内按 lastActiveAt 倒序。 */
const sessionGroups = computed(() => groupSessionsByWorkspace(agent.sessions, ws.path))
/** 当前工作区组会话(空态提示与进入页面续接最近会话用)。 */
const ownSessions = computed(
  () => sessionGroups.value.find((g) => g.key === 'own')?.items ?? [],
)

/** 其他工作区/未分组组的展开态(默认折叠;当前工作区组恒展开,不在此列)。 */
const expandedGroups = reactive(new Set<string>())
function toggleGroup(key: string) {
  toggle(expandedGroups, key)
}

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

/** 点击会话条目:跨工作区会话由 openSession 先切工作区再选会话;
 * 工作区切换失败(目录不存在/不在白名单)提示「工作区不可用」,不切换。 */
async function onSelect(s: AgentSessionInfo) {
  try {
    await agent.openSession(s.id)
  } catch (e) {
    message.error(`工作区不可用:${(e as Error).message}`)
    return
  }
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

/* ---- 消息底部行:HH:MM 时间 + hover/focus-within 显现的操作(复制;user 另有撤回) ---- */
function fmtTime(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** 复制成功标记:按条目 id 记住刚复制的那条,图标变 ✓,1s 后换回。 */
const copiedKey = ref<string | null>(null)
let copiedTimer: ReturnType<typeof setTimeout> | null = null

/** 复制消息文本(成功图标变 ✓ 一秒,失败才提示)。非安全上下文(局域网 IP 直连)
 * 无 navigator.clipboard,copyText 内部回退 execCommand。 */
async function onCopy(key: string, text: string) {
  try {
    await copyText(text)
  } catch {
    message.error('复制失败')
    return
  }
  copiedKey.value = key
  if (copiedTimer !== null) clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => {
    copiedKey.value = null
    copiedTimer = null
  }, 1000)
}

/** 撤回:后端删库后本地移除该条及其后全部;409(进行中)按钮已禁用,兜底提示。 */
async function onRecall(e: AgentUserEntry) {
  try {
    await agent.recallFrom(e.id)
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) message.warning('对话进行中,无法撤回')
    else if (err instanceof ApiError && err.message.includes('尚未落盘')) return // 乐观条目未确认,静默
    else message.error(`撤回失败:${(err as Error).message}`)
  }
}

/* ---- 折叠状态:思考过程 / 工具结果 / 子代理思考,均按条目 id 标记,默认收起;
 * id 随条目全局唯一且随会话重建,切换/新建会话后旧 id 自然失效(不会串到新
 * 会话的条目上,无需手工重置;撤回/换会话后残留 id 无人引用)。 ---- */
const thinkOpen = reactive(new Set<string>())
const toolOpen = reactive(new Set<string>())
/** 子代理思考折叠:`${工具条目 id}:${child 下标}`。 */
const subThinkOpen = reactive(new Set<string>())
function toggle<T>(set: Set<T>, key: T) {
  if (set.has(key)) set.delete(key)
  else set.add(key)
}

/* ---- 流式渲染态:最后一个 assistant 条目在对话进行中走增量渲染;
 * 思考折叠行仅在「思考仍在流入」(该条目还没有正文)时取末行并横向跟随 ---- */
const streamingEntry = computed(() => {
  if (!busy.value) return null
  const last = agent.entries[agent.entries.length - 1]
  return last?.kind === 'assistant' ? last : null
})


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

/* ---- 检测结果卡:data 正常是结构化 payload;后端解析失败时为原始字符串 ---- */
function asPayload(data: unknown): DetectionPayload | null {
  return data && typeof data === 'object' ? (data as DetectionPayload) : null
}

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
  if (!files?.length) return
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

/* ---- 图片画廊:user 气泡附件图 / 工具结果图 / 检测卡逐事件标注图按时间序进画廊;
 * 点击放大,左右切换(按钮/键盘) ---- */
const previewIndex = ref<number | null>(null)

const galleryImages = computed(() => {
  const urls: string[] = []
  for (const e of agent.entries) {
    if (e.kind === 'user' && e.images?.length) urls.push(...e.images)
    else if (e.kind === 'tool' && e.images.length) urls.push(...e.images)
    else if (e.kind === 'detection') {
      const p = asPayload(e.data)
      if (p?.events) {
        for (const ev of p.events) if (ev.annotated_image) urls.push(ev.annotated_image)
      }
    }
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

/* ---- Turn 级 loading:对话进行中(connecting/running,等审批不算——composer 已被
 * 审批面板接管)时间线底部常驻一行「分析中…」,超过 15s 追加秒表。
 * 只由 status 驱动,step 之间状态不变,行不闪烁。 ---- */
const turnActive = computed(
  () => agent.status === 'connecting' || agent.status === 'running',
)
const busySince = ref<number | null>(null)
const nowTick = ref(0)
let tickTimer: ReturnType<typeof setInterval> | null = null
watch(turnActive, (active) => {
  if (active) {
    busySince.value = Date.now()
    nowTick.value = Date.now()
    if (tickTimer === null) {
      tickTimer = setInterval(() => {
        nowTick.value = Date.now()
      }, 1000)
    }
  } else {
    busySince.value = null
    if (tickTimer !== null) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  }
})
/** 已进行秒数(超过 15s 才在时间线 loading 行展示)。 */
const turnElapsedSec = computed(() =>
  busySince.value === null
    ? 0
    : Math.max(0, Math.floor((nowTick.value - busySince.value) / 1000)),
)

/* ---- composer 键盘:Enter 发送 / Shift+Enter 换行;输入法合成态(isComposing/
 * keyCode 229)中的 Enter 是选词上屏,不发送(判定抽 utils/chatDisplay 便于直测) ---- */
function onComposerEnter(ev: KeyboardEvent) {
  if (!shouldSendOnEnter(ev)) return
  ev.preventDefault()
  onSend()
}

/** user 气泡视频地址:当次上传附件用 src;工作区视频由 path 确定性推流地址
 * (历史重载同路径同源);推不出(工作区外绝对路径)回退路径 chip。 */

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
  scrollToBottom()
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

/** 恢复条「刷新进度」:立即拉一次 events 补齐(平时 5s 自动轮询)。 */
const refreshing = ref(false)
async function onRefreshProgress() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await agent.refreshProgress()
  } finally {
    refreshing.value = false
  }
}

async function onRetry() {
  await agent.retry()
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

async function onNewSession() {
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
    // 续上当前工作区的最近会话(ownSessions 已按 lastActiveAt 倒序);
    // 当前工作区没有历史会话时新建,避免把旧工作区会话绑到新工作区
    const latest = ownSessions.value[0]
    if (latest) await agent.selectSession(latest.id)
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
  if (tickTimer !== null) clearInterval(tickTimer)
  if (copiedTimer !== null) clearTimeout(copiedTimer)
  agent.stop() // 离开页面中断在途流
  clearPending() // 释放全部 objectURL
})
</script>

<template>
  <div class="chat-page">
    <!-- 历史会话栏:按工作区分组(当前工作区组最上恒展开;其他工作区/未分组组
         默认折叠,caret 展开;悬停组标题显示完整路径);title + 相对时间,
         点击切换(跨工作区先切工作区),悬停出删除(确认后 optimistic 移除) -->
    <aside class="session-col">
      <div class="session-head">
        <UiIcon name="chat" :size="14" />
        <span class="session-head-title">历史会话</span>
        <span class="session-spacer" />
        <n-button size="small" :disabled="busy" @click="onNewSession">新建</n-button>
      </div>
      <n-scrollbar class="session-scroll">
        <div v-if="!sortedSessions.length" class="session-empty">暂无历史会话</div>
        <div v-else-if="!ownSessions.length" class="session-empty">当前工作区暂无会话</div>
        <template v-for="g in sessionGroups" :key="g.key">
          <!-- 其他工作区/未分组组标题:caret 折叠(同 TreeNode 模式),悬停显示完整路径 -->
          <button
            v-if="g.collapsible"
            class="session-group-head"
            :title="g.title ?? g.label"
            @click="toggleGroup(g.key)"
          >
            <span class="session-group-caret" :class="{ open: expandedGroups.has(g.key) }">
              ▸
            </span>
            <span class="session-group-label">{{ g.label }}</span>
            <span class="session-group-count">{{ g.items.length }}</span>
          </button>
          <template v-if="!g.collapsible || expandedGroups.has(g.key)">
            <div
              v-for="s in g.items"
              :key="s.id"
              class="session-item"
              :class="{ active: s.id === agent.sessionId }"
              tabindex="0"
              @click="onSelect(s)"
              @keydown.enter="onSelect(s)"
            >
              <div class="session-item-title" :title="sessionTitle(s)">{{ sessionTitle(s) }}</div>
              <div class="session-item-meta">
                <span class="session-item-meta-left">
                  <span class="session-item-time">{{ relTime(s.lastActiveAt) }}</span>
                </span>
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
          </template>
        </template>
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
      <div v-show="dragOver" class="drop-overlay">松开鼠标,将图片或视频添加为附件</div>

      <!-- 顶条:标题 + 状态 chip -->
      <div class="chat-bar">
        <UiIcon name="chat" :size="14" />
        <span class="chat-title">对话</span>
        <span class="status-chip" :class="`st-${agent.status}`">{{ statusLabel }}</span>
        <span class="chat-spacer" />
      </div>

      <!-- 时间线 -->
      <n-scrollbar ref="scrollbar" class="chat-scroll">
        <div class="chat-scroll-inner">
          <div v-if="!agent.entries.length" class="chat-empty">
            输入问题或检测指令,如:检测这段视频的交通事件
          </div>
          <template v-for="e in agent.entries" :key="e.id">
            <ChatEntryUser
              v-if="e.kind === 'user'"
              :entry="e"
              :copied="copiedKey === e.id"
              :busy="busy"
              :video-src="workspaceVideoSrc(e.videoPath, e.videoSrc, ws.path)"
              :time="fmtTime(e.at)"
              @copy="onCopy(e.id, $event)"
              @recall="onRecall(e)"
              @preview="openPreview"
            />
            <ChatEntryAssistant
              v-else-if="e.kind === 'assistant'"
              :entry="e"
              :copied="copiedKey === e.id"
              :streaming="streamingEntry?.id === e.id"
              :think-open="thinkOpen.has(e.id)"
              :time="fmtTime(e.at)"
              @copy="onCopy(e.id, $event)"
              @toggle-think="toggle(thinkOpen, e.id)"
            />
            <ChatEntryTool
              v-else-if="e.kind === 'tool'"
              :entry="e"
              :tool-open="toolOpen.has(e.id)"
              :sub-think-open="subThinkOpen"
              @toggle-tool="toggle(toolOpen, e.id)"
              @toggle-sub-think="toggle(subThinkOpen, $event)"
              @preview="openPreview"
            />
            <ChatEntryApproval v-else-if="e.kind === 'approval'" :entry="e" />
            <ChatEntrySystem v-else-if="e.kind === 'system'" :entry="e" />
            <ChatEntryDetection
              v-else-if="e.kind === 'detection'"
              :entry="e"
              @preview="openPreview"
            />
          </template>
          <!-- Turn 级 loading:对话进行中常驻一行,超过 15s 追加秒表(仅 status 驱动,step 间不闪烁) -->
          <div v-if="turnActive" class="turn-loading">
            <span class="turn-loading-dot" />
            <span>分析中…</span>
            <span v-if="turnElapsedSec >= 15" class="turn-loading-time">
              {{ turnElapsedSec }} 秒
            </span>
          </div>
        </div>
      </n-scrollbar>

      <!-- 恢复条:刷新/断网后服务端轮次仍在跑(本地无 SSE 流),5s 轮询补齐,可手动刷新 -->
      <div v-if="agent.recovering" class="resume-bar">
        <span class="resume-text">分析仍在进行中,每 5 秒自动补齐进度</span>
        <n-button size="small" :disabled="refreshing" @click="onRefreshProgress">
          {{ refreshing ? '刷新中…' : '刷新进度' }}
        </n-button>
      </div>

      <!-- 失败条:错误信息 + 重试 -->
      <div v-if="agent.status === 'failed'" class="fail-bar">
        <span class="fail-text" :title="agent.error ?? ''">{{ agent.error || '运行失败' }}</span>
        <n-button size="small" type="primary" @click="onRetry">
          <template #icon><UiIcon name="retry" :size="12" /></template>
          重试
        </n-button>
      </div>

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

/* 工作区分组标题:可折叠(caret 同 TreeNode:▸ 展开时旋转 90°),弱化处理 */
.session-group-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: var(--space-sm) var(--space-md) var(--space-xs);
  border: none;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-2);
  font-size: var(--text-xs);
  color: var(--color-text2);
  cursor: pointer;
  text-align: left;
}

.session-group-head:hover {
  color: var(--color-text);
}

.session-group-head:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: -2px;
}

.session-group-caret {
  display: inline-block;
  width: 12px;
  flex: 0 0 12px;
  text-align: center;
  font-size: 10px;
  transition: transform var(--dur-fast) var(--ease-out);
}

.session-group-caret.open {
  transform: rotate(90deg);
}

.session-group-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-group-count {
  margin-left: auto;
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

.session-item-meta-left {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
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

/* ---- Turn 级 loading(时间线底部常驻行) ---- */
.turn-loading {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-xs) 0;
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.turn-loading-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-blue);
  animation: turn-pulse 1.2s ease-in-out infinite;
}

@keyframes turn-pulse {
  0%,
  100% {
    opacity: 0.3;
  }
  50% {
    opacity: 1;
  }
}

.turn-loading-time {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}



/* ---- 恢复条(断连恢复:服务端轮次仍在跑,轮询补齐) ---- */
.resume-bar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-xs) var(--space-lg);
  border-top: 1px solid var(--color-border);
  background: var(--color-blue-soft);
}

.resume-text {
  flex: 1;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--color-blue);
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

/* ---- 权限模式选择器(触发钮 + 弹层菜单) ---- */
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
  font-family: var(--font-mono);
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
