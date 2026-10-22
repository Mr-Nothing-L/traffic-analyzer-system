/** 活动会话控制器与会话状态通道(自 stores/agentchat.ts 拆出):一次
 * 「选中/新建」生命周期内的全部在途工作——SSE 流式轮次、断线恢复轮询、取消、
 * steer 乐观账、失败重试快照、后端落盘水位。切会话/新建/切工作区 = store
 * 整体替换实例(swap):旧实例 dispose 时中断其流与轮询,此后任何晚到的异步
 * 收尾因 disposed 不再写共享状态。实例只经 SessionHost 读写时间线与运行
 * 状态,会话编排(选择/新建/删除/工作区切换)留在 store。 */
import { markRaw, type Ref } from 'vue'
import { ApiError } from '../api/client'
import {
  toApiError,
  extractToolImages,
  formatToolOutput,
  hasToolVideo,
  mapDetectionData,
  mapHistoryEntry,
  nextEntryId,
  postJson,
  reqJson,
  resolveMediaUrl,
} from './entries'
import type {
  AgentAccess,
  AgentAssistantEntry,
  AgentEntry,
  AgentPreviewContent,
  AgentStatus,
  AgentSubItem,
  AgentToolEntry,
  PendingVideo,
  SendOptions,
} from './types'

/** ActiveSession 写共享状态的通道:会话编排(选择/新建/删除/工作区切换)留在
 * store,实例只经 host 读写时间线与运行状态。 */
export interface SessionHost {
  sessionId: Ref<string | null>
  entries: Ref<AgentEntry[]>
  status: Ref<AgentStatus>
  error: Ref<string | null>
  recovering: Ref<boolean>
  usedTokens: Ref<number | null>
  maxTokens: Ref<number>
  /** 轮询收尾/首轮结束后刷新会话列表(失败由实例吞掉)。 */
  fetchSessions(): Promise<void>
}

/** 活动会话控制器:一次「选中/新建」生命周期内的全部在途工作——SSE 流式轮次、
 * 断线恢复轮询、取消、steer 乐观账、失败重试快照、后端落盘水位。切会话/新建/
 * 切工作区 = store 整体替换实例(swap):旧实例 dispose 时中断其流与轮询,此后
 * 任何晚到的异步收尾因 disposed 不再写共享状态——store 顶层的 turnSeq/selectSeq/
 * pollTimer/pollSeq/pendingSteers 五个代际计数器由此被实例生命周期取代。 */
export class ActiveSession {
  private disposed = false
  /** 在途 SSE 轮次的中断器(stop/替换实例时中断)。 */
  private ctrl: AbortController | null = null
  /** 轮次代际(实例内):retry 连点等并发 runTurn 时,旧轮次的收尾不写状态。 */
  private turnSeq = 0
  /** 恢复轮询定时器(recovering 期间存活)。 */
  private pollTimer: ReturnType<typeof setInterval> | null = null
  /** 后端落盘水位(已落盘条数;后端 seq 从 1 起=落盘序号):events 轮询的
   * fromSeq,也是给本地新 user 条目推算落盘序号(+1)的基准。 */
  private persistedSeq = 0
  /** 已并入时间线的落盘 seq 集合(防重):恢复探测与 5s 自动轮询并发/重叠返回
   * 同一段 events 时按 seq 跳过已存在条目(detection/done 不重复追加)。 */
  private mergedSeqs = new Set<number>()
  /** 乐观 steer 条目 id 队列(先入先出):POST /steer 成功入队;SSE steer 事件/
   * 轮询补齐按到达顺序出队绑定落盘 seq——同文本两次插话各占一席,不误判。 */
  private pendingSteerIds: string[] = []
  /** 失败重试快照:随实例生灭(切会话自然作废,无需手工清空)。 */
  private lastInput = ''
  private lastVideoPath: string | undefined
  private lastImages: string[] | undefined

  constructor(private host: SessionHost) {}

  /** 会话被替换/工作区切换:中断在途流与轮询、清乐观账,此后晚到的收尾一律丢弃。 */
  dispose() {
    this.disposed = true
    this.ctrl?.abort()
    this.stopPolling()
    this.pendingSteerIds = []
  }

  /** 离开页面/删除当前会话:中断在途流、停轮询;实例仍有效(会话未替换)。 */
  stop() {
    this.ctrl?.abort()
    this.stopPolling()
  }

  /** 历史重载后校准落盘水位(条目 seq 即其数组下标+1);1..seq 视为已在时间线。 */
  syncPersisted(seq: number) {
    this.persistedSeq = seq
    for (let i = 1; i <= seq; i++) this.mergedSeqs.add(i)
  }

  /** 撤回成功后回卷水位:后端只保留前 seq-1 条。 */
  rewindTo(seq: number) {
    this.persistedSeq = seq - 1
  }

  /* ---- 断连恢复:events 轮询(简化方案,不做实时流重连) ---- */

  /** selectSession 收尾的恢复探测:以已加载水位拉一次 events,inProgress=true
   * 则进入恢复态(status running + 常驻条 + 5s 轮询)。探测失败(网络/404)
   * 不阻塞会话切换。 */
  async resumeFromServer() {
    let r: { events?: Array<{ seq?: unknown; entry?: unknown }>; inProgress?: boolean }
    try {
      r = (await reqJson(
        `/api/agent/sessions/${this.host.sessionId.value}/events?fromSeq=${this.persistedSeq}`,
        'GET',
      )) as typeof r
    } catch {
      return
    }
    if (this.disposed) return // 探测在途期间已切换会话
    this.mergePersistedEvents(Array.isArray(r.events) ? r.events : [])
    if (r.inProgress === true) {
      this.host.recovering.value = true
      this.host.status.value = 'running'
      this.startPolling()
    }
  }

  private stopPolling() {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    }
  }

  private startPolling() {
    this.stopPolling()
    this.pollTimer = setInterval(() => {
      void this.pollEvents()
    }, 5000)
  }

  /** 拉一次 events 并合并;inProgress 变 false 时收尾恢复态(停轮询、置完成)。
   * 网络抖动静默,下轮重试;切换会话后的晚到响应整体丢弃。 */
  async pollEvents() {
    if (this.disposed) return
    let r: { events?: Array<{ seq?: unknown; entry?: unknown }>; inProgress?: boolean }
    try {
      r = (await reqJson(
        `/api/agent/sessions/${this.host.sessionId.value}/events?fromSeq=${this.persistedSeq}`,
        'GET',
      )) as typeof r
    } catch {
      return // 网络抖动:下轮重试
    }
    if (this.disposed) return // 轮询在途期间已切换会话
    this.mergePersistedEvents(Array.isArray(r.events) ? r.events : [])
    if (r.inProgress !== true) {
      this.stopPolling()
      this.host.recovering.value = false
      if (this.host.status.value === 'running') this.host.status.value = 'done'
      await this.host.fetchSessions().catch(() => {}) // 标题/用量已变,刷新列表(失败不阻断)
    }
  }

  /** 合并一批已落盘条目(带 seq):先按 seq 去重(已并入过的时间线条目跳过,
   * 防并发/重叠拉取把 detection 等重复追加),再推进水位;user 条目命中乐观
   * steer 账时绑定真实 seq 并跳过(本地条目已在),其余 markRaw 压入时间线
   * (落盘条目不再变更)。 */
  private mergePersistedEvents(events: Array<{ seq?: unknown; entry?: unknown }>) {
    for (const ev of events) {
      const seq = typeof ev.seq === 'number' ? ev.seq : 0
      if (seq > 0) {
        if (this.mergedSeqs.has(seq)) continue // 已在时间线(水位回退/在途请求重叠):跳过
        this.mergedSeqs.add(seq)
      }
      if (seq > this.persistedSeq) this.persistedSeq = seq
      const mapped = mapHistoryEntry(ev.entry, seq > 0 ? seq : undefined)
      if (mapped === null) continue
      if (mapped.kind === 'user' && this.bindPendingSteer(seq)) continue
      this.host.entries.value.push(markRaw(mapped))
      if (mapped.kind === 'approval' && !mapped.decision && !mapped.stale) {
        this.host.status.value = 'awaiting_approval'
      }
    }
  }

  /** steer 乐观账出队:FIFO 取一个待绑定条目,把落盘 seq 写回它(它已在时间线,
   * 落盘版跳过插入)并占住该 seq。队列空说明是他端插入,返回 false。 */
  private bindPendingSteer(seq: number): boolean {
    const id = this.pendingSteerIds.shift()
    if (id === undefined) return false
    const local = this.host.entries.value.find((e) => e.id === id)
    if (local && local.kind === 'user' && seq > 0) {
      local.seq = seq
      this.mergedSeqs.add(seq)
    }
    return true
  }

  /* ---- SSE 流式轮次 ---- */

  /** 发起一轮 /chat SSE;input 给定则先压 user 条目并更新重试快照(撤回定位用
   * 落盘水位+1 作 seq),不给则用快照重跑(失败重试,不重复压条目)。 */
  async runTurn(input?: string, opts: SendOptions = {}) {
    if (input !== undefined) {
      this.lastInput = input
      this.lastVideoPath = opts.videoPath || undefined
      this.lastImages = opts.images?.length ? [...opts.images] : undefined
      this.persistedSeq += 1
      // 本地乐观条目占住该落盘序号:并发/重叠的 events 拉取按 seq 去重不重复插入
      this.mergedSeqs.add(this.persistedSeq)
      this.host.entries.value.push({
        id: nextEntryId(),
        kind: 'user',
        text: input,
        at: Date.now(),
        seq: this.persistedSeq,
        ...(this.lastVideoPath ? { videoPath: this.lastVideoPath } : {}),
        ...(opts.videoSrc ? { videoSrc: opts.videoSrc } : {}),
        ...(this.lastImages ? { images: this.lastImages } : {}),
      })
    }
    const seq = ++this.turnSeq
    const { sessionId, status, error } = this.host
    status.value = 'running'
    error.value = null
    this.ctrl = new AbortController()
    try {
      let res: Response
      try {
        res = await fetch('/api/agent/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sessionId: sessionId.value,
            input: this.lastInput,
            ...(this.lastVideoPath ? { videoPath: this.lastVideoPath } : {}),
            ...(this.lastImages ? { images: this.lastImages } : {}),
          }),
          signal: this.ctrl.signal,
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
        if (this.disposed) return
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
            this.handleEvent(ev)
          }
        }
      }
      // 流结束但没收到 done(代理中断等):停在运行态会卡死 UI,按失败处理
      if (!this.disposed && (status.value === 'running' || status.value === 'awaiting_approval')) {
        status.value = 'failed'
        error.value = 'SSE 流异常中断,未收到完成事件'
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        // stop()/dispose 主动中断:静默收尾,已收到内容保留在条目里;
        // 已被替换的旧实例不写状态(切换/新建会话场景)
        if (!this.disposed && seq === this.turnSeq) status.value = 'done'
        return
      }
      if (!this.disposed) {
        status.value = 'failed'
        error.value = (e as Error).message
      }
    } finally {
      if (seq === this.turnSeq) this.ctrl = null
    }
  }

  /** 失败重试:用上次输入重跑一轮(不重复压 user 条目)。 */
  async retryTurn() {
    if (!this.lastInput) return
    await this.runTurn()
  }

  /** 进行中插话:POST /steer,成功后乐观插入带「已插话」标记的 user 条目并入
   * 乐观账(SSE steer 事件/轮询补齐按 id 队列去重)。409 no_active_turn 表示
   * 轮次恰好结束:停轮询收尾恢复态,返回 false 由调用方回退正常 /chat;其他
   * 错误原样抛。 */
  async steer(input: string, opts: SendOptions): Promise<boolean> {
    const { sessionId, recovering } = this.host
    try {
      await postJson(`/api/agent/sessions/${sessionId.value}/steer`, {
        input,
        ...(opts.videoPath ? { videoPath: opts.videoPath } : {}),
        ...(opts.images?.length ? { images: opts.images } : {}),
      })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 恢复态下轮次已结束:停轮询收尾,随后走正常 /chat 重连实时流
        this.stopPolling()
        recovering.value = false
        return false
      }
      throw e
    }
    if (this.disposed) return true // 在途期间会话已切换:新会话不接管旧插话
    const id = nextEntryId()
    this.pendingSteerIds.push(id)
    this.host.entries.value.push({
      id,
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
  async cancel() {
    const { sessionId, recovering } = this.host
    if (!sessionId.value) return
    try {
      await postJson(`/api/agent/sessions/${sessionId.value}/cancel`, {})
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        await this.pollEvents()
        return
      }
      throw e
    }
    // 恢复态(本地无流)收不到 done:主动拉齐一次;服务端收尾有延迟时
    // inProgress 仍为 true,由在跑的轮询继续补齐直至结束
    if (recovering.value) await this.pollEvents()
  }

  /** 流式中追加文本/思考:最后一个条目是 assistant 就续写,否则开新气泡
   * (工具气泡会自然切断前后两段 assistant 文本)。 */
  private currentAssistant(): AgentAssistantEntry {
    const entries = this.host.entries.value
    const last = entries[entries.length - 1]
    if (last?.kind === 'assistant') return last
    const entry: AgentAssistantEntry = {
      id: nextEntryId(),
      kind: 'assistant',
      text: '',
      think: '',
      at: Date.now(),
    }
    entries.push(entry)
    return entry
  }

  private handleEvent(ev: Record<string, unknown>) {
    // 每个事件都带 seq(发出时后端已落盘条数):单调推进落盘水位
    if (typeof ev.seq === 'number' && ev.seq > this.persistedSeq) this.persistedSeq = ev.seq
    const { entries, status, error, usedTokens, maxTokens } = this.host
    if (ev.type === 'text_delta') {
      this.currentAssistant().text += String(ev.text ?? '')
    } else if (ev.type === 'think_delta') {
      this.currentAssistant().think += String(ev.text ?? '')
    } else if (ev.type === 'tool_call_start') {
      const call = ev.call as { id?: unknown; name?: unknown; arguments?: unknown } | undefined
      entries.value.push({
        id: nextEntryId(),
        kind: 'tool',
        callId: String(call?.id ?? ''),
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
        at: Date.now(),
      })
    } else if (ev.type === 'tool_result') {
      const callId = String(ev.toolCallId ?? '')
      const tool = [...entries.value]
        .reverse()
        .find((e): e is AgentToolEntry => e.kind === 'tool' && e.callId === callId)
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
      const callId = String(ev.toolCallId ?? '')
      const tool = [...entries.value]
        .reverse()
        .find((e): e is AgentToolEntry => e.kind === 'tool' && e.callId === callId)
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
      const preview =
        ev.preview && typeof ev.preview === 'object'
          ? ({
              language: String((ev.preview as Record<string, unknown>).language ?? 'text'),
              content: String((ev.preview as Record<string, unknown>).content ?? ''),
              truncated: (ev.preview as Record<string, unknown>).truncated === true,
            } as AgentPreviewContent)
          : undefined
      entries.value.push({
        id: nextEntryId(),
        kind: 'approval',
        requestId: String(ev.requestId ?? ''),
        toolName: String(ev.toolName ?? ''),
        approvalRule: String(ev.approvalRule ?? ''),
        ...(ev.description != null ? { description: String(ev.description) } : {}),
        accesses: Array.isArray(ev.accesses) ? (ev.accesses as AgentAccess[]) : [],
        ...(preview ? { preview } : {}),
        at: Date.now(),
      })
      status.value = 'awaiting_approval'
    } else if (ev.type === 'detection') {
      entries.value.push({
        id: nextEntryId(),
        kind: 'detection',
        data: mapDetectionData(ev.data),
        at: Date.now(),
      })
    } else if (ev.type === 'steer') {
      // 进行中插话生效:本地已乐观插入(发 /steer 成功时)则按 id 队列出队绑定
      // 落盘 seq 后跳过;否则(他端插入)补一条带「已插话」标记的 user 条目。
      // 同批多条 steer 共享事件水位(seq=批末位置),按剩余排队数回推各自序号。
      if (this.pendingSteerIds.length) {
        const id = this.pendingSteerIds.shift()!
        const local = entries.value.find((e) => e.id === id)
        if (
          local &&
          local.kind === 'user' &&
          typeof ev.seq === 'number' &&
          ev.seq > this.pendingSteerIds.length
        ) {
          local.seq = ev.seq - this.pendingSteerIds.length
          this.mergedSeqs.add(local.seq) // 占住落盘序号:轮询重放不重复绑定/插入
        }
      } else {
        entries.value.push({
          id: nextEntryId(),
          kind: 'user',
          text: String(ev.text ?? ''),
          steered: true,
          at: Date.now(),
          ...(typeof ev.seq === 'number' ? { seq: ev.seq } : {}),
          ...(Array.isArray(ev.images) && ev.images.length
            ? { images: ev.images.map(String) }
            : {}),
          ...(ev.videoPath != null ? { videoPath: String(ev.videoPath) } : {}),
        })
      }
    } else if (ev.type === 'steer_dropped') {
      // 轮末未消费的插话:服务端从未落盘,乐观条目移出时间线并出队,
      // 保持本地与后端落盘逐条对齐(按文本在乐观账中定位)。
      const text = String(ev.text ?? '')
      const idx = this.pendingSteerIds.findIndex((id) => {
        const e = entries.value.find((x) => x.id === id)
        return e?.kind === 'user' && e.text === text
      })
      if (idx >= 0) {
        const [id] = this.pendingSteerIds.splice(idx, 1)
        const i = entries.value.findIndex((e) => e.id === id)
        if (i >= 0) entries.value.splice(i, 1)
      }
    } else if (ev.type === 'generate_done') {
      // 该步 LLM 调用耗时:写到当前步的 assistant 条目(最后一条);本步无
      // 可见文本/思考(纯工具调用步)则不建空气泡,直接丢弃。
      const last = entries.value[entries.value.length - 1]
      const ms = Number(ev.generateMs)
      if (last?.kind === 'assistant' && Number.isFinite(ms) && ms >= 0) last.generateMs = ms
    } else if (ev.type === 'generate_retry') {
      // 服务端工具解析 400 的自动恢复(仅本地提示,不落盘不占 seq)。
      entries.value.push({
        id: nextEntryId(),
        kind: 'system',
        text: '工具调用参数被服务端解析器拒绝(400),已自动重试',
        tone: 'warn',
        at: Date.now(),
      })
    } else if (ev.type === 'context_usage') {
      const used = Number(ev.usedTokens)
      const max = Number(ev.maxTokens)
      if (Number.isFinite(used) && used >= 0) usedTokens.value = used
      if (Number.isFinite(max) && max > 0) maxTokens.value = max
    } else if (ev.type === 'compaction') {
      entries.value.push({ id: nextEntryId(), kind: 'system', text: '上下文已自动压缩', at: Date.now() })
    } else if (ev.type === 'done') {
      // 落盘水位兜底:done 的 seq 在异常路径先于轮末兜底落盘,以本地非 system
      // 条目数对齐(本地与后端逐条对齐是既有契约;见 rewindTo/seq 注释)。
      const localCount = entries.value.filter((e) => e.kind !== 'system').length
      if (localCount > this.persistedSeq) this.persistedSeq = localCount
      if (ev.reason === 'error') {
        status.value = 'failed'
        error.value = typeof ev.error === 'string' ? ev.error : 'Agent 运行出错'
      } else {
        status.value = 'done'
        // 输出达到 token 上限被截断:时间线末尾插警示条目(仅本地,不落历史)
        if (ev.truncated === true) {
          entries.value.push({
            id: nextEntryId(),
            kind: 'system',
            text: '输出达到 token 上限被截断,部分内容可能不完整,可继续追问',
            tone: 'warn',
            at: Date.now(),
          })
        }
      }
    }
    // step_done:无独立 UI(工具批结束的进度信号,时间线条目已自解释)
  }
}
