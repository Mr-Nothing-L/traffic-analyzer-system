/** SFT Forge(多轮纠错 SFT label 生成)API 封装。
 * 契约见 traffic_analyzer/web/sftforge/(routes.py + service.py):
 * - POST /api/sftforge/jobs({dir 或 videos[], max_rounds} → {job_id, videos});
 * - GET /api/sftforge/jobs/{id}/events(逐任务 SSE,命名事件 + JSON data,
 *   先回放历史再收增量,终态 job_completed/job_failed 后服务端关流);
 * - GET /api/sftforge/jobs/{id}/result(status/stats/videos 摘要/jsonl 训练行/
 *   quarantine 列表);
 * - GET/DELETE /api/sftforge/store(当前工作区持久化结果的读/删)。
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
  /** 思考档位(off/low/medium/high,默认 medium;off=关闭隐藏 reasoning)。 */
  thinking?: string
  /** 初稿链路(pipeline=批量推理链路[默认];prompt=单次 forward 盲推)。 */
  draft?: string
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
  /** 隐藏 reasoning 通道文本(thinking 开启且端点返回 reasoning_content 时)。 */
  reasoning?: string
  patched: boolean
}

/** 逐视频摘要(video_completed 事件载荷 / result.videos 条目)。 */
export interface ForgeVideoSummary {
  video: string
  match: boolean
  skipped: boolean
  /** R0(正向盲推)是否已与真值一致:false 表示经纠错轮「修改后一致」;
   * 历史结果缺该字段,前端以 rounds.some(round_no>0 && patched) 回退判定。 */
  r0_match?: boolean | null
  reason: string | null
  gt: number[]
  rounds: number
  /** 最终 <think>/<answer> 文本(最终结论视图数据源;跳过/失败时可能为空)。 */
  final_output?: string
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
  /** match 中经纠错轮修订才一致的视频数(「修改后一致」;历史结果可能缺)。 */
  corrected?: number
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

/** 持久化的单轮精简信息(轮次号/类型/目标事件/是否合并修改),
 * 用于恢复视图的「第 N 轮修正」标注(patchedRoundsOf 经回填的最小 trace 读取)。 */
export interface ForgeStoredRound {
  round_no: number
  kind: string
  event_ids: number[]
  patched: boolean
}

/** GET /api/sftforge/store 响应:任务终态按工作区落盘的结果
 * (<workspace>/.agent/sftforge/latest_result.json,见 web/sftforge/store.py)。 */
export interface ForgeStoredResult extends ForgeResultPayload {
  /** 落盘时间(本地时区 ISO 分钟级)。 */
  saved_at?: string
  /** 视频路径 → 轮次精简信息。 */
  rounds?: Record<string, ForgeStoredRound[]>
}

/* ---------- SSE 事件载荷(命名事件 + JSON data) ---------- */

export interface RoundStartedData {
  video: string
  round_no: number
  kind: string
  event_ids: number[]
}

/** 流式增量(round_delta 事件):reasoning/response 追加;text 为增量。
 * field=reset 时清空该轮流式缓冲(重试/failover 作废重推)。 */
export interface RoundDeltaData {
  video: string
  round_no: number
  kind: string
  field: 'reasoning' | 'response' | 'reset'
  text: string
}

/** 初稿阶段(批量链路)步骤进度事件(draft_step):管线进度 JSONL 原样
 * 透传(见 utils/progress.py),type ∈ register/start/phase/lane_done/step/done。 */
export interface DraftStepData {
  video: string
  type?: string
  lane?: string
  label?: string
  step?: number
  total?: number
  name?: string
  result?: string
  done?: number
  ts?: number
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

/** 读当前工作区的持久化结果(无缓存时后端 404,调用方按 ApiError 处理)。 */
export function fetchStoredForgeResult(): Promise<ForgeStoredResult> {
  return apiFetch<ForgeStoredResult>('/sftforge/store')
}

/** 删除当前工作区的持久化结果缓存。 */
export function deleteStoredForgeResult(): Promise<{ deleted: boolean }> {
  return apiFetch<{ deleted: boolean }>('/sftforge/store', { method: 'DELETE' })
}

/** 逐任务 SSE 地址(EventSource 用,带 /api 前缀)。 */
export function jobEventsUrl(jobId: number): string {
  return `/api/sftforge/jobs/${jobId}/events`
}
