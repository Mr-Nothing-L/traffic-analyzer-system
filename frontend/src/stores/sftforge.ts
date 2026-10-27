/** SFT Forge 任务状态(多轮纠错 SFT label 生成面板):
 * 生命周期 idle → running(创建任务 + EventSource 逐任务 SSE)→ done/failed。
 * round_started 先占位「进行中」轮次卡,round_completed 回填完整 RoundTrace
 * (prompt/response/patched);video_completed 落定 match/skipped/quarantine
 * 徽标;终态事件后关流并拉 /result 汇总(统计 + 训练 JSONL + quarantine)。
 * 事件全部幂等落地(按 round_no+kind 去重复写):SSE 断线重连时服务端会
 * 回放历史快照(见 web/sftforge/routes.py),重复事件不产生重复卡片。
 * 事件 id → 中文名映射复用 /api/config/events(与 SFT 编辑器同源)。 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '../api/client'
import { getEventConfig } from '../api/results'
import {
  createForgeJob,
  deleteStoredForgeResult,
  fetchForgeResult,
  fetchStoredForgeResult,
  jobEventsUrl,
} from '../api/sftforge'
import type {
  CreateForgeJobRequest,
  DraftStepData,
  ForgeResultPayload,
  ForgeVideoSummary,
  JobFailedData,
  RoundCompletedData,
  RoundDeltaData,
  RoundStartedData,
  RoundTrace,
} from '../api/sftforge'
import type { EventDef } from '../sft/types'

export type ForgeStatus = 'idle' | 'running' | 'done' | 'failed'

/** 单视频的一轮:running=已开推待回填;done=trace 已到(含 prompt/response)。
 * streamReasoning/streamResponse 为流式增量缓冲(round_delta 事件追加,
 * round_completed 回填 trace 后清空)。 */
export interface ForgeRoundEntry {
  round_no: number
  kind: string
  event_ids: number[]
  state: 'running' | 'done'
  trace: RoundTrace | null
  streamReasoning: string
  streamResponse: string
}

/** 单视频进度:逐轮卡片 + 初稿步骤(批量链路)+ 完成摘要(match/quarantine 徽标来源)。 */
export interface ForgeVideoState {
  video: string
  rounds: ForgeRoundEntry[]
  /** 初稿(批量链路)步骤进度事件(draft_step;空数组=非批量链路或未到初稿)。 */
  draftSteps: DraftStepData[]
  summary: ForgeVideoSummary | null
}

export const useSftForgeStore = defineStore('sftforge', () => {
  const status = ref<ForgeStatus>('idle')
  const error = ref<string | null>(null)
  const jobId = ref<number | null>(null)
  const videos = ref<ForgeVideoState[]>([])
  const result = ref<ForgeResultPayload | null>(null)
  const eventNames = ref<Record<number, string>>({})
  const posting = ref(false) // 创建请求在途(提交按钮 loading)

  let source: EventSource | null = null

  /* ---- 事件类别名(懒加载一次,与 SFT 编辑器同一配置源) ---- */
  async function ensureEventNames(): Promise<void> {
    if (Object.keys(eventNames.value).length) return
    const defs = await getEventConfig<EventDef[]>()
    const names: Record<number, string> = {}
    for (const ev of defs || []) names[ev.event_id] = ev.name_zh
    eventNames.value = names
  }

  /** 轮次目标事件中文名(标签展示 + 修复段落高亮的命中集合)。 */
  function targetNamesOf(eventIds: number[]): Set<string> {
    const names = new Set<string>()
    for (const id of eventIds) {
      const n = eventNames.value[id]
      if (n) names.add(n)
    }
    return names
  }

  function videoStateOf(video: string): ForgeVideoState {
    let v = videos.value.find((x) => x.video === video)
    if (!v) {
      v = { video, rounds: [], draftSteps: [], summary: null }
      videos.value.push(v)
    }
    return v
  }

  /* ---- SSE 事件落地(全部幂等:重连回放不产生重复) ---- */
  function onRoundStarted(d: RoundStartedData) {
    const v = videoStateOf(d.video)
    const dup = v.rounds.some((r) => r.round_no === d.round_no && r.kind === d.kind)
    if (dup) return
    v.rounds.push({
      round_no: d.round_no,
      kind: d.kind,
      event_ids: [...d.event_ids],
      state: 'running',
      trace: null,
      streamReasoning: '',
      streamResponse: '',
    })
  }

  /** 流式增量:reasoning/response 追加;reset 清空(重试/failover 作废重推)。
   * 回放时 delta 先于 started 到达也容忍(就地占位)。 */
  function onRoundDelta(d: RoundDeltaData) {
    const v = videoStateOf(d.video)
    let entry = v.rounds.find((r) => r.round_no === d.round_no && r.kind === d.kind)
    if (!entry) {
      entry = {
        round_no: d.round_no,
        kind: d.kind,
        event_ids: [],
        state: 'running',
        trace: null,
        streamReasoning: '',
        streamResponse: '',
      }
      v.rounds.push(entry)
    }
    if (d.field === 'reset') {
      entry.streamReasoning = ''
      entry.streamResponse = ''
    } else if (d.field === 'reasoning') {
      entry.streamReasoning += d.text
    } else if (d.field === 'response') {
      entry.streamResponse += d.text
    }
  }

  function onRoundCompleted(d: RoundCompletedData) {
    const v = videoStateOf(d.video)
    const t = d.trace
    const entry = v.rounds.find((r) => r.round_no === t.round_no && r.kind === t.kind)
    if (entry) {
      entry.state = 'done'
      entry.trace = t
      entry.event_ids = [...t.event_ids]
      entry.streamReasoning = ''
      entry.streamResponse = ''
    } else {
      // 重连回放只拿到 completed(极端乱序)也落地为完成态卡片
      v.rounds.push({
        round_no: t.round_no,
        kind: t.kind,
        event_ids: [...t.event_ids],
        state: 'done',
        trace: t,
        streamReasoning: '',
        streamResponse: '',
      })
    }
  }

  function onVideoCompleted(d: ForgeVideoSummary) {
    videoStateOf(d.video).summary = d
  }

  /** 初稿(批量链路)步骤进度:按内容去重(回放不产生重复行),追加到视频。 */
  function onDraftStep(d: DraftStepData) {
    const v = videoStateOf(d.video)
    const key = `${d.type}|${d.lane || ''}|${d.label || ''}|${d.step ?? ''}|${d.result || ''}`
    if (v.draftSteps.some((s) => `${s.type}|${s.lane || ''}|${s.label || ''}|${s.step ?? ''}|${s.result || ''}` === key)) return
    v.draftSteps.push(d)
  }

  function closeSource() {
    if (source) {
      source.close()
      source = null
    }
  }

  async function finish(ok: boolean, failError?: string) {
    closeSource()
    status.value = ok ? 'done' : 'failed'
    if (!ok) error.value = failError || '任务失败'
    if (jobId.value != null) {
      try {
        result.value = await fetchForgeResult(jobId.value)
        if (ok) error.value = null
        else if (result.value.error) error.value = result.value.error
      } catch {
        // 结果拉取失败不遮蔽任务终态:面板仍展示已收到的逐轮过程
      }
    }
  }

  function connect(id: number) {
    closeSource()
    const es = new EventSource(jobEventsUrl(id))
    source = es
    // 事件 data 一律为 JSON(format_sse);解析失败跳过该条,不让单条坏数据中断流
    const parseData = <T,>(ev: unknown): T | null => {
      try {
        return JSON.parse((ev as MessageEvent).data) as T
      } catch {
        return null
      }
    }
    es.addEventListener('round_started', (ev) => {
      const d = parseData<RoundStartedData>(ev)
      if (d) onRoundStarted(d)
    })
    es.addEventListener('round_delta', (ev) => {
      const d = parseData<RoundDeltaData>(ev)
      if (d) onRoundDelta(d)
    })
    es.addEventListener('draft_step', (ev) => {
      const d = parseData<DraftStepData>(ev)
      if (d) onDraftStep(d)
    })
    es.addEventListener('round_completed', (ev) => {
      const d = parseData<RoundCompletedData>(ev)
      if (d) onRoundCompleted(d)
    })
    es.addEventListener('video_completed', (ev) => {
      const d = parseData<ForgeVideoSummary>(ev)
      if (d) onVideoCompleted(d)
    })
    es.addEventListener('job_completed', () => void finish(true))
    es.addEventListener('job_failed', (ev) => {
      const d = parseData<JobFailedData>(ev)
      void finish(false, d?.error)
    })
    // 兜底:CLOSED = 连接被永久关闭(非浏览器自动重连的 CONNECTING),
    // 不置 failed 则 UI 假死在「进行中」;结果仍尝试拉 /result 对齐服务端真实状态
    es.onerror = () => {
      if (source !== es) return // 已主动断开或被新连接替换
      if (es.readyState === EventSource.CLOSED) {
        void finish(false, 'SSE 连接已断开;任务结果可能仍在服务端生成,请稍后用结果接口核对')
      }
    }
  }

  /** 创建任务并连接 SSE。返回 ok 供视图提示;错误文案入 error。 */
  async function start(body: CreateForgeJobRequest): Promise<boolean> {
    if (posting.value || status.value === 'running') return false
    posting.value = true
    error.value = null
    try {
      await ensureEventNames()
      const r = await createForgeJob(body)
      reset()
      jobId.value = r.job_id
      // 先占位视频列表(创建响应即最终处理清单),轮次随 SSE 生长
      videos.value = r.videos.map((v) => ({ video: v, rounds: [], draftSteps: [], summary: null }))
      status.value = 'running'
      connect(r.job_id)
      return true
    } catch (e) {
      error.value =
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
      return false
    } finally {
      posting.value = false
    }
  }

  /** 路由切换回来后兜底:任务仍在跑但 SSE 已断(浏览器/网络原因)时重连,
   * 服务端回放历史事件(见 routes.py),状态无感续上。已连接或不在跑则 no-op。 */
  function ensureConnected() {
    if (status.value === 'running' && jobId.value != null && !source) {
      connect(jobId.value)
    }
  }

  /** 从工作区持久化结果恢复视图(服务重启/重进面板;缓存由任务终态落盘,
   * 重新推理覆盖)。仅 idle 且无视频时尝试;无缓存(404)/读取失败静默 no-op。
   * 轮次以最小 trace 回填(prompt/response 未持久化,展开仅见轮次头与
   * patched 徽标;「第 N 轮修正」标注所需的 patched/event_ids 齐全)。 */
  async function restore(): Promise<boolean> {
    if (status.value !== 'idle' || videos.value.length) return false
    let stored
    try {
      stored = await fetchStoredForgeResult()
    } catch {
      return false
    }
    try {
      await ensureEventNames()
    } catch {
      // 事件名加载失败不阻塞恢复:标签回退「事件{id}」(组件已有兜底)
    }
    videos.value = (stored.videos || []).map((v) => ({
      video: v.video,
      draftSteps: [],
      summary: v,
      rounds: (stored.rounds?.[v.video] || []).map((r) => ({
        round_no: r.round_no,
        kind: r.kind,
        event_ids: [...r.event_ids],
        state: 'done' as const,
        trace: {
          round_no: r.round_no,
          kind: r.kind,
          event_ids: [...r.event_ids],
          prompt: '',
          response: '',
          patched: r.patched,
        },
        streamReasoning: '',
        streamResponse: '',
      })),
    }))
    result.value = {
      job_id: stored.job_id,
      status: stored.status,
      error: stored.error,
      stats: stored.stats,
      videos: stored.videos,
      jsonl: stored.jsonl,
      quarantine: stored.quarantine,
    }
    status.value = stored.status === 'failed' ? 'failed' : 'done'
    error.value = stored.error
    return true
  }

  /** 删除工作区持久化缓存并清空视图(结果面板「删除缓存」按钮)。 */
  async function clearStored(): Promise<void> {
    try {
      await deleteStoredForgeResult()
    } catch {
      // 删除失败则缓存仍在(下次进面板会复活),不能清空视图——只上报错误
      error.value = '删除缓存失败,请重试'
      return
    }
    reset()
  }

  /** 清空任务状态并断开 SSE(新建任务/离开页面)。 */
  function reset() {
    closeSource()
    status.value = 'idle'
    error.value = null
    jobId.value = null
    videos.value = []
    result.value = null
  }

  const running = computed(() => status.value === 'running')
  /** 已出摘要的视频数(进度文案「x / N」)。 */
  const completedCount = computed(() => videos.value.filter((v) => v.summary).length)

  return {
    status,
    error,
    jobId,
    videos,
    result,
    eventNames,
    posting,
    running,
    completedCount,
    targetNamesOf,
    start,
    reset,
    ensureConnected,
    restore,
    clearStored,
    // 测试与视图复用的纯落地逻辑(组件不直接调,经 SSE 回调驱动)
    onRoundStarted,
    onRoundDelta,
    onRoundCompleted,
    onVideoCompleted,
    onDraftStep,
  }
})
