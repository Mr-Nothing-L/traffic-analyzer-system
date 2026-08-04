/** 详情页 API:结果读取 / 证据保存 / 媒体 URL 构造。
 * 契约见 traffic_analyzer/web/evidence/*(results、乐观锁 PUT)与
 * frames.py / video_stream.py(meta/frame/stream)。
 * 媒体 URL 给 <video>/<img> src 用,需带 /api 前缀;apiFetch 的路径不带。 */
import { apiFetch } from './client'

/* ---------- 证据 JSON 结构(与 evidence_schema.py 对应,宽松可选) ---------- */
export interface EvidenceRegion {
  label?: string
  box_rel?: number[] // [x1, y1, x2, y2] 归一化
  frame_index?: number
  image?: string
}

export interface EvidenceCalibration {
  frame_index?: number
  emergency_polygon_rel?: number[][] // 应急车道多边形,归一化顶点
  chevron_polygon_rel?: number[][] // 导流区多边形
}

export interface EvidenceEvent {
  event_id: string | number
  name: string
  detected?: boolean
  calibration?: EvidenceCalibration
  evidence_regions?: EvidenceRegion[]
  gallery_images?: string[]
}

export interface EvidenceVideoInfo {
  file_name?: string
  duration_sec?: number
  fps?: number
  width?: number
  height?: number
}

export interface Evidence {
  schema_version?: string
  video?: EvidenceVideoInfo
  events: EvidenceEvent[]
}

/** SFT 标注(json 结构以标注文档 v4.5 为准,只读展示仅需这些字段)。 */
export interface SftLabel {
  chunk?: string
  idx?: number
  action?: number[]
  description?: string
  start_timestamp?: string
  end_timestamp?: string
  chunk_name?: string
  event_attributes?: Record<string, unknown>
}

/** GET /api/results/{stem} 响应。file_sig/evidence_sig 为乐观锁指纹。 */
export interface ResultsResponse {
  report_md: string | null
  sft_label: SftLabel | null
  evidence: Evidence | null
  file_sig: string | null
  evidence_sig: string | null
}

/** GET /api/videos/{stem}/meta 响应(逐帧降级需要 frame_count)。 */
export interface VideoMeta {
  frame_count: number
  fps?: number
  width?: number
  height?: number
  duration_sec?: number
}

const enc = encodeURIComponent

export function getResults(stem: string): Promise<ResultsResponse> {
  return apiFetch<ResultsResponse>(`/results/${enc(stem)}`)
}

/** 证据保存:body 需含 base_sig(乐观锁),冲突时后端 409;响应带新 evidence_sig。 */
export function putEvidence(
  stem: string,
  body: Evidence & { base_sig?: string },
): Promise<Evidence & { evidence_sig?: string }> {
  return apiFetch(`/results/${enc(stem)}/evidence`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

/* ---------- 媒体来源与 URL ---------- */

/** 当前视频媒体来源:顶层视频走 /api/videos/{stem} 端点;
 * 嵌套视频(rel 含 /)走 /api/workspace 端点(同 legacy api.js videoSource)。 */
export interface VideoSource {
  stem: string
  rel?: string
}

export function videoSourceOf(stem: string, rel?: string | null): VideoSource {
  return rel && rel.includes('/') ? { stem, rel } : { stem }
}

/** <video> Range 流地址;ss 为起始秒(同 legacy streamUrl)。 */
export function streamUrl(source: VideoSource, ss?: number): string {
  let url =
    source.rel != null
      ? '/api/workspace/stream?path=' + enc(source.rel)
      : '/api/videos/' + enc(source.stem) + '/stream'
  if (ss != null && ss > 0) url += (url.includes('?') ? '&' : '?') + 'ss=' + ss.toFixed(2)
  return url
}

/** meta 的 apiFetch 相对路径(不带 /api 前缀)。 */
export function metaPath(source: VideoSource): string {
  return source.rel != null
    ? '/workspace/meta?path=' + enc(source.rel)
    : '/videos/' + enc(source.stem) + '/meta'
}

/** 逐帧 JPEG 地址(<img> src 用)。 */
export function frameUrl(source: VideoSource, index: number): string {
  return source.rel != null
    ? '/api/workspace/frame?path=' + enc(source.rel) + '&index=' + index
    : '/api/videos/' + enc(source.stem) + '/frame?index=' + index
}

/** analysis/<stem>/ 下文件(报告插图、证据画廊图)地址;按原相对路径请求。 */
export function resultFileUrl(stem: string, name: string): string {
  return '/api/results/' + enc(stem) + '/file?path=' + enc(name)
}
