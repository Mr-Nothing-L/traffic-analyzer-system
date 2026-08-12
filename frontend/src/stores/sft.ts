/** SFT 编辑状态:事件配置缓存 + 草稿/dirty/file_sig 乐观锁/保存/重置。
 * 计算全部委托 sft/ 纯逻辑模块(见其 README);本 store 只做状态持有与 API 往返。
 * 编排逐语义移植自 legacy sft.js(renderSftBody / saveSft / updateSftDirty)。 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '../api/client'
import { getEventConfig, putSft } from '../api/results'
import { buildPutPayload, initDraft, isDirty, signature } from '../sft/model'
import type { EventDef, SftDraft, SftLabel, SftPutPayload } from '../sft/types'

/** action 结果:组件据此弹提示;conflict=true 表示 409 乐观锁冲突。 */
export interface SftSaveResult {
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

export const useSftStore = defineStore('sft', () => {
  const events = ref<EventDef[] | null>(null) // /api/config/events 缓存(同 legacy state.eventConfig)
  const draft = ref<SftDraft | null>(null)
  const savedSig = ref('')
  const saving = ref(false)
  const baseSig = ref<string | null>(null) // 乐观锁指纹(GET 响应的 file_sig;保存后随响应更新)
  const sftLabel = ref<SftLabel | null>(null) // 磁盘版本(重置/保存载荷的其余字段来源)
  const rawAction = ref<number[] | null>(null) // 模型原始推理 action(来自 _raw.json,保存后仍不变)
  const stem = ref('')
  const configError = ref<string | null>(null)

  /** 事件配置只拉一次(同 legacy state.eventConfig 缓存)。 */
  async function ensureEvents(): Promise<boolean> {
    if (events.value) return true
    try {
      events.value = await getEventConfig<EventDef[]>()
      return true
    } catch (e) {
      configError.value = errInfo(e).message
      return false
    }
  }

  /** 从结果初始化/重建草稿(切换视频、保存成功重建、重置共用;同 legacy renderSftBody)。 */
  async function init(
    newStem: string,
    sft: SftLabel,
    fileSig: string | null,
    rawActionData: number[] | null = null,
  ): Promise<boolean> {
    if (!(await ensureEvents())) return false
    const { draft: d, savedSig: sig } = initDraft(events.value!, sft)
    stem.value = newStem
    sftLabel.value = sft
    baseSig.value = fileSig
    rawAction.value = rawActionData
    draft.value = d
    savedSig.value = sig
    return true
  }

  /** 签名比对(同 legacy updateSftDirty):任何草稿编辑都会触发重算。 */
  const dirty = computed(() =>
    draft.value && events.value ? isDirty(draft.value, events.value, savedSig.value) : false,
  )

  /** 重置为磁盘版本(内存中的 sftLabel 即载入时的磁盘内容,同 legacy 不重新 GET)。 */
  function resetLocal() {
    if (!sftLabel.value || !events.value) return
    const { draft: d, savedSig: sig } = initDraft(events.value, sftLabel.value)
    draft.value = d
    savedSig.value = sig
  }

  /** 保存:base_sig 乐观锁;409 → {conflict:true} 由组件弹「丢弃并刷新/保留我的修改」。
   * 在途签名(同 legacy inFlightSig):保存期间无新编辑则以保存结果重建草稿,
   * 否则保留草稿仅重算 dirty。 */
  async function save(): Promise<SftSaveResult> {
    const d = draft.value
    if (!d || !events.value || !sftLabel.value || saving.value) return { ok: false }
    saving.value = true
    const saveStem = stem.value
    const inFlightSig = signature(d, events.value)
    const payload: SftPutPayload = buildPutPayload(d, events.value, sftLabel.value, baseSig.value)
    // last_edited_by 是服务端落盘的追溯字段:GET 原样返回但 PUT schema extra=forbid,
    // 不剥掉对已编辑过的文件必 422(legacy sft.js 的既有缺陷;与证据保存同款适配)。
    delete (payload as { last_edited_by?: unknown }).last_edited_by
    try {
      const saved = await putSft<SftLabel>(saveStem, payload)
      if (stem.value !== saveStem) return { ok: false } // 期间切换了视频,丢弃过期响应
      const newSig = saved && saved.file_sig ? saved.file_sig : null
      const body = { ...(saved || payload) } as SftLabel & { file_sig?: string }
      delete body.file_sig // 锁字段不进标注对象(同 legacy)
      sftLabel.value = body
      baseSig.value = newSig
      if (signature(d, events.value) === inFlightSig) {
        const rebuilt = initDraft(events.value, body) // 保存期间无新编辑:以保存后的内容重建草稿
        draft.value = rebuilt.draft
        savedSig.value = rebuilt.savedSig
      }
      // 保存期间用户继续编辑:保留草稿,savedSig 不动,dirty 由 computed 重算(同 legacy)
      return { ok: true }
    } catch (e) {
      const { status, message } = errInfo(e)
      return { ok: false, conflict: status === 409, status, message }
    } finally {
      saving.value = false
    }
  }

  /** 离开详情页/切换视频时清空,避免幽灵 dirty 态(同 legacy selectVideo)。 */
  function clear() {
    draft.value = null
    savedSig.value = ''
    saving.value = false
    baseSig.value = null
    sftLabel.value = null
    rawAction.value = null
    stem.value = ''
    configError.value = null
  }

  return {
    events, draft, savedSig, saving, baseSig, sftLabel, rawAction, stem, configError,
    dirty, ensureEvents, init, resetLocal, save, clear,
  }
})
