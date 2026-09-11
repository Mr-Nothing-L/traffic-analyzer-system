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
 *   每个事件都带 seq=发出时后端已落盘条数水位,据此维护会话落盘水位 persistedSeq);
 *   done {reason, error?, truncated?},truncated:true 表示输出达到 token 上限被截断);
 * POST   /api/agent/approval({requestId,decision,scope?}) → {status:'ok'};
 * POST   /api/agent/uploads(multipart file) → {path,name,size,contentType}(视频落工作区,
 *   返回 path 作 videoPath;GET /api/agent/uploads/{name} 供 <video> 预览);
 * POST   /api/agent/sessions/{id}/recall({entryIndex}) → {status:'ok'}(409=对话进行中;
 *   entryIndex 为后端持久化条目下标,前端按条目落盘序号 seq(seq-1=下标)直送,
 *   本地 system 条目不落盘、天然不在其列);
 * GET    /api/agent/sessions/{id}/events?fromSeq=N → {events:[{seq,entry}],inProgress}
 *   (断连续传:已落盘条目中 seq>N 的部分;inProgress 表示服务端轮次仍在跑——
 *   断连不再杀轮次,刷新后据此 5s 轮询补齐,不做实时流重连);
 * POST   /api/agent/sessions/{id}/cancel → 显式终止进行中轮次(409 no_active_turn);
 * POST   /api/agent/sessions/{id}/steer({input,videoPath?,images?}) → 进行中插话,
 *   下一 step 边界生效(409 no_active_turn 回退 /chat);生效时 SSE 收到
 *   {type:'steer',text,images,videoPath?,seq} 事件(本地乐观插入,按条目 id
 *   先入先出绑定去重,同文本两次插话不误判);轮末未消费的插话以
 *   {type:'steer_dropped',text} 告知,乐观条目随之移出时间线。
 * fetch+ReadableStream 按 \n\n 分块解析 data: 行,组件只接线,状态全部在这里。
 * 会话的「活动」收敛为 ActiveSession 实例(SSE 流、断线轮询、取消、steer 乐观账、
 * 失败重试快照、落盘水位都封装在实例内,类已拆至 stores/activeSession.ts,条目
 * 映射纯函数在 stores/agentEntries.ts);切会话/新建/切工作区=store 整体替换实例
 * (swap),旧实例 dispose 后晚到的异步收尾一律丢弃。 */
import { markRaw, ref, watch, type Ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, apiFetch } from '../api/client'
import { useWorkspaceStore } from './workspace'
import { ActiveSession, type SessionHost } from './activeSession'
import { mapHistoryEntry } from './agentEntries'

export { resolveMediaUrl } from './agentEntries'

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

/** 审批内容预览(与 agent 端 PreviewContent 对齐)。 */
export interface AgentPreviewContent {
  language: string
  content: string
  truncated: boolean
}

/** GET /sessions 的列表项(createdAt/lastActiveAt 为 epoch ms)。 */
export interface AgentSessionInfo {
  id: string
  workspaceDir?: string
  mode?: AgentMode
  title?: string
  createdAt?: number
  lastActiveAt?: number
  /** 最近一次已知上下文占用(token,有真实 usage 才返回)。 */
  usedTokens?: number
}

export interface AgentUserEntry {
  /** 前端条目 id(进入时间线即赋,全局唯一):v-for key/折叠态/撤回定位都用它。 */
  id: string
  kind: 'user'
  text: string
  /** 后端落盘序号(1 起,seq-1=后端持久化下标):历史加载按数组位置赋;
   * 乐观 steer 在 SSE/轮询确认落盘时绑定;正常发送按会话落盘水位推算。
   * 撤回按它直送后端 entryIndex(后端契约不变,前端不再做下标换算)。 */
  seq?: number
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
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'assistant'
  text: string
  think: string
  /** 该条对应 generate 步的 LLM 调用壁钟耗时(ms;实时由 generate_done SSE 写入,
   * 历史由后端落盘的 assistant 条目恢复;旧数据无此字段)。 */
  generateMs?: number
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
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'tool'
  /** tool_call_start 的 call.id,tool_result 按它回填(区别于前端条目 id)。 */
  callId: string
  name: string
  /** 创建时刻(epoch ms):流式本地打点/历史取落盘 at。分析链路流程图耗时推导用。 */
  at?: number
  /** call.arguments 原文(JSON 字符串)。 */
  args: string
  result: string
  /** 工具输出中的图片(展示地址:dataURL 或 /api/agent 代理的 media 引用 URL;
   * extract_frames/draw_boxes 等返回的 image ContentPart)。 */
  images: string[]
  /** 工具输出含 video part(load_video 整段视频直传);只显示静态提示,不做播放器。 */
  hasVideo: boolean
  /** 子代理迷你时间线(subagent_event 按 toolCallId 挂到本条目)。 */
  children: AgentSubItem[]
  isError: boolean
  done: boolean
}

export interface AgentApprovalEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'approval'
  requestId: string
  toolName: string
  approvalRule: string
  description?: string
  accesses: AgentAccess[]
  /** 创建时刻(epoch ms),见 AgentToolEntry.at。 */
  at?: number
  /** 工具调用参数的内容预览。 */
  preview?: AgentPreviewContent
  /** 已回执后记录:'approved' | 'rejected' | 'approved_session' | 'cancelled'(历史)。 */
  decision?: string
  /** 历史载入的未决审批:后端不再接受回执,UI 显示「已失效」,不渲染按钮。 */
  stale?: boolean
}

/** detection 事件的 data:submit_detection 提交的结构化检测载荷(以 agent 端
 * submitDetection.ts 的 zod schema 为权威对齐);仅迁移前的旧落盘条目可能是
 * 未解析成功的字符串,ChatView 按 detection-raw 降级渲染。 */
export interface DetectionPayload {
  video_path?: string
  binary_encoding?: string
  normal?: boolean
  events?: Array<{
    event_id: number
    detected: boolean
    confidence?: number
    instances?: Array<{
      description: string
      location: string
      start_sec: number
      end_sec: number
    }>
    reasoning: string
    /** 证据帧时间点(秒)。 */
    evidence_frames: number[]
    /** 可选定位框(归一化 xyxy)及其所属帧;用于服务端渲染标注图。 */
    boxes?: Array<{ x1: number; y1: number; x2: number; y2: number; label?: string }>
    box_frame?: number
    /** 逐事件标注图(submit_detection 服务端生成;展示地址为 dataURL(旧条目)
     * 或 /api/agent 代理的 media 引用 URL(新条目,store 解析后);无框/画框失败时缺省)。 */
    annotated_image?: string
  }>
  /** 标注降级元信息(两级):missing_boxes=检出但未给 boxes/box_frame;annotation_failed=画框失败。 */
  meta?: {
    missing_boxes?: number[]
    annotation_failed?: number[]
  }
  report_markdown?: string
}

export interface AgentDetectionEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'detection'
  data: unknown
  /** 创建时刻(epoch ms),见 AgentToolEntry.at。 */
  at?: number
}

/** 系统提示条目(如自动压缩提示、输出截断警示),不进历史,仅流式期间插入时间线。
 * tone:'warn' 用警示色系(gold)渲染,缺省为中性灰。 */
export interface AgentSystemEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'system'
  text: string
  tone?: 'warn'
  /** 创建时刻(epoch ms),见 AgentToolEntry.at。 */
  at?: number
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

  /** 当前活动会话实例(未选中/已清空为 null);selectSession/createSession/
   * 工作区切换经 swap 整体替换。 */
  let active: ActiveSession | null = null

  /** 实例的共享状态通道(时间线/运行状态 refs + 会话列表刷新)。 */
  const host: SessionHost = {
    sessionId,
    entries,
    status,
    error,
    recovering,
    usedTokens,
    maxTokens,
    fetchSessions: () => fetchSessions(),
  }

  /** 会话替换唯一入口:dispose 旧实例(中断其 SSE 流与恢复轮询、清乐观账,
   * 晚到的异步收尾从此丢弃)并退出恢复态。会话级字段(sessionId/entries/…)
   * 由调用方按需清理(切换在途失败时旧时间线仍可见,与既有行为一致)。 */
  function swap(next: ActiveSession | null) {
    active?.dispose()
    active = next
    recovering.value = false
  }

  /** 取当前会话实例;不存在则现建(测试/恢复现场手动置 running 态的兜底)。 */
  function ensureActive(): ActiveSession {
    if (active === null) active = new ActiveSession(host)
    return active
  }

  function stop() {
    active?.stop()
  }

  /* ---- 工作区切换:后端在建会话时注入当前工作区作 workspaceDir,旧会话仍绑旧工作区,
   * 新工作区相对路径送入旧会话会解析失败。监听工作区路径变化:替换会话实例并清
   * 当前会话态与 pendingVideo(不删后端历史会话),下次发送/进入对话时按新工作区
   * 惰性建新会话。首次加载(prev 为 null)不算切换;selectSession 不动工作区路径,
   * 不会误触发。 */
  const wsStore = useWorkspaceStore()
  watch(
    () => wsStore.path,
    (next, prev) => {
      if (prev == null || next === prev) return
      swap(null) // 旧实例作废:在途流/轮询/晚到的 history 一并失效
      sessionId.value = null
      entries.value = []
      usedTokens.value = null
      pendingVideo.value = null
      error.value = null
      status.value = 'idle'
      // 切换端点已同步触发 agent server 恢复该工作区的磁盘历史(set_workspace
      // 返回时 restore 已完成),此处重拉列表让恢复的会话立即可见;
      // 失败不阻塞切换,仅留在控制台。
      void fetchSessions().catch((e) => console.warn('[agentchat] 工作区切换后刷新会话列表失败', e))
    },
  )

  /** 拉历史会话列表(失败抛错,调用方提示)。 */
  async function fetchSessions() {
    const r = (await apiFetch('/api/agent/sessions')) as {
      sessions?: AgentSessionInfo[]
    }
    sessions.value = Array.isArray(r.sessions) ? r.sessions : []
  }

  /** 创建会话:workspaceDir 由后端代理注入,前端只传权限模式。 */
  async function createSession() {
    swap(null) // 取代任何在途实例(selectSession/runTurn/轮询一并作废)
    status.value = 'connecting'
    error.value = null
    sessionId.value = null
    usedTokens.value = null
    try {
      const r = (await apiFetch('/api/agent/sessions', { method: 'POST', body: JSON.stringify({ mode: mode.value }) })) as {
        sessionId?: string
      }
      if (!r.sessionId) throw new ApiError(0, '后端未返回 sessionId')
      if (active !== null) return // 在途期间发生了更新的选择/切换:让位
      sessionId.value = r.sessionId
      status.value = 'idle'
      await fetchSessions().catch(() => {}) // 列表出现新会话(无标题)
    } catch (e) {
      status.value = 'failed'
      error.value = (e as Error).message
    }
  }

  /** 切换历史会话:拉 history 重建时间线;权限模式跟随会话属性。
   * 在途期间发生更新的选择/新建/工作区切换都会 swap 掉调用时的实例,晚到的
   * 响应因实例已 dispose 被丢弃(旧工作区会话不得写回时间线)。 */
  async function selectSession(id: string) {
    if (id === sessionId.value && entries.value.length) return
    const s = new ActiveSession(host)
    swap(s)
    status.value = 'connecting'
    error.value = null
    try {
      const r = (await apiFetch(`/api/agent/sessions/${id}/history`)) as {
        entries?: unknown[]
      }
      if (active !== s) return // 在途期间发生了更新的选择/新建/工作区切换:丢弃晚到的历史
      entries.value = (Array.isArray(r.entries) ? r.entries : [])
        .map((raw, i) => {
          // 历史条目落盘后不再变更:markRaw 跳过深响应式代理(工具结果/
          // 附件里的大 base64 字符串逐层 proxy 化开销可观);例外:审批条目
          // 小且 respondApproval 要回写 decision,保持响应式。seq=数组下标+1
          // (后端落盘序号,撤回定位用)。
          const e = mapHistoryEntry(raw, i + 1)
          if (e === null) return null
          return e.kind === 'approval' ? e : markRaw(e)
        })
        .filter((e): e is AgentEntry => e !== null)
      sessionId.value = id
      s.syncPersisted(entries.value.length)
      const info = sessions.value.find((x) => x.id === id)
      if (info?.mode === 'manual' || info?.mode === 'auto' || info?.mode === 'yolo') {
        mode.value = info.mode
      }
      // 会话切换:沿用列表里的最近已知用量(没有则清空等下一次 context_usage)
      usedTokens.value = typeof info?.usedTokens === 'number' ? info.usedTokens : null
      // 刷新/重载场景:已落盘的未决审批(轮次已结束但仍挂起)需武装审批态,
      // 否则 composer 审批面板不出现,而时间线卡仍提示「请在下方输入区处理」。
      // 与 mergePersistedEvents 的判定对称(SSE/轮询路径只覆盖新增条目)。
      const hasPendingApproval = entries.value.some(
        (e) => e.kind === 'approval' && !e.decision && !e.stale,
      )
      status.value = hasPendingApproval ? 'awaiting_approval' : 'idle'
      // 断连恢复探测:服务端轮次仍在跑则进入恢复态(轮询补齐)
      await s.resumeFromServer()
    } catch (e) {
      if (active === s) {
        status.value = 'failed'
        error.value = (e as Error).message
      }
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
      await apiFetch(`/api/agent/sessions/${id}`, { method: 'DELETE' })
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

  /** 重命名会话:成功后就地更新列表项标题;空串 = 恢复自动派生(后端语义)。 */
  async function renameSession(id: string, title: string) {
    const res = (await apiFetch(`/api/agent/sessions/${id}/title`, { method: 'POST', body: JSON.stringify({ title }) })) as {
      title?: string
    }
    const s = sessions.value.find((x) => x.id === id)
    if (s) s.title = res.title ?? title.trim()
  }

  /** 切换权限模式:有会话走 POST /sessions/{id}/mode 就地切换,成功后更新本地 mode
   * 与列表项;无会话先记在 mode,建会话时带上。失败抛错(调用方提示),本地 mode 不变。 */
  async function setMode(m: AgentMode) {
    if (m === mode.value) return
    if (!sessionId.value) {
      mode.value = m
      return
    }
    await apiFetch(`/api/agent/sessions/${sessionId.value}/mode`, { method: 'POST', body: JSON.stringify({ mode: m }) })
    mode.value = m
    const info = sessions.value.find((s) => s.id === sessionId.value)
    if (info) info.mode = m
  }

  /** 新建会话(同模式):清空时间线重来。 */
  async function newSession() {
    entries.value = []
    await createSession()
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
      if (await ensureActive().steer(input, opts)) return
      // 409 回退:轮次已结束,落入正常发送路径
    }
    if (!sessionId.value) {
      await createSession()
      if (!sessionId.value) return // 建会话失败:状态已置 failed,由失败条重试兜底
    }
    const s = ensureActive()
    await s.runTurn(input, opts)
    // 首轮后标题/lastActiveAt 已变,刷新会话列表(失败不阻断)
    await fetchSessions().catch(() => {})
  }

  /** 停止:显式终止服务端轮次(POST /cancel;本地流随后收到 done 自然收尾)。 */
  async function cancelTurn() {
    await active?.cancel()
  }

  /** 上传视频文件落工作区(POST /api/agent/uploads),返回路径设为待发送视频附件。
   * 失败抛 ApiError(调用方提示);成功后 pendingVideo.src 供 composer 预览。 */
  async function uploadVideo(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    const r = (await apiFetch('/api/agent/uploads', { method: 'POST', body: fd })) as {
      path?: string
      name?: string
    }
    if (!r.path || !r.name) throw new ApiError(0, '后端未返回视频路径')
    pendingVideo.value = {
      path: r.path,
      name: r.name,
      src: `/api/agent/uploads/${encodeURIComponent(r.name)}`,
    }
  }

  /** 撤回:删到指定用户消息为止(含其后全部条目)。entryId 是前端条目 id
   * (ChatView 定位),落到后端契约:条目落盘序号 seq-1 即 entryIndex(乐观
   * steer 已在生效时绑定 seq;system 条目不落盘、不在其列)。409(对话进行中)
   * 原样抛给调用方。 */
  async function recallFrom(entryId: string) {
    if (!sessionId.value) throw new ApiError(0, '无活动会话')
    const idx = entries.value.findIndex((e) => e.id === entryId)
    const target = entries.value[idx]
    if (!target || target.kind !== 'user') throw new ApiError(0, '仅支持撤回用户消息')
    const seq = target.seq
    if (seq == null) throw new ApiError(0, '该消息尚未落盘,无法撤回')
    await apiFetch(`/api/agent/sessions/${sessionId.value}/recall`, { method: 'POST', body: JSON.stringify({
      entryIndex: seq - 1,
    }) })
    entries.value.splice(idx)
    active?.rewindTo(seq)
  }

  /** 失败重试:会话都没建上则重建;否则用上次输入重跑一轮(不重复压 user 条目,
   * 快照随会话实例存续,切换会话自然作废)。 */
  async function retry() {
    if (!sessionId.value) {
      await createSession()
      return
    }
    await ensureActive().retryTurn()
  }

  /** 审批回执:approved/rejected,scope:'session' 表示本会话都批准。
   * 失败抛错(调用方提示),条目保持未决状态可再点。 */
  async function respondApproval(
    requestId: string,
    decision: 'approved' | 'rejected',
    scope?: 'session',
  ) {
    await apiFetch('/api/agent/approval', { method: 'POST', body: JSON.stringify({
      requestId,
      decision,
      ...(scope ? { scope } : {}) }),
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
    const r = (await apiFetch(`/api/agent/sessions/${sessionId.value}/compact`, { method: 'POST', body: JSON.stringify({}) })) as {
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
    renameSession,
    setMode,
    newSession,
    send,
    uploadVideo,
    recallFrom,
    retry,
    stop,
    cancelTurn,
    respondApproval,
    compactContext,
  }
})
