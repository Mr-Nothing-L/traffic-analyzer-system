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
  fetchForgeResult,
  jobEventsUrl,
} from '../api/sftforge'
import type {
  CreateForgeJobRequest,
  ForgeResultPayload,
  ForgeVideoSummary,
  JobFailedData,
  RoundCompletedData,
  RoundStartedData,
  RoundTrace,
} from '../api/sftforge'
import type { EventDef } from '../sft/types'

export type ForgeStatus = 'idle' | 'running' | 'done' | 'failed'

/** 单视频的一轮:running=已开推待回填;done=trace 已到(含 prompt/response)。 */
export interface ForgeRoundEntry {
  round_no: number
  kind: string
  event_ids: number[]
  state: 'running' | 'done'
  trace: RoundTrace | null
}

/** 单视频进度:逐轮卡片 + 完成摘要(match/quarantine 徽标来源)。 */
export interface ForgeVideoState {
  video: string
  rounds: ForgeRoundEntry[]
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
      v = { video, rounds: [], summary: null }
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
    })
  }

  function onRoundCompleted(d: RoundCompletedData) {
    const v = videoStateOf(d.video)
    const t = d.trace
    const entry = v.rounds.find((r) => r.round_no === t.round_no && r.kind === t.kind)
    if (entry) {
      entry.state = 'done'
      entry.trace = t
      entry.event_ids = [...t.event_ids]
    } else {
      // 重连回放只拿到 completed(极端乱序)也落地为完成态卡片
      v.rounds.push({
        round_no: t.round_no,
        kind: t.kind,
        event_ids: [...t.event_ids],
        state: 'done',
        trace: t,
      })
    }
  }

  function onVideoCompleted(d: ForgeVideoSummary) {
    videoStateOf(d.video).summary = d
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
      videos.value = r.videos.map((v) => ({ video: v, rounds: [], summary: null }))
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
    // 测试与视图复用的纯落地逻辑(组件不直接调,经 SSE 回调驱动)
    onRoundStarted,
    onRoundCompleted,
    onVideoCompleted,
  }
})
