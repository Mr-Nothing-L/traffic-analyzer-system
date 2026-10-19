/** 详情页 API:结果读取 / 证据保存 / 媒体 URL 构造。
 * 契约见 traffic_analyzer/web/evidence/*(results、乐观锁 PUT)与
 * frames.py / video_stream.py(meta/frame/stream)。
 * 媒体 URL 给 <video>/<img> src 用,需带 /api 前缀;apiFetch 的路径不带。 */
import { apiFetch, apiResponse } from './client'
import type { SftSample } from './contracts.gen'

export type { SftSample }

/** GET 响应的 sft_label(磁盘样本):契约形状由生成的 SftSample 承载
 * (scripts/gen_frontend_contracts.py 从 evidence_schema.py 派生,禁手改)。
 * SftLabel 为调用点兼容别名。 */
export type SftLabel = SftSample

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

/** GET /api/results/{stem} 响应。file_sig/evidence_sig 为乐观锁指纹。 */
export interface ResultsResponse {
  report_md: string | null
  sft_label: SftLabel | null
  evidence: Evidence | null
  raw_action: number[] | null
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

/** 事件类别配置(SFT 编辑器按事件分框 + 封闭枚举选项;GET /api/config/events)。 */
export function getEventConfig<T>(): Promise<T> {
  return apiFetch<T>('/config/events')
}

/** SFT 保存:body 需含 base_sig(乐观锁,取 GET 响应的 file_sig),冲突时后端 409;
 * 响应为写入后的样本 + 新 file_sig(见 web/evidence/sft_api.py)。 */
export function putSft<T>(stem: string, body: unknown): Promise<T & { file_sig?: string }> {
  return apiFetch(`/results/${enc(stem)}/sft`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

/** 导出单视频 SFT 标注(GET /results/{stem}/sft/export):后端读取磁盘样本
 * (顶层优先/quarantine 回退)清洗为训练契约字段,响应为单行 JSONL 文本
 * 附件(见 web/evidence/sft_export.py)。返回文本,由调用方触发浏览器
 * 下载;404 = 无标注,422 = 文件损坏。 */
export function fetchSftJsonl(stem: string): Promise<string> {
  return apiResponse(`/results/${enc(stem)}/sft/export`).then((r) => r.text())
}

/** 批量导出结果:合并 JSONL 文本 + 诚实计数(响应头 X-Export-Total /
 * X-Export-Skipped;跳过 = 无标注/损坏的 stem)。 */
export interface SftBatchExportResult {
  text: string
  total: number
  skipped: number
}

/** 批量导出勾选集合的 SFT 标注(POST /workspace/sft/export):每行与单视频
 * 导出同构(共用后端投影函数);空集合/超上限 400,坏 stem 404。 */
export function exportSftBatch(stems: string[]): Promise<SftBatchExportResult> {
  return apiResponse('/workspace/sft/export', {
    method: 'POST',
    body: JSON.stringify({ stems }),
  }).then(async (r) => ({
    text: await r.text(),
    total: Number(r.headers.get('X-Export-Total') ?? 0),
    skipped: Number(r.headers.get('X-Export-Skipped') ?? 0),
  }))
}

/** 专家阶段定义(泳道缓行封顶用;404 时调用方回退内置封顶,同 legacy)。 */
export function getExpertPhases<T>(): Promise<T> {
  return apiFetch<T>('/expert-phases')
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
