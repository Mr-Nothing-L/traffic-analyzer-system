/** RAG 语义检索 API(契约 POST /api/rag/search)。
 * 检索库未建时后端返回 404,detail 为引导文案(提示先运行建库脚本),调用方原样展示。 */
import { apiFetch } from './client'

export type RagMode = 'text' | 'related' | 'adjacent' | 'site'

export interface RagSearchRequest {
  mode: RagMode
  query?: string
  video?: string // related/adjacent 用:当前视频文件名
  k?: number
  alpha?: number
  only_confirmed?: boolean
  human_edited?: boolean
  gap_s?: number
  direction?: string
  before?: number | null
  after?: number | null
}

export interface RagResult {
  video_path: string // 工作区相对路径(作详情路由 query.rel)
  score: number
  events: number[]
  site: string
  start_ts: number // unix 秒
  duration_s: number
  has_annotation: boolean
  human_edited: boolean
  review_status: string
}

export interface RagSearchResponse {
  results: RagResult[]
  mode: RagMode
  elapsed_ms: number
}

export function searchRag(req: RagSearchRequest): Promise<RagSearchResponse> {
  return apiFetch<RagSearchResponse>('/rag/search', {
    method: 'POST',
    body: JSON.stringify(req),
  })
}

/* ---- 建向量库(契约 POST /api/rag/build + GET /api/rag/build/status + POST /api/rag/build/cancel)----
 * 已在跑时 POST build 返回 409(detail: "build already running"),调用方兜底为直接轮询。 */

export interface RagBuildStart {
  started: boolean
  total: number
}

/** 向量库概况(空闲时 status 也返回;库未建为 null 或 exists=false)。 */
export interface RagLibraryInfo {
  exists: boolean
  count: number
  built_at: number | null // unix 秒
}

export interface RagBuildStatus {
  running: boolean
  done: number
  total: number
  failed: number
  started_at: number | null // unix 秒
  finished_at: number | null
  last_error: string | null
  partial: boolean // 被取消的部分完成
  library: RagLibraryInfo | null
  /** 空闲时:待更新条目数(新视频 + 标注变更需重算);构建中/未知为 null。 */
  pending: number | null
}

/** 启动增量更新(带 refresh_annotations:标注变更的视频也重算向量)。 */
export function startRagBuild(): Promise<RagBuildStart> {
  return apiFetch<RagBuildStart>('/rag/build', {
    method: 'POST',
    body: JSON.stringify({ refresh_annotations: true }),
  })
}

export function getRagBuildStatus(): Promise<RagBuildStatus> {
  return apiFetch<RagBuildStatus>('/rag/build/status')
}

export function cancelRagBuild(): Promise<{ cancelling: boolean }> {
  return apiFetch<{ cancelling: boolean }>('/rag/build/cancel', { method: 'POST' })
}

/** video_path(工作区相对路径)→ 详情路由 stem(basename 去扩展名,与 workspace store 口径一致)。 */
export function stemOfVideoPath(p: string): string {
  const base = p.split('/').pop() ?? p
  return base.replace(/\.[^.]+$/, '')
}

/** start_ts(unix 秒)→ 本地时间串(同 TopBar fmtLoginTime 口径)。 */
export function fmtTs(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '-'
}
