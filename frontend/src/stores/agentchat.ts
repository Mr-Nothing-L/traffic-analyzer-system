/** 统一对话(问答 + 检测):会话列表/切换/删除 + 时间线条目 + SSE 流式轮次。
 * 后端契约(FastAPI /api/agent/* 代理 → Node agent 服务,workspaceDir 由后端注入):
 * POST   /api/agent/sessions({mode}) → {sessionId};
 * GET    /api/agent/sessions → {sessions:[{id,workspaceDir,mode,title,createdAt,lastActiveAt,usedTokens?}]};
 * POST   /api/agent/sessions/{id}/mode({mode:'manual'|'auto'|'yolo'}) → {status:'ok'}(就地切换权限模式);
 * GET    /api/agent/sessions/{id}/history → {entries:[...]}(五类条目,见 mapHistoryEntry);
 * POST   /api/agent/sessions/{id}/compact → {status,compacted,beforeTokens?,afterTokens?}(进行中 409);
 * DELETE /api/agent/sessions/{id} → {status:'ok'};
 * POST   /api/agent/chat({sessionId,input,videoPath?,images?(dataURL,≤4)}) → SSE
 *   (每事件一行 'data: {json}\n\n',事件:text_delta/think_delta/tool_call_start/
 *   tool_result/step_done/approval_request/detection/context_usage/compaction/done;
 *   done {reason, error?, truncated?},truncated:true 表示输出达到 token 上限被截断);
 * POST   /api/agent/approval({requestId,decision,scope?}) → {status:'ok'};
 * POST   /api/agent/uploads(multipart file) → {path,name,size,contentType}(视频落工作区,
 *   返回 path 作 videoPath;GET /api/agent/uploads/{name} 供 <video> 预览);
 * POST   /api/agent/sessions/{id}/recall({entryIndex}) → {status:'ok'}(409=对话进行中;
 *   entryIndex 为后端持久化条目下标,不含本地 system 条目);
 * GET    /api/agent/sessions/{id}/events?fromSeq=N → {events:[{seq,entry}],inProgress}
 *   (断连续传:已落盘条目中 seq>N 的部分;inProgress 表示服务端轮次仍在跑——
 *   断连不再杀轮次,刷新后据此 5s 轮询补齐,不做实时流重连);
 * POST   /api/agent/sessions/{id}/cancel → 显式终止进行中轮次(409 no_active_turn);
 * POST   /api/agent/sessions/{id}/steer({input,videoPath?,images?}) → 进行中插话,
 *   下一 step 边界生效(409 no_active_turn 回退 /chat);生效时 SSE 收到
 *   {type:'steer',text,images,videoPath?,seq} 事件(本地乐观插入,按 text 去重)。
 * fetch+ReadableStream 按 \n\n 分块解析 data: 行,组件只接线,状态全部在这里。 */
import { markRaw, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '../api/client'
import { useWorkspaceStore } from './workspace'

/** 权限模式三档:逐条确认 manual / 自动通过 auto / 完全自主 yolo。 */
export type AgentMode = 'manual' | 'auto' | 'yolo'

/** idle 待开始 / connecting 连接中(建会话、切会话或流发起)/ running 运行中 /
 * awaiting_approval 等待审批 / done 已完成 / failed 失败(可重试)。 */
export type AgentStatus =
  | 'idle'
  | 'connecting'
  | 'running'
  | 'awaiting_approval'
  | 'done'
  | 'failed'

/** approval_request 事件里的资源访问声明(与 agent 端 ToolAccesses 对齐)。 */
export interface AgentAccess {
  kind: 'file' | 'all'
  operation?: 'read' | 'write' | 'readwrite' | 'search'
  path?: string
  recursive?: boolean
}

/** GET /sessions 的列表项(createdAt/lastActiveAt 为 epoch ms)。 */
export interface AgentSessionInfo {
  id: string
  workspaceDir?: string
  mode?: string
  title?: string
  createdAt?: number
  lastActiveAt?: number
  /** 最近一次已知上下文占用(token,有真实 usage 才返回)。 */
  usedTokens?: number
}

export interface AgentUserEntry {
  kind: 'user'
  text: string
  videoPath?: string
  /** 上传视频的预览地址(/api/agent/uploads/{name});仅本次会话内有效,
   * 历史重载由 videoPath 确定性推 /api/workspace/stream(见 utils/chatDisplay)。 */
  videoSrc?: string
  /** dataURL 图片附件(发送时随消息上传,历史原样返回)。 */
  images?: string[]
  /** 发送时间(epoch ms),用于气泡 HH:MM。 */
  at?: number
  /** 进行中插话(steer)标记:仅本地流式期间存在,历史重载不恢复。 */
  steered?: boolean
}

export interface AgentAssistantEntry {
  kind: 'assistant'
  text: string
  think: string
  /** 气泡创建时间(epoch ms)。 */
  at?: number
}

/** spawn_subagent 工具条目下挂的子代理迷你时间线条目(subagent_event 流式期间聚合,
 * 不落盘不恢复):think=think_delta 聚合的思考;text=text_delta 聚合的子结论文本;
 * tool=子代理工具调用(tool_call_start 压入,tool_result 按 id 置 done)。 */
export type AgentSubItem =
  | { kind: 'think'; text: string }
  | { kind: 'text'; text: string }
  | { kind: 'tool'; id: string; name: string; args: string; done: boolean }

export interface AgentToolEntry {
  kind: 'tool'
  /** tool_call_start 的 call.id,tool_result 按它回填。 */
  id: string
  name: string
  /** call.arguments 原文(JSON 字符串)。 */
  args: string
  result: string
  /** 工具输出中的图片(dataURL;extract_frames/draw_boxes 等返回的 image ContentPart)。 */
  images: string[]
  /** 工具输出含 video part(load_video 整段视频直传);只显示静态提示,不做播放器。 */
  hasVideo: boolean
  /** 子代理迷你时间线(subagent_event 按 toolCallId 挂到本条目)。 */
  children: AgentSubItem[]
  isError: boolean
  done: boolean
}

export interface AgentApprovalEntry {
  kind: 'approval'
  requestId: string
  toolName: string
  approvalRule: string
  description?: string
  accesses: AgentAccess[]
  /** 已回执后记录:'approved' | 'rejected' | 'approved_session' | 'cancelled'(历史)。 */
  decision?: string
  /** 历史载入的未决审批:后端不再接受回执,UI 显示「已失效」,不渲染按钮。 */
  stale?: boolean
}

/** detection 事件的 data:submit_detection 的结构化 payload(解析失败时为原始字符串)。 */
export interface DetectionPayload {
  binary_encoding?: string
  normal?: boolean
  events?: Array<{
    event_id: number
    detected: boolean
    reasoning: string
    evidence_frames: string[]
    /** 逐事件标注图(jpeg dataURL,submit_detection 服务端生成;无框/画框失败时缺省)。 */
    annotated_image?: string
  }>
  /** 标注降级元信息:annotation_not_provided=检出但未给定位框;annotation_missing=画框失败。 */
  meta?: {
    annotation_not_provided?: number[]
    annotation_missing?: number[]
  }
  report_markdown?: string
}

export interface AgentDetectionEntry {
  kind: 'detection'
  data: unknown
}

/** 系统提示条目(如自动压缩提示、输出截断警示),不进历史,仅流式期间插入时间线。
 * tone:'warn' 用警示色系(gold)渲染,缺省为中性灰。 */
export interface AgentSystemEntry {
  kind: 'system'
  text: string
  tone?: 'warn'
}

export type AgentEntry =
  | AgentUserEntry
  | AgentAssistantEntry
  | AgentToolEntry
  | AgentApprovalEntry
  | AgentDetectionEntry
  | AgentSystemEntry

/** 待发送的视频附件(同一时刻最多一个):
 * 上传得来:path=uploads 返回路径,src=/api/agent/uploads/{name} 供预览;
 * 工作区树「送入对话」:path=工作区相对路径,无 src(composer 显示图标块,
 * 气泡由 path 确定性推 /api/workspace/stream 小播放器预览)。 */
export interface PendingVideo {
  path: string
  name: string
  src?: string
}

/** send 的附件参数(图片 dataURL ≤4;视频二选一:工作区路径或上传后路径)。 */
export interface SendOptions {
  videoPath?: string
  videoSrc?: string
  images?: string[]
}

/** 非 2xx 统一成 ApiError:agent 服务错误为 {error:{code,message}},
 * FastAPI 代理/其他后端为 {detail},两种都认。 */
async function toApiError(res: Response): Promise<ApiError> {
  if (res.status === 401 && window.location.pathname !== '/login') {
    window.location.href = '/login'
  }
  let detail = res.statusText
  try {
    const body = (await res.json()) as {
      detail?: unknown
      error?: { message?: unknown }
    }
    if (body?.detail != null) detail = String(body.detail)
    else if (body?.error?.message != null) detail = String(body.error.message)
  } catch {
    // 非 JSON 错误响应,保留 statusText
  }
  return new ApiError(res.status, detail)
}

/** JSON POST 封装(错误体两种形态兼容;401 跳登录由 toApiError 处理)。 */
async function postJson(path: string, body: unknown): Promise<unknown> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, '网络错误,无法连接后端')
  }
  if (!res.ok) throw await toApiError(res)
  return res.json()
}

/** GET/DELETE 封装(与 postJson 同一套错误语义)。 */
async function reqJson(path: string, method: 'GET' | 'DELETE'): Promise<unknown> {
  let res: Response
  try {
    res = await fetch(path, { method })
  } catch {
    throw new ApiError(0, '网络错误,无法连接后端')
  }
  if (!res.ok) throw await toApiError(res)
  return res.json()
}

/** tool_result 的 output:string 原样;ContentPart[] 取 text 拼接;其他 JSON 化。 */
function formatToolOutput(output: unknown): string {
  if (typeof output === 'string') return output
  if (Array.isArray(output)) {
    const texts = output
      .map((p) =>
        p && typeof p === 'object' && 'text' in p ? String((p as { text: unknown }).text) : '',
      )
      .filter(Boolean)
    if (texts.length) return texts.join('\n')
  }
  try {
    return JSON.stringify(output, null, 2) ?? ''
  } catch {
    return String(output ?? '')
  }
}

/** tool_result 的 output 为 ContentPart[] 时,提取 image_url 部分的 url(dataURL)。
 * 兼容 kosong 的 camelCase(imageUrl)与 snake_case(image_url)两种键名。 */
function extractToolImages(output: unknown): string[] {
  if (!Array.isArray(output)) return []
  const urls: string[] = []
  for (const p of output) {
    if (!p || typeof p !== 'object') continue
    const part = p as {
      type?: unknown
      imageUrl?: { url?: unknown }
      image_url?: { url?: unknown }
    }
    if (part.type !== 'image_url') continue
    const url = part.imageUrl?.url ?? part.image_url?.url
    if (typeof url === 'string' && url) urls.push(url)
  }
  return urls
}

/** tool_result 的 output 是否含 video part(load_video 整段视频直传)。
 * 只作标记供 UI 显示静态提示;视频 dataURL 体积巨大,绝不当图渲染。 */
function hasToolVideo(output: unknown): boolean {
  if (!Array.isArray(output)) return false
  return output.some(
    (p) => p !== null && typeof p === 'object' && (p as { type?: unknown }).type === 'video_url',
  )
}

/** GET history 的条目 → 本地时间线条目;不认识/缺 kind 的条目丢弃。 */
function mapHistoryEntry(raw: unknown): AgentEntry | null {
  if (!raw || typeof raw !== 'object') return null
  const e = raw as Record<string, unknown>
  if (e.kind === 'user') {
    return {
      kind: 'user',
      text: String(e.text ?? ''),
      ...(Array.isArray(e.images) && e.images.length
        ? { images: e.images.map(String) }
        : {}),
      ...(e.videoPath != null ? { videoPath: String(e.videoPath) } : {}),
      ...(e.videoSrc != null ? { videoSrc: String(e.videoSrc) } : {}),
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  if (e.kind === 'assistant') {
    return {
      kind: 'assistant',
      text: String(e.text ?? ''),
      think: String(e.think ?? ''),
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  if (e.kind === 'tool') {
    return {
      kind: 'tool',
      id: String(e.toolCallId ?? ''),
      name: String(e.name ?? ''),
      args:
        typeof e.arguments === 'string' ? e.arguments : JSON.stringify(e.arguments ?? ''),
      result: formatToolOutput(e.output),
      images: extractToolImages(e.output),
      hasVideo: hasToolVideo(e.output),
      children: [],
      isError: e.isError === true,
      done: true,
    }
  }
  if (e.kind === 'approval') {
    const decision = e.decision != null ? String(e.decision) : undefined
    return {
      kind: 'approval',
      requestId: String(e.requestId ?? ''),
      toolName: String(e.toolName ?? ''),
      approvalRule: String(e.approvalRule ?? ''),
      ...(e.description != null ? { description: String(e.description) } : {}),
      accesses: [],
      // 历史里仍挂起的审批:后端已不回溯,标记失效(UI 显示「已失效」)
      ...(decision ? { decision } : { stale: true }),
    }
  }
  if (e.kind === 'detection') return { kind: 'detection', data: e.data }
  return null
}

export const useAgentChatStore = defineStore('agentchat', () => {
  const sessionId = ref<string | null>(null)
  const mode = ref<AgentMode>('manual')
  const status = ref<AgentStatus>('idle')
  const error = ref<string | null>(null)
  /** 时间线条目(user/assistant/工具/审批/检测结果),渲染顺序 = 到达顺序。 */
  const entries = ref<AgentEntry[]>([])
  /** 历史会话列表(GET /sessions),进入页面/新建/发送/删除后刷新。 */
  const sessions = ref<AgentSessionInfo[]>([])
  /** 上下文已用 token(context_usage 事件;无真实用量时为 null)。 */
  const usedTokens = ref<number | null>(null)
  /** 上下文窗口上限(默认 256k,以 context_usage 事件为准)。 */
  const maxTokens = ref(262144)
  /** 待发送视频附件(composer 预览行;发送后清空)。树组件「送入对话」也写它。 */
  const pendingVideo = ref<PendingVideo | null>(null)
  /** 恢复态:刷新/断网后服务端轮次仍在跑,本地无 SSE 流,靠 5s 轮询补齐。
   * UI 据此显示「分析仍在进行中」常驻条。 */
  const recovering = ref(false)

  function setPendingVideo(v: PendingVideo) {
    pendingVideo.value = v
  }

  function clearPendingVideo() {
    pendingVideo.value = null
  }

  let ctrl: AbortController | null = null
  /** 轮次代际:切换/新建会话会中断在途流并递增,旧 runTurn 的收尾不再写状态。 */
  let turnSeq = 0
  /** 选择代际:selectSession 的 history 请求在途期间,若发生更新的选择/
   * 新建会话/工作区切换,晚到的响应必须丢弃(stale write 会把已清空的旧
   * 工作区会话写回时间线)。 */
  let selectSeq = 0
  let lastInput = ''
  let lastVideoPath: string | undefined
  let lastImages: string[] | undefined
  /** 恢复轮询定时器(events 5s 轮询;recovering 期间存活)。 */
  let pollTimer: ReturnType<typeof setInterval> | null = null
  /** events 续传水位:已对齐的服务端落盘 seq(seq 从 1 起,等于已落盘条数)。 */
  let pollSeq = 0
  /** 乐观插入的 steer 文本多重集合:SSE steer 事件/轮询补齐的 user 条目
   * 按 text 消费一条,避免与本地乐观条目重复。 */
  let pendingSteers: string[] = []

  function stop() {
    ctrl?.abort()
    stopPolling()
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  /** 切换/新建会话前调用:中断在途流,并使其 AbortError 收尾失效(不得覆盖新状态)。
   * 同时收尾恢复态:停轮询、清 steer 去重账。 */
  function supersedeTurn() {
    turnSeq += 1
    ctrl?.abort()
    stopPolling()
    recovering.value = false
    pendingSteers = []
  }

  /** 上次输入快照清空(新建/切换会话后,失败重试不得把旧输入重放进新会话)。 */
  function resetLastTurn() {
    lastInput = ''
    lastVideoPath = undefined
    lastImages = undefined
  }

  /* ---- 工作区切换:后端在建会话时注入当前工作区作 workspaceDir,旧会话仍绑旧工作区,
   * 新工作区相对路径送入旧会话会解析失败。监听工作区路径变化:清当前会话态与 pendingVideo
   * (不删后端历史会话),下次发送/进入对话时按新工作区惰性建新会话。
   * 首次加载(prev 为 null)不算切换;selectSession 不动工作区路径,不会误触发。 */
  const wsStore = useWorkspaceStore()
  watch(
    () => wsStore.path,
    (next, prev) => {
      if (prev == null || next === prev) return
      supersedeTurn()
      selectSeq += 1 // 使在途的 selectSession 失效:旧工作区会话不得写回
      sessionId.value = null
      entries.value = []
      usedTokens.value = null
      pendingVideo.value = null
      error.value = null
      status.value = 'idle'
      resetLastTurn()
      // 切换端点已同步触发 agent server 恢复该工作区的磁盘历史(set_workspace
      // 返回时 restore 已完成),此处重拉列表让恢复的会话立即可见;
      // 失败不阻塞切换,仅留在控制台。
      void fetchSessions().catch((e) => console.warn('[agentchat] 工作区切换后刷新会话列表失败', e))
    },
  )

  /** 拉历史会话列表(失败抛错,调用方提示)。 */
  async function fetchSessions() {
    const r = (await reqJson('/api/agent/sessions', 'GET')) as {
      sessions?: AgentSessionInfo[]
    }
    sessions.value = Array.isArray(r.sessions) ? r.sessions : []
  }

  /* ---- 断连恢复:events 轮询(简化方案,不做实时流重连) ---- */

  /** 合并一批已落盘条目(带 seq):推进水位;user 条目命中乐观 steer 账时
   * 跳过(本地条目已在),其余 markRaw 压入时间线(落盘条目不再变更)。 */
  function mergePersistedEvents(events: Array<{ seq?: unknown; entry?: unknown }>) {
    for (const ev of events) {
      if (typeof ev.seq === 'number' && ev.seq > pollSeq) pollSeq = ev.seq
      const mapped = mapHistoryEntry(ev.entry)
      if (mapped === null) continue
      if (mapped.kind === 'user') {
        const i = pendingSteers.indexOf(mapped.text)
        if (i >= 0) {
          pendingSteers.splice(i, 1) // 本地乐观 steer 条目已在,去重跳过
          continue
        }
      }
      entries.value.push(markRaw(mapped))
    }
  }

  /** 拉一次 events 并合并;inProgress 变 false 时收尾恢复态(停轮询、置完成)。
   * 网络抖动静默,下轮重试;切换会话后的晚到响应整体丢弃。 */
  async function pollEvents(id: string) {
    if (sessionId.value !== id) {
      stopPolling()
      return
    }
    let r: { events?: Array<{ seq?: unknown; entry?: unknown }>; inProgress?: boolean }
    try {
      r = (await reqJson(
        `/api/agent/sessions/${id}/events?fromSeq=${pollSeq}`,
        'GET',
      )) as typeof r
    } catch {
      return // 网络抖动:下轮重试
    }
    if (sessionId.value !== id) return // 轮询在途期间已切换会话
    mergePersistedEvents(Array.isArray(r.events) ? r.events : [])
    if (r.inProgress !== true) {
      stopPolling()
      recovering.value = false
      if (status.value === 'running') status.value = 'done'
      await fetchSessions().catch(() => {}) // 标题/用量已变,刷新列表(失败不阻断)
    }
  }

  /** 启动 5s 轮询(先停旧定时器,幂等)。 */
  function startPolling(id: string) {
    stopPolling()
    pollTimer = setInterval(() => {
      void pollEvents(id)
    }, 5000)
  }

  /** 手动「刷新进度」:立即拉一次 events(恢复条按钮)。 */
  async function refreshProgress() {
    if (sessionId.value) await pollEvents(sessionId.value)
  }

  /** selectSession 收尾的恢复探测:以已加载条数为水位拉 events,
   * inProgress=true 则进入恢复态(status running + 常驻条 + 5s 轮询)。
   * 探测失败(网络/404)不阻塞会话切换。 */
  async function resumeFromServer(id: string, gen: number) {
    pollSeq = entries.value.length
    let r: { events?: Array<{ seq?: unknown; entry?: unknown }>; inProgress?: boolean }
    try {
      r = (await reqJson(
        `/api/agent/sessions/${id}/events?fromSeq=${pollSeq}`,
        'GET',
      )) as typeof r
    } catch {
      return
    }
    if (gen !== selectSeq || sessionId.value !== id) return // 探测在途期间已切换
    mergePersistedEvents(Array.isArray(r.events) ? r.events : [])
    if (r.inProgress === true) {
      recovering.value = true
      status.value = 'running'
      startPolling(id)
    }
  }

  /** 创建会话:workspaceDir 由后端代理注入,前端只传权限模式。 */
  async function createSession() {
    supersedeTurn()
    selectSeq += 1 // 新建取代任何在途的 selectSession
    status.value = 'connecting'
    error.value = null
    sessionId.value = null
    usedTokens.value = null
    resetLastTurn()
    try {
      const r = (await postJson('/api/agent/sessions', { mode: mode.value })) as {
        sessionId?: string
      }
      if (!r.sessionId) throw new ApiError(0, '后端未返回 sessionId')
      sessionId.value = r.sessionId
      status.value = 'idle'
      await fetchSessions().catch(() => {}) // 列表出现新会话(无标题)
    } catch (e) {
      status.value = 'failed'
      error.value = (e as Error).message
    }
  }

  /** 切换历史会话:拉 history 重建时间线;权限模式跟随会话属性。 */
  async function selectSession(id: string) {
    if (id === sessionId.value && entries.value.length) return
    supersedeTurn()
    const gen = ++selectSeq
    status.value = 'connecting'
    error.value = null
    resetLastTurn()
    try {
      const r = (await reqJson(`/api/agent/sessions/${id}/history`, 'GET')) as {
        entries?: unknown[]
      }
      // 在途期间发生了更新的选择/新建/工作区切换:丢弃晚到的历史
      if (gen !== selectSeq) return
      entries.value = (Array.isArray(r.entries) ? r.entries : [])
        .map((raw) => {
          const e = mapHistoryEntry(raw)
          // 历史条目落盘后不再变更:markRaw 跳过深响应式代理(工具结果/
          // 附件里的大 base64 字符串逐层 proxy 化开销可观);流式轮次新
          // 压入的条目不受影响,仍走正常响应式更新。
          return e === null ? null : markRaw(e)
        })
        .filter((e): e is AgentEntry => e !== null)
      sessionId.value = id
      const info = sessions.value.find((s) => s.id === id)
      if (info?.mode === 'manual' || info?.mode === 'auto' || info?.mode === 'yolo') {
        mode.value = info.mode
      }
      // 会话切换:沿用列表里的最近已知用量(没有则清空等下一次 context_usage)
      usedTokens.value = typeof info?.usedTokens === 'number' ? info.usedTokens : null
      status.value = 'idle'
      // 断连恢复探测:服务端轮次仍在跑则进入恢复态(轮询补齐)
      await resumeFromServer(id, gen)
    } catch (e) {
      status.value = 'failed'
      error.value = (e as Error).message
    }
  }

  /** 会话栏点击入口:会话绑其他工作区(workspaceDir 与当前不同)时先切工作区
   * (applyWorkspace → POST /api/workspace,等它返回——path 变更触发的上方 watch
   * 会在其 resolve 前清空会话态),成功后再 selectSession,避免选上的会话被清掉;
   * 切换失败(400 目录不存在/403 不在白名单)抛错,由调用方提示「工作区不可用」,
   * 不切换会话。当前工作区/无 workspaceDir 的旧会话直接 selectSession。 */
  async function openSession(id: string) {
    const dir = sessions.value.find((s) => s.id === id)?.workspaceDir
    if (dir && dir !== wsStore.path) await wsStore.applyWorkspace(dir)
    await selectSession(id)
  }

  /** 删除会话:列表先移除(optimistic),失败回滚重拉并抛错;
   * 删的是当前会话则优先切到当前工作区内最近的会话;只剩其他工作区的会话时走
   * openSession(先 applyWorkspace 再选);没有任何会话才新建。 */
  async function deleteSession(id: string) {
    const wasCurrent = sessionId.value === id
    sessions.value = sessions.value.filter((s) => s.id !== id)
    try {
      await reqJson(`/api/agent/sessions/${id}`, 'DELETE')
    } catch (e) {
      await fetchSessions().catch(() => {})
      throw e
    }
    if (wasCurrent) {
      stop()
      sessionId.value = null
      entries.value = []
      const latest = [...sessions.value].sort(
        (a, b) => (b.lastActiveAt ?? 0) - (a.lastActiveAt ?? 0),
      )
      // 与 openSession 同语义:无 workspaceDir 的旧会话视为当前工作区。其他
      // 工作区的会话直接 selectSession 会绕过 applyWorkspace,时间线加载了
      // 别的工作区的会话而 ws.path 仍是旧工作区,路径解析/视频预览全部错位。
      const current = latest.find((s) => !s.workspaceDir || s.workspaceDir === wsStore.path)
      if (current) await selectSession(current.id)
      else if (latest[0]) await openSession(latest[0].id)
      else await createSession()
    }
  }

  /** 切换权限模式:有会话走 POST /sessions/{id}/mode 就地切换,成功后更新本地 mode
   * 与列表项;无会话先记在 mode,建会话时带上。失败抛错(调用方提示),本地 mode 不变。 */
  async function setMode(m: AgentMode) {
    if (m === mode.value) return
    if (!sessionId.value) {
      mode.value = m
      return
    }
    await postJson(`/api/agent/sessions/${sessionId.value}/mode`, { mode: m })
    mode.value = m
    const info = sessions.value.find((s) => s.id === sessionId.value)
    if (info) info.mode = m
  }

  /** 新建会话(同模式):清空时间线重来。 */
  async function newSession() {
    entries.value = []
    await createSession()
  }

  /** 流式中追加文本/思考:最后一个条目是 assistant 就续写,否则开新气泡
   * (工具气泡会自然切断前后两段 assistant 文本)。 */
  function currentAssistant(): AgentAssistantEntry {
    const last = entries.value[entries.value.length - 1]
    if (last?.kind === 'assistant') return last
    const entry: AgentAssistantEntry = { kind: 'assistant', text: '', think: '', at: Date.now() }
    entries.value.push(entry)
    return entry
  }

  function handleEvent(ev: Record<string, unknown>) {
    if (ev.type === 'text_delta') {
      currentAssistant().text += String(ev.text ?? '')
    } else if (ev.type === 'think_delta') {
      currentAssistant().think += String(ev.text ?? '')
    } else if (ev.type === 'tool_call_start') {
      const call = ev.call as { id?: unknown; name?: unknown; arguments?: unknown } | undefined
      entries.value.push({
        kind: 'tool',
        id: String(call?.id ?? ''),
        name: String(call?.name ?? ''),
        args:
          typeof call?.arguments === 'string'
            ? call.arguments
            : JSON.stringify(call?.arguments ?? ''),
        result: '',
        images: [],
        hasVideo: false,
        children: [],
        isError: false,
        done: false,
      })
    } else if (ev.type === 'tool_result') {
      const id = String(ev.toolCallId ?? '')
      const tool = [...entries.value]
        .reverse()
        .find((e): e is AgentToolEntry => e.kind === 'tool' && e.id === id)
      if (tool) {
        tool.done = true
        tool.isError = ev.isError === true
        const output = (ev.result as { output?: unknown } | undefined)?.output
        tool.result = formatToolOutput(output)
        tool.images = extractToolImages(output)
        tool.hasVideo = hasToolVideo(output)
      }
      // 审批后的 tool_result 到达即说明流已恢复
      if (status.value === 'awaiting_approval') status.value = 'running'
    } else if (ev.type === 'subagent_event') {
      // 子代理嵌套事件:按 toolCallId 挂到对应 spawn_subagent 工具条目的 children,
      // 不建独立顶层条目;不落盘(历史只留工具条目的结论 output)。
      const id = String(ev.toolCallId ?? '')
      const tool = [...entries.value]
        .reverse()
        .find((e): e is AgentToolEntry => e.kind === 'tool' && e.id === id)
      const child = ev.event as Record<string, unknown> | undefined
      if (tool && child) {
        if (child.type === 'think_delta' || child.type === 'text_delta') {
          const kind = child.type === 'think_delta' ? ('think' as const) : ('text' as const)
          const last = tool.children[tool.children.length - 1]
          // 连续同类 delta 聚合进同一尾巴条目;中间插过工具调用则开新块
          if (last?.kind === kind) last.text += String(child.text ?? '')
          else tool.children.push({ kind, text: String(child.text ?? '') })
        } else if (child.type === 'tool_call_start') {
          const call = child.call as
            | { id?: unknown; name?: unknown; arguments?: unknown }
            | undefined
          tool.children.push({
            kind: 'tool',
            id: String(call?.id ?? ''),
            name: String(call?.name ?? ''),
            args:
              typeof call?.arguments === 'string'
                ? call.arguments
                : JSON.stringify(call?.arguments ?? ''),
            done: false,
          })
        } else if (child.type === 'tool_result') {
          const cid = String(child.toolCallId ?? '')
          const ct = [...tool.children]
            .reverse()
            .find(
              (c): c is Extract<AgentSubItem, { kind: 'tool' }> =>
                c.kind === 'tool' && c.id === cid,
            )
          if (ct) ct.done = true
        }
        // 其余子事件(step_done/done 等)无独立 UI
      }
    } else if (ev.type === 'approval_request') {
      entries.value.push({
        kind: 'approval',
        requestId: String(ev.requestId ?? ''),
        toolName: String(ev.toolName ?? ''),
        approvalRule: String(ev.approvalRule ?? ''),
        ...(ev.description != null ? { description: String(ev.description) } : {}),
        accesses: Array.isArray(ev.accesses) ? (ev.accesses as AgentAccess[]) : [],
      })
      status.value = 'awaiting_approval'
    } else if (ev.type === 'detection') {
      entries.value.push({ kind: 'detection', data: ev.data })
    } else if (ev.type === 'steer') {
      // 进行中插话生效:本地已乐观插入(发 /steer 成功时)则按 text 去重跳过,
      // 否则(他端插入)补一条带「已插话」标记的 user 条目
      const text = String(ev.text ?? '')
      const i = pendingSteers.indexOf(text)
      if (i >= 0) {
        pendingSteers.splice(i, 1)
      } else {
        entries.value.push({
          kind: 'user',
          text,
          steered: true,
          at: Date.now(),
          ...(Array.isArray(ev.images) && ev.images.length
            ? { images: ev.images.map(String) }
            : {}),
          ...(ev.videoPath != null ? { videoPath: String(ev.videoPath) } : {}),
        })
      }
    } else if (ev.type === 'context_usage') {
      const used = Number(ev.usedTokens)
      const max = Number(ev.maxTokens)
      if (Number.isFinite(used) && used >= 0) usedTokens.value = used
      if (Number.isFinite(max) && max > 0) maxTokens.value = max
    } else if (ev.type === 'compaction') {
      entries.value.push({ kind: 'system', text: '上下文已自动压缩' })
    } else if (ev.type === 'done') {
      if (ev.reason === 'error') {
        status.value = 'failed'
        error.value = typeof ev.error === 'string' ? ev.error : 'Agent 运行出错'
      } else {
        status.value = 'done'
        // 输出达到 token 上限被截断:时间线末尾插警示条目(仅本地,不落历史)
        if (ev.truncated === true) {
          entries.value.push({
            kind: 'system',
            text: '输出达到 token 上限被截断,部分内容可能不完整,可继续追问',
            tone: 'warn',
          })
        }
      }
    }
    // step_done:无独立 UI(工具批结束的进度信号,时间线条目已自解释)
  }

  /** 进行中插话:POST /steer,成功后乐观插入带「已插话」标记的 user 条目
   * (SSE steer 事件/轮询补齐到达时按 text 去重)。409 no_active_turn 表示
   * 轮次恰好结束:返回 false 由调用方回退正常 /chat;其他错误原样抛。 */
  async function trySteer(input: string, opts: SendOptions): Promise<boolean> {
    try {
      await postJson(`/api/agent/sessions/${sessionId.value}/steer`, {
        input,
        ...(opts.videoPath ? { videoPath: opts.videoPath } : {}),
        ...(opts.images?.length ? { images: opts.images } : {}),
      })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 恢复态下轮次已结束:停轮询收尾,随后走正常 /chat 重连实时流
        stopPolling()
        recovering.value = false
        return false
      }
      throw e
    }
    pendingSteers.push(input)
    entries.value.push({
      kind: 'user',
      text: input,
      steered: true,
      at: Date.now(),
      ...(opts.videoPath ? { videoPath: opts.videoPath } : {}),
      ...(opts.videoSrc ? { videoSrc: opts.videoSrc } : {}),
      ...(opts.images?.length ? { images: [...opts.images] } : {}),
    })
    return true
  }

  /** 显式终止进行中轮次:POST /cancel(断连不再杀轮次,停止语义由它承担;
   * 本地流随后会收到 done 自然收尾)。409 no_active_turn 说明服务端轮次已结束
   * (恢复态滞后),拉齐 events 收尾;其他错误抛给调用方提示。 */
  async function cancelTurn() {
    if (!sessionId.value) return
    try {
      await postJson(`/api/agent/sessions/${sessionId.value}/cancel`, {})
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        await pollEvents(sessionId.value)
        return
      }
      throw e
    }
    // 恢复态(本地无流)收不到 done:主动拉齐一次;服务端收尾有延迟时
    // inProgress 仍为 true,由在跑的轮询继续补齐直至结束
    if (recovering.value) await pollEvents(sessionId.value)
  }

  /** 发起一轮 /chat SSE(不压 user 条目;send/retry 共用)。 */
  async function runTurn() {
    const seq = ++turnSeq
    status.value = 'running'
    error.value = null
    ctrl = new AbortController()
    try {
      let res: Response
      try {
        res = await fetch('/api/agent/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sessionId: sessionId.value,
            input: lastInput,
            ...(lastVideoPath ? { videoPath: lastVideoPath } : {}),
            ...(lastImages ? { images: lastImages } : {}),
          }),
          signal: ctrl.signal,
        })
      } catch (e) {
        if ((e as Error).name === 'AbortError') return
        throw new ApiError(0, '网络错误,无法连接后端')
      }
      if (!res.ok || !res.body) throw await toApiError(res)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE 事件以空行(\n\n)分隔;每行 data: {json}
        let sep: number
        while ((sep = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, sep)
          buf = buf.slice(sep + 2)
          for (const line of raw.split('\n')) {
            if (!line.startsWith('data:')) continue
            let ev: Record<string, unknown>
            try {
              ev = JSON.parse(line.slice(5).trim())
            } catch {
              continue // 非 JSON 行(注释/心跳)忽略
            }
            handleEvent(ev)
          }
        }
      }
      // 流结束但没收到 done(代理中断等):停在运行态会卡死 UI,按失败处理
      if (status.value === 'running' || status.value === 'awaiting_approval') {
        status.value = 'failed'
        error.value = 'SSE 流异常中断,未收到完成事件'
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        // stop() 主动中断:静默收尾,已收到内容保留在条目里;
        // 已被 supersedeTurn 接管的旧轮次不写状态(切换/新建会话场景)
        if (seq === turnSeq) status.value = 'done'
        return
      }
      status.value = 'failed'
      error.value = (e as Error).message
    } finally {
      if (seq === turnSeq) ctrl = null
    }
  }

  /** 发送一轮(压 user 条目);opts.images 为 dataURL 数组(≤4)。
   * 进行中(running/awaiting_approval)改走 /steer 插话(乐观插入 user 条目,
   * 409 no_active_turn 回退正常 /chat);connecting 中忽略。
   * 无会话(工作区切换后已清空)时先按当前工作区惰性建会话,建不上则交给失败条/重试。 */
  async function send(input: string, opts: SendOptions = {}) {
    if (status.value === 'connecting') return
    const busy = status.value === 'running' || status.value === 'awaiting_approval'
    if (busy) {
      if (!sessionId.value) return
      if (await trySteer(input, opts)) return
      // 409 回退:轮次已结束,落入正常发送路径
    }
    if (!sessionId.value) {
      await createSession()
      if (!sessionId.value) return // 建会话失败:状态已置 failed,由失败条重试兜底
    }
    lastInput = input
    lastVideoPath = opts.videoPath || undefined
    lastImages = opts.images?.length ? [...opts.images] : undefined
    entries.value.push({
      kind: 'user',
      text: input,
      at: Date.now(),
      ...(lastVideoPath ? { videoPath: lastVideoPath } : {}),
      ...(opts.videoSrc ? { videoSrc: opts.videoSrc } : {}),
      ...(lastImages ? { images: lastImages } : {}),
    })
    await runTurn()
    // 首轮后标题/lastActiveAt 已变,刷新会话列表(失败不阻断)
    await fetchSessions().catch(() => {})
  }

  /** 上传视频文件落工作区(POST /api/agent/uploads),返回路径设为待发送视频附件。
   * 失败抛 ApiError(调用方提示);成功后 pendingVideo.src 供 composer 预览。 */
  async function uploadVideo(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    let res: Response
    try {
      res = await fetch('/api/agent/uploads', { method: 'POST', body: fd })
    } catch {
      throw new ApiError(0, '网络错误,无法连接后端')
    }
    if (!res.ok) throw await toApiError(res)
    const r = (await res.json()) as { path?: string; name?: string }
    if (!r.path || !r.name) throw new ApiError(0, '后端未返回视频路径')
    pendingVideo.value = {
      path: r.path,
      name: r.name,
      src: `/api/agent/uploads/${encodeURIComponent(r.name)}`,
    }
  }

  /** 撤回:删到指定用户消息为止(含其后全部条目)。
   * localIndex 是本地时间线下标;system 条目仅本地流式插入不落库,
   * 提交给后端的 entryIndex 需剔除 system 后换算。409(对话进行中)原样抛给调用方。 */
  async function recallFrom(localIndex: number) {
    if (!sessionId.value) throw new ApiError(0, '无活动会话')
    const target = entries.value[localIndex]
    if (!target || target.kind !== 'user') throw new ApiError(0, '仅支持撤回用户消息')
    const persistedIndex =
      entries.value.slice(0, localIndex + 1).filter((e) => e.kind !== 'system').length - 1
    await postJson(`/api/agent/sessions/${sessionId.value}/recall`, {
      entryIndex: persistedIndex,
    })
    entries.value.splice(localIndex)
  }

  /** 失败重试:会话都没建上则重建;否则用上次输入重跑一轮(不重复压 user 条目)。 */
  async function retry() {
    if (!sessionId.value) {
      await createSession()
      return
    }
    if (!lastInput) return
    await runTurn()
  }

  /** 审批回执:approved/rejected,scope:'session' 表示本会话都批准。
   * 失败抛错(调用方提示),条目保持未决状态可再点。 */
  async function respondApproval(
    requestId: string,
    decision: 'approved' | 'rejected',
    scope?: 'session',
  ) {
    await postJson('/api/agent/approval', {
      requestId,
      decision,
      ...(scope ? { scope } : {}),
    })
    const entry = [...entries.value]
      .reverse()
      .find((e): e is AgentApprovalEntry => e.kind === 'approval' && e.requestId === requestId)
    if (entry) entry.decision = scope === 'session' ? 'approved_session' : decision
    if (status.value === 'awaiting_approval') status.value = 'running'
  }

  /** 手动压缩上下文:POST compact;成功且有 afterTokens 时刷新圆环用量。
   * 失败抛错(调用方提示);进行中后端返回 409。 */
  async function compactContext() {
    if (!sessionId.value) throw new ApiError(0, '无活动会话')
    const r = (await postJson(`/api/agent/sessions/${sessionId.value}/compact`, {})) as {
      compacted?: boolean
      afterTokens?: number
    }
    if (r.compacted && typeof r.afterTokens === 'number') usedTokens.value = r.afterTokens
    return r
  }

  return {
    sessionId,
    mode,
    status,
    error,
    entries,
    sessions,
    usedTokens,
    maxTokens,
    pendingVideo,
    recovering,
    setPendingVideo,
    clearPendingVideo,
    fetchSessions,
    createSession,
    selectSession,
    openSession,
    deleteSession,
    setMode,
    newSession,
    send,
    uploadVideo,
    recallFrom,
    retry,
    stop,
    cancelTurn,
    refreshProgress,
    respondApproval,
    compactContext,
  }
})
