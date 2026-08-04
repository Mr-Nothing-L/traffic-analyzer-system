/** 推理任务状态:首拉 /api/jobs + SSE 增量;infer/retry/cancel action。
 * 迁移自 legacy jobs.js(去掉 mock 轮询体系,v2 仅 SSE 驱动)。 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, apiFetch } from '../api/client'

/** 专家泳道(泳道面板数据源:job.progress 快照,见 web/progress.py / jobs/job.py to_dict)。 */
export interface ExpertLane {
  name: string
  status: string // queued / running / done / error
  detected: boolean | null // done 时的检出语义;裁决/阶段泳道恒 null
  fraction: number
  label: string
}

export interface JobProgress {
  fraction: number | null
  step_label?: string
  step_index?: number
  experts?: ExpertLane[]
}

export interface Job {
  id: number
  kind: string
  stem?: string | null
  rel?: string | null
  status: string // queued / running / done / failed
  progress?: JobProgress | null
  returncode?: number | null
}

/** action 结果:组件据此弹提示(409 需友好文案,见 TreeNode/TreeView)。 */
export interface ActionResult {
  ok: boolean
  status?: number
  message?: string
}

export const useJobsStore = defineStore('jobs', () => {
  const jobs = ref<Job[]>([])
  const inferPosting = ref(false) // 提交防抖(legacy inferPosting)

  /** stem → 最新 infer 任务索引(jobs 变更时重建;latestJobForStem 走 O(1))。 */
  const latestJobByStem = computed(() => {
    const m = new Map<string, Job>()
    for (const j of jobs.value) {
      if (j.kind === 'infer' && j.stem) m.set(j.stem, j) // 列表尾部最新,后者覆盖前者
    }
    return m
  })

  /** 同 stem 取最新一条 infer 任务。 */
  function latestJobForStem(stem: string): Job | null {
    return latestJobByStem.value.get(stem) ?? null
  }

  /** 是否有运行中/排队中的 infer(「开始推理」禁用条件之一)。 */
  const hasActiveInfer = computed(() =>
    jobs.value.some((j) => j.kind === 'infer' && (j.status === 'running' || j.status === 'queued')),
  )

  /** 进入页面/提交推理后全量对齐一次;之后由 SSE 增量驱动。 */
  async function pollJobs() {
    try {
      jobs.value = (await apiFetch<Job[]>('/jobs')) || []
    } catch {
      // 后端未就绪:静默,交由 SSE 重连对齐
    }
  }

  /** SSE 快照落库:按 id 更新/插入;终态后忽略迟到的非终态快照(同 legacy)。 */
  function applyJob(job: Job) {
    const cur = jobs.value.find((j) => j.id === job.id)
    if (
      cur &&
      (cur.status === 'done' || cur.status === 'failed') &&
      job.status !== 'done' &&
      job.status !== 'failed'
    ) {
      return
    }
    if (cur) Object.assign(cur, job)
    else jobs.value.push(job)
  }

  /* ---- 进度节流:非终态快照 ~100ms 合并落库(同 job 只留最新一帧),避免高频 SSE 驱动整树重渲染 ---- */
  const PROGRESS_FLUSH_MS = 100
  const pendingJobs = new Map<number, Job>() // 待落库快照(job id → 最新一帧)
  let flushTimer: ReturnType<typeof setTimeout> | null = null

  function flushPending() {
    flushTimer = null
    pendingJobs.forEach(applyJob)
    pendingJobs.clear()
  }

  /** SSE 入口:终态立即落库(并丢弃该 job 待合并帧);非终态并入节流窗口。 */
  function onJobEvent(job: Job) {
    if (!job || job.id == null) return
    if (job.status === 'done' || job.status === 'failed') {
      pendingJobs.delete(job.id)
      applyJob(job)
      return
    }
    pendingJobs.set(job.id, job)
    if (!flushTimer) flushTimer = setTimeout(flushPending, PROGRESS_FLUSH_MS)
  }

  async function postInfer(rels: string[]): Promise<ActionResult> {
    if (!rels.length || inferPosting.value) return { ok: false }
    inferPosting.value = true
    try {
      await apiFetch('/infer', { method: 'POST', body: JSON.stringify({ rels }) })
      await pollJobs() // 提交后立即对齐一次;后续进度由 SSE 推送
      return { ok: true }
    } catch (e) {
      return {
        ok: false,
        status: e instanceof ApiError ? e.status : 0,
        message: e instanceof Error ? e.message : String(e),
      }
    } finally {
      inferPosting.value = false
    }
  }

  /** 「开始推理」:对全部勾选视频提交。 */
  function startInfer(rels: string[]) {
    return postInfer(rels)
  }

  /** 失败任务 ↻ 重试:仅对该视频重新提交。 */
  function retryInfer(rel: string) {
    return postInfer([rel])
  }

  /** 「■ 停止」:成功后本地立即标记失败态,避免徽标在下次对齐前闪跳(同 legacy)。 */
  async function cancelJob(id: number): Promise<ActionResult> {
    try {
      await apiFetch(`/jobs/${encodeURIComponent(String(id))}/cancel`, { method: 'POST' })
    } catch (e) {
      return {
        ok: false,
        status: e instanceof ApiError ? e.status : 0,
        message: e instanceof Error ? e.message : String(e),
      }
    }
    const job = jobs.value.find((j) => j.id === id)
    if (job && (job.status === 'running' || job.status === 'queued')) {
      job.status = 'failed'
      job.returncode = -15
      job.progress = { ...(job.progress || { fraction: null }), step_label: '已停止' }
    }
    await pollJobs() // 立即对齐后端真实状态
    return { ok: true }
  }

  return {
    jobs, inferPosting, hasActiveInfer,
    latestJobForStem, pollJobs, onJobEvent, startInfer, retryInfer, cancelJob,
  }
})
