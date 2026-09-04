/** 侧栏语义检索状态机(契约见 api/rag.ts;结果列表接管 TreeView 侧栏视频区)。
 * 竞态:每次提交自增 seq,晚到的过期响应直接丢弃(切模式/清空同样使在途响应失效)。 */
import { computed, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { ApiError } from '../api/client'
import { cancelRagBuild, getRagBuildStatus, searchRag, startRagBuild } from '../api/rag'
import type { RagBuildStatus, RagLibraryInfo, RagResult } from '../api/rag'

export type SideSearchMode = 'name' | 'semantic'
/** idle=未检索(显示文件树);missing=检索库未建(404 引导文案)。 */
export type RagStatus = 'idle' | 'loading' | 'done' | 'empty' | 'missing' | 'error'
/** 建向量库状态机:done/error 为一次构建的终态(下次启动回到 running)。 */
export type RagBuildState = 'idle' | 'running' | 'done' | 'error'

export const useRagStore = defineStore('rag', () => {
  const mode = ref<SideSearchMode>('name')
  const query = ref('') // 已提交的检索词(清空即恢复文件树;不回写 ws.filter)
  const status = ref<RagStatus>('idle')
  const results = shallowRef<RagResult[]>([])
  const error = ref('') // missing/error 态的展示文案
  let seq = 0 // 在途请求序号

  /** 结果列表是否接管侧栏(语义模式且已提交非空检索词)。 */
  const active = computed(() => mode.value === 'semantic' && query.value !== '')

  /** 切模式:丢弃旧结果与在途响应(文件名模式回到 ws.filter 本地过滤)。 */
  function setMode(m: SideSearchMode) {
    if (mode.value === m) return
    mode.value = m
    clear()
  }

  /** 提交检索(回车);空词等同清空,恢复文件树。 */
  async function search(q: string) {
    query.value = q.trim()
    const my = ++seq
    if (!query.value) {
      status.value = 'idle'
      results.value = []
      error.value = ''
      return
    }
    status.value = 'loading'
    try {
      const resp = await searchRag({ mode: 'text', query: query.value, k: 10 })
      if (my !== seq) return // 过期响应:已有更新的查询/已切模式
      results.value = resp.results
      status.value = resp.results.length ? 'done' : 'empty'
    } catch (e) {
      if (my !== seq) return
      results.value = []
      if (e instanceof ApiError && e.status === 404) {
        status.value = 'missing' // 检索库未建:detail 即引导文案
        error.value = e.message
      } else {
        status.value = 'error'
        error.value = e instanceof Error ? e.message : String(e)
      }
    }
  }

  /** 清空查询并作废在途响应,侧栏恢复文件树。 */
  function clear() {
    seq += 1
    query.value = ''
    status.value = 'idle'
    results.value = []
    error.value = ''
  }

  /* ---- 建向量库:idle/running/done/error + 2s 轮询(契约见 api/rag.ts)---- */
  const buildState = ref<RagBuildState>('idle')
  const buildDone = ref(0)
  const buildTotal = ref(0)
  const buildFailed = ref(0)
  const buildPartial = ref(false) // 终态为被取消的部分完成
  const buildError = ref('') // error 态的 last_error
  const library = shallowRef<RagLibraryInfo | null>(null) // 库概况(空闲也维护,供 tooltip)
  const buildPending = ref<number | null>(null) // 空闲时待更新条目数(新视频+标注变更);null=未知
  let buildPolling = false // 防多实例并发轮询

  /** 应用一次 status:running 更新进度;非 running 且之前在跑 → 落定终态;
   * 空闲时只更新 library 概况,不进终态。 */
  function applyBuildStatus(st: RagBuildStatus) {
    library.value = st.library
    if (st.pending !== undefined) buildPending.value = st.pending
    if (st.running) {
      buildState.value = 'running'
      buildDone.value = st.done
      buildTotal.value = st.total
      buildFailed.value = st.failed
      buildPartial.value = st.partial
      return
    }
    if (buildState.value !== 'running') return
    buildDone.value = st.done
    buildTotal.value = st.total
    buildFailed.value = st.failed
    buildPartial.value = st.partial
    if (st.last_error) {
      buildState.value = 'error'
      buildError.value = st.last_error
    } else {
      buildState.value = 'done'
    }
  }

  /** 拉一次 status;进入 running 时启动轮询。查询失败静默(后端未就绪不阻塞工具栏)。 */
  async function refreshBuildStatus() {
    try {
      applyBuildStatus(await getRagBuildStatus())
    } catch {
      // 状态查询失败:保留现状,下轮重试
    }
    if (buildState.value === 'running') void pollBuild()
  }

  /** running 期间每 2s 轮询直至终态;buildPolling 防多实例并发,状态被重置(idle)即退出。 */
  async function pollBuild() {
    if (buildPolling) return
    buildPolling = true
    try {
      while (buildState.value === 'running') {
        await new Promise((r) => setTimeout(r, 2000))
        if (buildState.value !== 'running') break // 轮询间隙被重置(切工作区/卸载)
        try {
          applyBuildStatus(await getRagBuildStatus())
        } catch {
          // 单次失败继续,下轮重试
        }
      }
    } finally {
      buildPolling = false
    }
  }

  /** 确认后启动构建并进入轮询;409(已在跑)兜底为直接拉 status 轮询。 */
  async function startBuild() {
    try {
      await startRagBuild()
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409)) throw e
    }
    buildState.value = 'running'
    await refreshBuildStatus()
  }

  /** 取消构建:后端置取消标记,轮询随终态(partial)自然结束。 */
  async function cancelBuild() {
    await cancelRagBuild()
  }

  /** 切工作区:重置状态机(轮询循环据此退出),按新工作区拉一次 status。 */
  async function resetBuild() {
    buildState.value = 'idle'
    buildDone.value = 0
    buildTotal.value = 0
    buildFailed.value = 0
    buildPartial.value = false
    buildError.value = ''
    library.value = null
    buildPending.value = null
    await refreshBuildStatus()
  }

  /** 页面卸载清理:退到 idle 让轮询循环自然退出(不持有定时器,无需额外清)。 */
  function stopBuild() {
    buildState.value = 'idle'
  }

  return {
    mode, query, status, results, error, active, setMode, search, clear,
    buildState, buildDone, buildTotal, buildFailed, buildPartial, buildError, library, buildPending,
    refreshBuildStatus, startBuild, cancelBuild, resetBuild, stopBuild,
  }
})
