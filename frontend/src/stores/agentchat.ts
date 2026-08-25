/** 统一对话(问答 + 检测):会话列表/切换/删除 + 时间线条目 + SSE 流式轮次。
 * 后端契约(FastAPI /api/agent/* 代理 → Node agent 服务,workspaceDir 由后端注入):
 * POST   /api/agent/sessions({mode}) → {sessionId};
 * GET    /api/agent/sessions → {sessions:[{id,workspaceDir,mode,title,createdAt,lastActiveAt,usedTokens?}]};
 * GET    /api/agent/sessions/{id}/history → {entries:[...]}(五类条目,见 mapHistoryEntry);
 * POST   /api/agent/sessions/{id}/compact → {status,compacted,beforeTokens?,afterTokens?}(进行中 409);
 * DELETE /api/agent/sessions/{id} → {status:'ok'};
 * POST   /api/agent/chat({sessionId,input,videoPath?,images?(dataURL,≤4)}) → SSE
 *   (每事件一行 'data: {json}\n\n',事件:text_delta/think_delta/tool_call_start/
 *   tool_result/step_done/approval_request/detection/context_usage/compaction/done);
 * POST   /api/agent/approval({requestId,decision,scope?}) → {status:'ok'}。
 * fetch+ReadableStream 按 \n\n 分块解析 data: 行,组件只接线,状态全部在这里。 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '../api/client'

export type AgentMode = 'manual' | 'yolo'

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
  /** dataURL 图片附件(发送时随消息上传,历史原样返回)。 */
  images?: string[]
}

export interface AgentAssistantEntry {
  kind: 'assistant'
  text: string
  think: string
}

export interface AgentToolEntry {
  kind: 'tool'
  /** tool_call_start 的 call.id,tool_result 按它回填。 */
  id: string
  name: string
  /** call.arguments 原文(JSON 字符串)。 */
  args: string
  result: string
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
  }>
  report_markdown?: string
}

export interface AgentDetectionEntry {
  kind: 'detection'
  data: unknown
}

/** 系统提示条目(如自动压缩提示),不进历史,仅流式期间插入时间线。 */
export interface AgentSystemEntry {
  kind: 'system'
  text: string
}

export type AgentEntry =
  | AgentUserEntry
  | AgentAssistantEntry
  | AgentToolEntry
  | AgentApprovalEntry
  | AgentDetectionEntry
  | AgentSystemEntry

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
    }
  }
  if (e.kind === 'assistant') {
    return { kind: 'assistant', text: String(e.text ?? ''), think: String(e.think ?? '') }
  }
  if (e.kind === 'tool') {
    return {
      kind: 'tool',
      id: String(e.toolCallId ?? ''),
      name: String(e.name ?? ''),
      args:
        typeof e.arguments === 'string' ? e.arguments : JSON.stringify(e.arguments ?? ''),
      result: formatToolOutput(e.output),
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

  let ctrl: AbortController | null = null
  /** 轮次代际:切换/新建会话会中断在途流并递增,旧 runTurn 的收尾不再写状态。 */
  let turnSeq = 0
  let lastInput = ''
  let lastVideoPath: string | undefined
  let lastImages: string[] | undefined

  function stop() {
    ctrl?.abort()
  }

  /** 切换/新建会话前调用:中断在途流,并使其 AbortError 收尾失效(不得覆盖新状态)。 */
  function supersedeTurn() {
    turnSeq += 1
    ctrl?.abort()
  }

  /** 上次输入快照清空(新建/切换会话后,失败重试不得把旧输入重放进新会话)。 */
  function resetLastTurn() {
    lastInput = ''
    lastVideoPath = undefined
    lastImages = undefined
  }

  /** 拉历史会话列表(失败抛错,调用方提示)。 */
  async function fetchSessions() {
    const r = (await reqJson('/api/agent/sessions', 'GET')) as {
      sessions?: AgentSessionInfo[]
    }
    sessions.value = Array.isArray(r.sessions) ? r.sessions : []
  }

  /** 创建会话:workspaceDir 由后端代理注入,前端只传权限模式。 */
  async function createSession() {
    supersedeTurn()
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
    status.value = 'connecting'
    error.value = null
    resetLastTurn()
    try {
      const r = (await reqJson(`/api/agent/sessions/${id}/history`, 'GET')) as {
        entries?: unknown[]
      }
      entries.value = (Array.isArray(r.entries) ? r.entries : [])
        .map(mapHistoryEntry)
        .filter((e): e is AgentEntry => e !== null)
      sessionId.value = id
      const info = sessions.value.find((s) => s.id === id)
      if (info?.mode === 'manual' || info?.mode === 'yolo') mode.value = info.mode
      // 会话切换:沿用列表里的最近已知用量(没有则清空等下一次 context_usage)
      usedTokens.value = typeof info?.usedTokens === 'number' ? info.usedTokens : null
      status.value = 'idle'
    } catch (e) {
      status.value = 'failed'
      error.value = (e as Error).message
    }
  }

  /** 删除会话:列表先移除(optimistic),失败回滚重拉并抛错;
   * 删的是当前会话则切到最近会话,没有则新建。 */
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
      )[0]
      if (latest) await selectSession(latest.id)
      else await createSession()
    }
  }

  /** 切换权限模式:清空时间线并重建会话(模式是会话级属性)。 */
  async function setMode(m: AgentMode) {
    if (m === mode.value && sessionId.value) return
    mode.value = m
    entries.value = []
    await createSession()
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
    const entry: AgentAssistantEntry = { kind: 'assistant', text: '', think: '' }
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
        tool.result = formatToolOutput((ev.result as { output?: unknown } | undefined)?.output)
      }
      // 审批后的 tool_result 到达即说明流已恢复
      if (status.value === 'awaiting_approval') status.value = 'running'
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
      }
    }
    // step_done:无独立 UI(工具批结束的进度信号,时间线条目已自解释)
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

  /** 发送一轮(压 user 条目);images 为 dataURL 数组(≤4)。进行中/审批中忽略。 */
  async function send(input: string, videoPath?: string, images?: string[]) {
    const busy =
      status.value === 'connecting' ||
      status.value === 'running' ||
      status.value === 'awaiting_approval'
    if (!sessionId.value || busy) return
    lastInput = input
    lastVideoPath = videoPath || undefined
    lastImages = images?.length ? [...images] : undefined
    entries.value.push({
      kind: 'user',
      text: input,
      ...(lastVideoPath ? { videoPath: lastVideoPath } : {}),
      ...(lastImages ? { images: lastImages } : {}),
    })
    await runTurn()
    // 首轮后标题/lastActiveAt 已变,刷新会话列表(失败不阻断)
    await fetchSessions().catch(() => {})
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
    fetchSessions,
    createSession,
    selectSession,
    deleteSession,
    setMode,
    newSession,
    send,
    retry,
    stop,
    respondApproval,
    compactContext,
  }
})
