<script setup lang="ts">
/** 统一对话整卡(问答 + 检测,后端统一走 /api/agent/*):
 * 左侧历史会话栏(按工作区分组的卡片纵列:每张卡片 = 卡片头(工作区名 + 会话数,
 * 像素字体)+ 会话条目列表;当前工作区卡置顶恒展开,其他工作区/未分组卡默认折叠;
 * 条目 title + 相对时间 / 点击切换(跨工作区先切工作区)重建时间线 /
 * 删除(optimistic,点击即删)/ 新建(运行中也可点:断连不杀服务端轮次,
 * 切回时恢复轮询续跑))+ 右侧对话卡:顶条(状态 chip)/ 时间线(user 气泡(图片附件
 * + 视频预览或路径 chip)/ assistant 流式气泡(思考折叠(运行中末行横向跟随/
 * 结束后首行)与正文 markdown(流式期间冻结已完成块,结束后一次性完整渲染,
 * 见 components/chat/MdStream)仅纯问答/无面板轮次——有面板承接的轮次(进行中
 * 或有 detection)思考与正文分别改由链路面板的思考/说明节点呈现,气泡对应隐藏;
 * submit_detection 之后的收尾正文在面板区间外,仍作普通气泡跟在检测卡后)/
 * 消息底部行(HH:MM + hover/focus-within 显现的复制
 * (成功变 ✓ 一秒);user 另有撤回,调 recall API 从时间线移除该条及其后全部,
 * 进行中禁用)/ 审批(approval_request 到达时 composer 输入区整体替换为审批面板
 * (拒绝/批准一次/本会话都批准),时间线里的审批卡只读留档;历史未决显示「已失效」)/
 * 检测结果卡(11 位编码等宽高亮 + 检出事件(逐事件标注图进画廊,无框/画框失败显示
 * 降级小字)+ markdown 报告))/ 失败条(错误 + 重试)/
 * 恢复条(刷新/断网后服务端轮次仍在跑:常驻「分析仍在进行中」,5s 自动轮询补齐)/
 * 工具调用不在时间线单独渲染:链路节点即工具条目(实时态底部生长的分析链路树与
 * 检测卡内冻结链路共用 ChatAnalysisFlow,节点默认折叠 label+耗时+状态,点击展开
 * ChatToolDetail 明细(tool_call 参数行 + 结果文本/图片/子代理时间线/取证视频),
 * assistant 思考与正文按时间序交错插为思考/说明节点(思考折叠一句话摘要,说明
 * 折叠前两行预览;展开全文/ markdown + 复制);展开态按条目 id 会话内记忆,面板
 * 「全部展开/折叠」同时作用于工具、思考与说明节点;纯问答轮次无工具节点不显示
 * 面板)。
 * composer 圆角盒(三行:附件预览行(图片缩略图 +
 * 视频块,视频同一时刻最多一个,可移除)/ 无边框 textarea(自动增高)/ 底部功能行(左:「+」
 * 上传图片或视频 + 权限模式选择器(逐条确认/自动通过/完全自主);右:压缩按钮 + 上下文圆环
 * (点击弹层:百分比 + 总量 + 压缩按钮)+ 发送/停止);图片粘贴/选择/拖拽 ≤4 张走 dataURL,
 * 视频粘贴/选择/拖拽走 /api/agent/uploads 落盘拿 path;Enter 发送 / Shift+Enter 换行,
 * 输入法合成态(isComposing)中的 Enter 是选词上屏,不发送)。
 * 进行中输入框不禁用:发送即 /steer 插话(气泡带「已插话」标记),停止走 /cancel 显式终止。
 * 工作区视频(无 src)气泡内按 path 确定性推 /api/workspace/stream 小播放器预览。
 * 对话进行中时间线底部常驻一行「分析中…」,超过 15s 追加秒表(从本轮 user 条目
 * 的 at 起算,切出再切回不重计)。
 * 图片画廊:点击气泡图放大,左右切换(按钮/键盘 ←→)。
 * 状态在 stores/agentchat.ts,组件只接线。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { NButton, NInput, NModal, NPopover, NScrollbar, useMessage } from 'naive-ui'
import { useAgentChatStore } from '../stores/agentchat'
import type { AgentAccess, AgentApprovalEntry, AgentMode, AgentSessionInfo, AgentUserEntry, DetectionPayload } from '../stores/agentchat'
import { useWorkspaceStore } from '../stores/workspace'
import { ApiError } from '../api/client'
import { mdToHtml } from '../utils/markdown'
import { groupSessionsByWorkspace } from '../utils/sessionGroups'
import { copyText, lastUserEntryAt, shouldSendOnEnter, timelineEntries, toolLabel, toolRoundAssistantIds, toolRoundAssistantTextIds, toolRoundImages, workspaceVideoSrc } from '../utils/chatDisplay'
import { buildAnalysisFlow } from '../utils/analysisFlow'
import type { AnalysisFlow } from '../utils/analysisFlow'
import UiIcon from '../components/UiIcon.vue'
import ContextRing from '../components/chat/ContextRing.vue'
import ChatEntryUser from '../components/chat/ChatEntryUser.vue'
import ChatEntryAssistant from '../components/chat/ChatEntryAssistant.vue'
import ChatEntryApproval from '../components/chat/ChatEntryApproval.vue'
import ChatEntrySystem from '../components/chat/ChatEntrySystem.vue'
import ChatEntryDetection from '../components/chat/ChatEntryDetection.vue'
import ChatAnalysisFlow from '../components/chat/ChatAnalysisFlow.vue'
import ChatSessionBar from '../components/chat/ChatSessionBar.vue'
import ChatGallery from '../components/chat/ChatGallery.vue'
import ChatComposer from '../components/chat/ChatComposer.vue'

const agent = useAgentChatStore()
const ws = useWorkspaceStore()
const message = useMessage()


/** 图片画廊子组件引用:时间线/检测卡的 preview 事件经此打开画廊。 */
const galleryRef = ref<InstanceType<typeof ChatGallery> | null>(null)
function openPreview(u: string) {
  galleryRef.value?.openPreview(u)
}

/** composer 子组件引用:拖拽落文件的暂存入口与离开页面时的 objectURL 释放。 */
const composerRef = ref<InstanceType<typeof ChatComposer> | null>(null)

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

/* 当前工作区组会话(进入页面续接最近会话用;分组渲染在 ChatSessionBar)。 */
const ownSessions = computed(
  () =>
    groupSessionsByWorkspace(agent.sessions, ws.path).find((g) => g.key === 'own')?.items ?? [],
)

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

/* ---- 折叠状态:思考过程按条目 id 标记,默认收起;
 * id 随条目全局唯一且随会话重建,切换/新建会话后旧 id 自然失效(不会串到新
 * 会话的条目上,无需手工重置;撤回/换会话后残留 id 无人引用)。 ---- */
const thinkOpen = reactive(new Set<string>())
function toggle<T>(set: Set<T>, key: T) {
  if (set.has(key)) set.delete(key)
  else set.add(key)
}

/* ---- 链路节点展开态(链路节点即工具条目):按工具条目 id 记忆,会话内共享
 * 同一份(实时态与各检测卡冻结态);折叠只记 id,明细在节点展开时渲染。 ---- */
const toolNodesOpen = reactive(new Set<string>())
function toggleToolNode(id: string) {
  toggle(toolNodesOpen, id)
}
/** 全部展开/折叠(面板工具行):对链路内全部工具节点批量置开/关。 */
function setAllToolNodes(ids: string[], open: boolean) {
  for (const id of ids) {
    if (open) toolNodesOpen.add(id)
    else toolNodesOpen.delete(id)
  }
}

/** 链路思考节点展开态:与气泡思考折叠共用同一份 thinkOpen(都按 assistant 条目
 * id 记忆)——有面板承接的轮次气泡不渲染 thinking,同一 id 只会出现在其中一处,
 * 不会互相串扰;「全部展开/折叠」与工具节点一并批量动作。 */
function toggleThinkNode(id: string) {
  toggle(thinkOpen, id)
}

function setAllThinkNodes(ids: string[], open: boolean) {
  for (const id of ids) {
    if (open) thinkOpen.add(id)
    else thinkOpen.delete(id)
  }
}

/** 链路说明节点展开态(按 assistant 条目 id,与思考节点同机制、各自独立;
 * 「全部展开/折叠」与工具、思考节点一并批量动作)。 */
const textOpen = reactive(new Set<string>())
function toggleTextNode(id: string) {
  toggle(textOpen, id)
}

function setAllTextNodes(ids: string[], open: boolean) {
  for (const id of ids) {
    if (open) textOpen.add(id)
    else textOpen.delete(id)
  }
}

/** 气泡 thinking 应隐藏的 assistant 条目(utils/chatDisplay 纯函数,与链路区间
 * 边界同口径):仅当该轮思考有链路面板承载(进行中或有 detection)时才隐藏,
 * 否则气泡保持原样。awaiting_approval 也算进行中——审批等待期间轮次未结束,
 * 否则 manual 模式下每次审批等待气泡都会重现造成与面板重复。 */
const hideThinkIds = computed(() =>
  toolRoundAssistantIds(agent.entries, {
    live: ['running', 'connecting', 'awaiting_approval'].includes(agent.status),
  }),
)

/** 气泡正文应隐藏的 assistant 条目(口径同上):有面板承接的轮次正文改由链路
 * 「说明」节点呈现;submit_detection 之后的收尾文本(detection 条目之后)不隐藏,
 * 仍作普通气泡跟在检测卡后。 */
const hideTextIds = computed(() =>
  toolRoundAssistantTextIds(agent.entries, {
    live: ['running', 'connecting', 'awaiting_approval'].includes(agent.status),
  }),
)

/** 时间线渲染条目:tool 条目不单独渲染(链路节点即工具条目)。 */
const timeline = computed(() => timelineEntries(agent.entries))

/** 正常问答轮次(无 detection)工具产物图:轮内最后一条 assistant 条目 id →
 * 该轮产物图片(与 hideThink/hideText 同口径的轮次边界);该气泡下方渲染缩略图条,
 * 点击进画廊。检测轮次与纯文本轮不出条目;历史会话恢复后 tool.images 由
 * store 映射保留,同样命中。 */
const roundImageStrips = computed(() => toolRoundImages(agent.entries))

/* ---- 流式渲染态:最后一个 assistant 条目在对话进行中走增量渲染;
 * 思考折叠行仅在「思考仍在流入」(该条目还没有正文)时取末行并横向跟随 ---- */
const streamingEntry = computed(() => {
  if (!busy.value) return null
  const last = agent.entries[agent.entries.length - 1]
  return last?.kind === 'assistant' ? last : null
})


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
  composerRef.value?.stageFiles(files).then((ok) => {
    if (ok) message.success('已添加附件')
  })
}

/** 卡片外区域拖放文件时阻止浏览器默认的导航打开行为。 */
function preventWindowDrop(ev: Event) {
  ev.preventDefault()
}
/* ---- 滚动:不跟随流式滚动,仅进入页面/切换会话/自己发送时主动滚底一次 ---- */
const scrollbar = ref<InstanceType<typeof NScrollbar> | null>(null)
function scrollToBottom() {
  scrollbar.value?.scrollTo({ top: Number.MAX_SAFE_INTEGER })
}

/* ---- Turn 级 loading:对话进行中(connecting/running,等审批不算——composer 已被
 * 审批面板接管)时间线底部常驻一行「分析中…」,超过 15s 追加秒表。
 * 只由 status 驱动,step 之间状态不变,行不闪烁。
 * 秒表起点 = 本轮真实起点(最后一条 user 条目的 at):切出再切回正在分析的会话
 * (恢复轮询接上)不重计;user 条目缺 at(旧数据)回退 turnActive 翻真时刻。 ---- */
const turnActive = computed(
  () => agent.status === 'connecting' || agent.status === 'running',
)
const activeSince = ref<number | null>(null)
const nowTick = ref(0)
let tickTimer: ReturnType<typeof setInterval> | null = null
watch(turnActive, (active) => {
  if (active) {
    if (activeSince.value === null) activeSince.value = Date.now()
    nowTick.value = Date.now()
    if (tickTimer === null) {
      tickTimer = setInterval(() => {
        nowTick.value = Date.now()
      }, 1000)
    }
  } else {
    activeSince.value = null
    if (tickTimer !== null) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  }
})
/** 已进行秒数(超过 15s 才在时间线 loading 行展示)。
 * 起点取「最后一条 user 条目」与「ActiveSession 实例创建时刻」的较晚者:
 * 恢复合并可能把几天前的旧 user 条目排在最后,轮次实际是新发起的,
 * 秒表从旧条目起算会显示几十万秒的错误值(实测 229907s)。 */
const turnElapsedSec = computed(() => {
  if (!turnActive.value) return 0
  const fromEntry = lastUserEntryAt(agent.entries)
  const start =
    fromEntry !== null && activeSince.value !== null
      ? Math.max(fromEntry, activeSince.value)
      : (fromEntry ?? activeSince.value)
  if (start === null) return 0
  return Math.max(0, Math.floor((nowTick.value - start) / 1000))
})

/* ---- 分析链路流程图(W6):实时态 + 冻结态共用 ChatAnalysisFlow ----
 * 实时态:分析中(connecting/running)在时间线底部生长完整树,当前步脉冲;
 * 还没有任何工具节点时不渲染。冻结态:每个检测卡按其条目 id 预推导
 * (区间 = 最近一条 user 之后至该检测),卡片内默认折叠一行摘要。 */
const liveFlow = computed<AnalysisFlow | null>(() => {
  if (!turnActive.value) return null
  const f = buildAnalysisFlow(agent.entries, null)
  return f.phases.some((p) => p.nodes.length > 0) ? f : null
})

const detectionFlows = computed(() => {
  const m = new Map<string, AnalysisFlow>()
  for (const e of agent.entries) {
    if (e.kind === 'detection') m.set(e.id, buildAnalysisFlow(agent.entries, e.id))
  }
  return m
})


/** user 气泡视频地址:当次上传附件用 src;工作区视频由 path 确定性推流地址
 * (历史重载同路径同源);推不出(工作区外绝对路径)回退路径 chip。 */


/** 恢复条:只提示 + 依赖 5s 自动轮询补齐(手动刷新按钮已移除:恢复轮询与
 * 自动轮询并发拉取曾把同一段落盘条目重复并入时间线,去重修复后不再需要手动入口)。 */

async function onRetry() {
  await agent.retry()
}


async function onNewSession() {
  await agent.newSession()
}

onMounted(async () => {
  window.addEventListener('dragover', preventWindowDrop)
  window.addEventListener('drop', preventWindowDrop)
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
  if (tickTimer !== null) clearInterval(tickTimer)
  if (copiedTimer !== null) clearTimeout(copiedTimer)
  agent.stop() // 离开页面中断在途流
  composerRef.value?.clearPending() // 释放全部 objectURL
})
</script>

<template>
  <div class="chat-page">
    <!-- 历史会话栏:工作区分组渲染为独立卡片纵列(当前工作区卡最上恒展开;其他
         工作区/未分组卡默认折叠,点击卡头展开,悬停卡头显示完整路径);条目
         title + 相对时间(最近活跃)· 绝对发起时间(createdAt),点击切换(跨工作区
         先切工作区),悬停出删除(trash 图标,点击即删,store 乐观移除 + 失败回滚) -->
    <!-- 历史会话栏(子组件内聚分组/重命名/删除;选中后滚动时间线到底) -->
    <ChatSessionBar @select="scrollToBottom" />

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
          <template v-for="e in timeline" :key="e.id">
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
              :hide-think="hideThinkIds.has(e.id)"
              :hide-text="hideTextIds.has(e.id)"
              :time="fmtTime(e.at)"
              @copy="onCopy(e.id, $event)"
              @toggle-think="toggle(thinkOpen, e.id)"
            />
            <ChatEntryApproval v-else-if="e.kind === 'approval'" :entry="e" />
            <ChatEntrySystem v-else-if="e.kind === 'system'" :entry="e" />
            <ChatEntryDetection
              v-else-if="e.kind === 'detection'"
              :entry="e"
              :flow="detectionFlows.get(e.id)"
              :expanded-tools="toolNodesOpen"
              :expanded-thinks="thinkOpen"
              :expanded-texts="textOpen"
              @toggle-tool="toggleToolNode"
              @toggle-all-tools="setAllToolNodes"
              @toggle-think="toggleThinkNode"
              @toggle-all-thinks="setAllThinkNodes"
              @toggle-text="toggleTextNode"
              @toggle-all-texts="setAllTextNodes"
              @preview="openPreview"
            />
            <!-- 正常问答轮次工具产物图(无 detection 轮):缩略图条贴在该轮最后一条
                 assistant 气泡下,点击进画廊;面板承接的轮次(进行中/hideText)不重复渲染。
                 注意置于整条 v-if/v-else-if 链之后,不插入链条中间 -->
            <div
              v-if="
                e.kind === 'assistant' &&
                !hideTextIds.has(e.id) &&
                roundImageStrips.get(e.id)?.length
              "
              class="round-images"
            >
              <img
                v-for="(u, i) in roundImageStrips.get(e.id)"
                :key="i"
                class="round-thumb"
                :src="u"
                alt=""
                @click="openPreview(u)"
              />
            </div>
          </template>
          <!-- 分析链路实时态:分析中随 entries 生长的完整阶段树(W6,当前步脉冲);
               节点即工具条目,思考/说明按时间序插为思考/说明节点,展开态与检测卡
               冻结链路共享同一份记忆 -->
          <ChatAnalysisFlow
            v-if="liveFlow"
            :flow="liveFlow"
            realtime
            :expanded-tools="toolNodesOpen"
            :expanded-thinks="thinkOpen"
            :expanded-texts="textOpen"
            @toggle-tool="toggleToolNode"
            @toggle-all-tools="setAllToolNodes"
            @toggle-think="toggleThinkNode"
            @toggle-all-thinks="setAllThinkNodes"
            @toggle-text="toggleTextNode"
            @toggle-all-texts="setAllTextNodes"
          />
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

      <!-- 恢复条:刷新/断网后服务端轮次仍在跑(本地无 SSE 流),5s 自动轮询补齐 -->
      <div v-if="agent.recovering" class="resume-bar">
        <span class="resume-text">分析仍在进行中,每 5 秒自动补齐进度</span>
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
      <!-- composer(子组件内聚附件/审批面板/模式/压缩;发送后滚底,拖拽经 ref 暂存) -->
      <ChatComposer ref="composerRef" @sent="scrollToBottom" />
    </section>

    <!-- 图片画廊:单图放大 + 左右切换(按钮/键盘,子组件内聚清单与键位) -->
    <ChatGallery ref="galleryRef" />
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

/* 按钮(design.md §2 骨架角色):Naive 按钮字体走主题,逐类覆盖为像素体 */
.chat-page :deep(.n-button) {
  font-family: var(--font-pixel);
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
  font-family: var(--font-pixel); /* 操作提示覆盖层 → 像素(空态/骨架提示) */
  pointer-events: none;
}

/* ---- 顶条(卡片头 → 像素) ---- */
.chat-bar {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-lg);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text2);
  font-family: var(--font-pixel);
}

.chat-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
}

.chat-spacer {
  flex: 1;
}

/* ---- 状态 chip:四态(+运行中)配色全部走 token(chips → 像素) ---- */
.status-chip {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: 600;
  font-family: var(--font-pixel);
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
  font-family: var(--font-pixel); /* 空态 → 像素 */
}


/* ---- Turn 级 loading(时间线底部常驻行;状态行 → 像素,秒表数值 → 等宽) ---- */
.turn-loading {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-xs) 0;
  color: var(--color-text2);
  font-size: var(--text-sm);
  font-family: var(--font-pixel);
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

/* ---- 正常问答轮次工具产物图缩略条(贴该轮最后一条 assistant 气泡下) ---- */
.round-images {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin: var(--space-xs) 0 var(--space-sm) calc(36px + var(--space-sm));
}

.round-thumb {
  width: 64px;
  height: 48px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}
</style>
