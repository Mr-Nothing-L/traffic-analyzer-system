/** SFT Forge(多轮纠错 SFT label 生成)API 封装。
 * 契约见 traffic_analyzer/web/sftforge/(routes.py + service.py):
 * - POST /api/sftforge/jobs({dir 或 videos[], max_rounds} → {job_id, videos});
 * - GET /api/sftforge/jobs/{id}/events(逐任务 SSE,命名事件 + JSON data,
 *   先回放历史再收增量,终态 job_completed/job_failed 后服务端关流);
 * - GET /api/sftforge/jobs/{id}/result(status/stats/videos 摘要/jsonl 训练行/
 *   quarantine 列表)。
 * SSE 不走 apiFetch:调用方直接 new EventSource(jobEventsUrl(id))。 */
import { apiFetch } from './client'

/* ---------- 请求/响应契约 ---------- */

export interface CreateForgeJobRequest {
  /** 取目录下全部视频;与 videos 可同时给(后端合并去重),至少其一。 */
  dir?: string
  /** 视频绝对路径列表。 */
  videos?: string[]
  /** 最大纠错轮次(0-10,默认 2;0 = 仅正向盲推)。 */
  max_rounds?: number
}

export interface CreateForgeJobResponse {
  job_id: number
  videos: string[]
}

/** 单次 VLM 调用轨迹(RoundTrace.model_dump(),见 sftforge/models.py)。
 * round_no=0 为正向盲推,1..max_rounds 为纠错轮次。 */
export interface RoundTrace {
  round_no: number
  kind: string // forward / correct_missing / review_extra
  event_ids: number[]
  prompt: string
  response: string
  patched: boolean
}

/** 逐视频摘要(video_completed 事件载荷 / result.videos 条目)。 */
export interface ForgeVideoSummary {
  video: string
  match: boolean
  skipped: boolean
  reason: string | null
  gt: number[]
  rounds: number
}

export interface ForgeQuarantineItem {
  video: string
  reason: string | null
  gt: number[]
}

export interface ForgeJobStats {
  match: number
  quarantine: number
  skipped: number
}

/** 训练 JSONL 行(exporter.build_train_row;内容原样透传,导出时逐行序列化)。 */
export type ForgeTrainRow = Record<string, unknown>

/** GET /result 响应(job_result_payload)。 */
export interface ForgeResultPayload {
  job_id: number
  status: string // queued / running / done / failed
  error: string | null
  stats: ForgeJobStats
  videos: ForgeVideoSummary[]
  jsonl: ForgeTrainRow[]
  quarantine: ForgeQuarantineItem[]
}

/* ---------- SSE 事件载荷(命名事件 + JSON data) ---------- */

export interface RoundStartedData {
  video: string
  round_no: number
  kind: string
  event_ids: number[]
}

export interface RoundCompletedData {
  video: string
  trace: RoundTrace
}

export type VideoCompletedData = ForgeVideoSummary

export interface JobCompletedData {
  stats: ForgeJobStats
}

export interface JobFailedData {
  error: string
}

/* ---------- 端点封装 ---------- */

export function createForgeJob(body: CreateForgeJobRequest): Promise<CreateForgeJobResponse> {
  return apiFetch<CreateForgeJobResponse>('/sftforge/jobs', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function fetchForgeResult(jobId: number): Promise<ForgeResultPayload> {
  return apiFetch<ForgeResultPayload>(`/sftforge/jobs/${jobId}/result`)
}

/** 逐任务 SSE 地址(EventSource 用,带 /api 前缀)。 */
export function jobEventsUrl(jobId: number): string {
  return `/api/sftforge/jobs/${jobId}/events`
}
