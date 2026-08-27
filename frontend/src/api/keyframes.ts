/** 关键帧面板 API:候选帧 / 增删排序 / 智能挑选 / 批量(契约见 web/keyframes.py)。
 * 图片地址复用 results.ts 的 frameUrl(candidate)与 resultFileUrl('关键帧/<filename>')。 */
import { apiFetch } from './client'

export interface CandidateFrame {
  index: number
  time_sec: number
}

/** keyframes 列表条目(order = 时间顺序位;文件位于 analysis/<stem>/关键帧/)。 */
export interface KeyframeEntry {
  order: number
  filename: string
  frame_index: number
  time_sec: number
}

/** 增删/排序/智能挑选的统一响应:新列表 + 新 file_sig(回写 sft store 乐观锁)。 */
export interface KeyframeMutation {
  keyframes: KeyframeEntry[]
  file_sig?: string | null
  picked?: number[]
}

export type BatchItemStatus = 'pending' | 'running' | 'ok' | 'failed' | 'skipped'

export interface BatchItemState {
  status: BatchItemStatus
  message?: string
}

export interface BatchStatus {
  id: string
  total: number
  finished: number
  running: boolean
  items: Record<string, BatchItemState>
}

const enc = encodeURIComponent

export function getKfCandidates(stem: string): Promise<CandidateFrame[]> {
  return apiFetch<CandidateFrame[]>(`/videos/${enc(stem)}/keyframes/candidates`)
}

export function getKfList(stem: string): Promise<KeyframeEntry[]> {
  return apiFetch<KeyframeEntry[]>(`/results/${enc(stem)}/keyframes`)
}

export function addKf(stem: string, frameIndex: number, timeSec: number): Promise<KeyframeMutation> {
  return apiFetch(`/results/${enc(stem)}/keyframes`, {
    method: 'POST',
    body: JSON.stringify({ frame_index: frameIndex, time_sec: timeSec }),
  })
}

export function deleteKf(stem: string, filename: string): Promise<KeyframeMutation> {
  return apiFetch(`/results/${enc(stem)}/keyframes/${enc(filename)}`, { method: 'DELETE' })
}

export function reorderKf(stem: string, filenames: string[]): Promise<KeyframeMutation> {
  return apiFetch(`/results/${enc(stem)}/keyframes/order`, {
    method: 'PUT',
    body: JSON.stringify({ filenames }),
  })
}

export function autoPickKf(stem: string, overwrite: boolean): Promise<KeyframeMutation> {
  return apiFetch(`/videos/${enc(stem)}/keyframes/auto_pick`, {
    method: 'POST',
    body: JSON.stringify({ overwrite }),
  })
}

export function startKfBatch(stems: string[], overwrite: boolean): Promise<{ id: string }> {
  return apiFetch('/keyframes/batch', {
    method: 'POST',
    body: JSON.stringify({ stems, overwrite }),
  })
}

export function getKfBatch(id: string): Promise<BatchStatus> {
  return apiFetch<BatchStatus>(`/keyframes/batch/${enc(id)}`)
}
