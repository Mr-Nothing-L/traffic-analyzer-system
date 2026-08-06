/** 证据编辑状态:结果缓存 + 草稿/dirty/file_sig 乐观锁/保存/重置。
 * 迁移自 legacy evidence.js + api.js 的 evidenceSigCache 与 preview.js selectVideo。 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '../api/client'
import type { Evidence, ResultsResponse } from '../api/results'
import { getResults, putEvidence } from '../api/results'

/** action 结果:组件据此弹提示;conflict=true 表示 409 乐观锁冲突。 */
export interface SaveResult {
  ok: boolean
  conflict?: boolean
  status?: number
  message?: string
}

function errInfo(e: unknown): { status: number; message: string } {
  return e instanceof ApiError
    ? { status: e.status, message: e.message }
    : { status: 0, message: e instanceof Error ? e.message : String(e) }
}

export const useEvidenceStore = defineStore('evidence', () => {
  const results = ref<ResultsResponse | null>(null)
  const draft = ref<Evidence | null>(null) // 深拷贝草稿,编辑直接改它
  const dirty = ref(false)
  const saving = ref(false)
  const loading = ref(false)
  const loadError = ref<string | null>(null)
  const evidenceSig = ref<string | null>(null) // 乐观锁指纹(PUT base_sig)
  const tabIdx = ref(0) // 当前事件 Tab
  let loadedStem = ''

  /** 拉取结果并重建草稿(切换视频 / 保存冲突后刷新 / 重置共用)。 */
  async function load(stem: string): Promise<boolean> {
    loading.value = true
    loadError.value = null
    try {
      const r = await getResults(stem)
      results.value = r
      evidenceSig.value = r.evidence_sig
      draft.value = r.evidence ? (JSON.parse(JSON.stringify(r.evidence)) as Evidence) : null
      dirty.value = false
      if (draft.value && tabIdx.value >= draft.value.events.length) tabIdx.value = 0
      loadedStem = stem
      return true
    } catch (e) {
      loadError.value = errInfo(e).message
      return false
    } finally {
      loading.value = false
    }
  }

  function markDirty() {
    dirty.value = true
  }

  /** 保存:base_sig 乐观锁;409 → {conflict:true} 由组件提示并刷新。
   * 在途草稿签名(同 legacy inFlightSig):保存期间继续编辑则保留 dirty。 */
  async function save(stem: string): Promise<SaveResult> {
    if (!draft.value || saving.value) return { ok: false }
    saving.value = true
    const inFlightSig = JSON.stringify(draft.value)
    // last_edited_by 是后端落盘的追溯字段:GET 原样返回但 PUT schema extra=forbid,
    // 不剥掉第二次保存必 422(后端契约如此,前端适配)。
    const clean: Evidence & { base_sig?: string; last_edited_by?: string } = Object.assign(
      {},
      draft.value,
    )
    delete clean.last_edited_by
    const body = evidenceSig.value
      ? Object.assign(clean, { base_sig: evidenceSig.value })
      : clean
    try {
      const resp = await putEvidence(stem, body)
      if (resp && resp.evidence_sig) evidenceSig.value = resp.evidence_sig
      if (results.value) results.value.evidence = JSON.parse(JSON.stringify(draft.value))
      if (JSON.stringify(draft.value) === inFlightSig) dirty.value = false
      return { ok: true }
    } catch (e) {
      const { status, message } = errInfo(e)
      return { ok: false, conflict: status === 409, status, message }
    } finally {
      saving.value = false
    }
  }

  /** 重置为磁盘版本(重新 GET)。 */
  async function reset(stem: string): Promise<SaveResult> {
    const ok = await load(stem)
    return ok ? { ok: true } : { ok: false, message: loadError.value || '未知错误' }
  }

  /** 离开详情页/切换视频时清空,避免幽灵 dirty 态(同 legacy selectVideo)。 */
  function clear() {
    results.value = null
    draft.value = null
    dirty.value = false
    saving.value = false
    loadError.value = null
    evidenceSig.value = null
    tabIdx.value = 0
    loadedStem = ''
  }

  return {
    results, draft, dirty, saving, loading, loadError, evidenceSig, tabIdx,
    load, markDirty, save, reset, clear,
  }
})
