/** 分析报告删除 API(契约见 web/workspace/videos.py):
 * 单删 DELETE /workspace/analysis/{stem} 与批量 POST /workspace/analysis/delete,
 * 均删 analysis/<stem>/ 整目录且幂等(existed=false 表示本就不存在)。 */
import { apiFetch } from './client'

/** 删除结果(ok=false 时附 error:非法 stem 或盘上删除失败的原因)。 */
export interface AnalysisDeleteResult {
  stem: string
  ok: boolean
  /** 后端处理该条前 analysis/<stem>/ 是否存在。 */
  existed: boolean
  error?: string
}

export function deleteAnalysisReport(stem: string): Promise<AnalysisDeleteResult> {
  return apiFetch<AnalysisDeleteResult>(`/workspace/analysis/${encodeURIComponent(stem)}`, {
    method: 'DELETE',
  })
}

export function deleteAnalysisReports(stems: string[]): Promise<AnalysisDeleteResult[]> {
  return apiFetch<AnalysisDeleteResult[]>('/workspace/analysis/delete', {
    method: 'POST',
    body: JSON.stringify({ stems }),
  })
}
