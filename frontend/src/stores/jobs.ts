/** 推理任务状态:首拉 /api/jobs + SSE 增量;infer/retry/cancel action。
 * 迁移自 legacy jobs.js(去掉 mock 轮询体系,v2 仅 SSE 驱动)。 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, apiFetch } from '../api/client'

export interface JobProgress {
  fraction: number | null
  step_label?: string
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

  /** 同 stem 取最新一条 infer 任务(列表尾部最新)。 */
  function latestJobForStem(stem: string): Job | null {
    for (let i = jobs.value.length - 1; i >= 0; i--) {
      const j = jobs.value[i]
      if (j.kind === 'infer' && j.stem === stem) return j
    }
    return null
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
  function onJobEvent(job: Job) {
    if (!job || job.id == null) return
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
