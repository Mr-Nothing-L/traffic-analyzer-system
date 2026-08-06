/** 数据看板状态:汇总指标 + 逐视频明细(服务端分页/筛选)+ 行内审核乐观更新。
 * 迁移自 legacy static/js/dashboard.js 的模块级状态;Pinia store 跨页面保留
 * 筛选/分页(legacy 为模块级全局)。契约:
 *   GET /api/dashboard → { summary, event_names, metrics }(web/dashboard/metrics.py)
 *   GET /api/dashboard/rows?page&size&consistency&review&edited&q(先过滤后分页,rows.py)
 *   PUT /api/dashboard/review { stem, status }(REVIEW_STATUSES 见 review.py) */
import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api/client'

/* ---------- 契约类型(与后端字段一一对应;可选字段缺失时 UI 显空态) ---------- */
export type ConsistencyKey = 'consistent' | 'diff' | 'no_gt' | 'no_results'
export type ReviewStatus = 'unconfirmed' | 'confirmed' | 'needs_review'
export type ChipCls = 'ok' | 'warn' | 'mute' | 'edit'

export interface FilterOption<K extends string> {
  key: K
  label: string
  cls: ChipCls
}

export const CONSISTENCY_OPTIONS: FilterOption<ConsistencyKey>[] = [
  { key: 'consistent', label: '一致', cls: 'ok' },
  { key: 'diff', label: '有差异', cls: 'warn' },
  { key: 'no_gt', label: '无 GT', cls: 'mute' },
  { key: 'no_results', label: '未推理', cls: 'mute' },
]
// 三态以 web/dashboard/review.py 的 REVIEW_STATUSES 为准
export const REVIEW_OPTIONS: FilterOption<ReviewStatus>[] = [
  { key: 'unconfirmed', label: '未确认', cls: 'mute' },
  { key: 'confirmed', label: '已确认', cls: 'ok' },
  { key: 'needs_review', label: '需复核', cls: 'warn' },
]

export interface DashboardSummary {
  total?: number; consistent?: number; diff?: number; no_gt?: number; no_results?: number
  confirmed?: number; unconfirmed?: number; needs_review?: number; edited?: number
}
export interface MetricAvg {
  tp?: number; fp?: number; fn?: number; precision?: number; recall?: number; f1?: number
}
export interface EventMetric extends MetricAvg {
  event_id: number
  name?: string
}
export interface DashboardMetrics {
  per_event?: EventMetric[]
  macro?: MetricAvg
  micro?: MetricAvg
}
export interface DashboardData {
  summary?: DashboardSummary
  event_names?: Record<string, string>
  metrics?: DashboardMetrics
}
export interface DashboardRow {
  rel: string; stem: string; has_results: boolean
  gt_ids: number[]; pred_ids: number[]
  status: ConsistencyKey; missing: number[]; extra: number[]
  pred_raw_ids: number[] | null; edited: boolean
  edit_missing: number[]; edit_extra: number[]
  review: ReviewStatus
}
export interface RowsData {
  rows: DashboardRow[]; page: number; size: number; total: number; total_pages: number
}

export const PAGE_SIZE = 50 // 每页行数(契约 size 参数,同 legacy)
const SEARCH_DEBOUNCE_MS = 300 // 名称搜索防抖(同 legacy)

export const useDashboardStore = defineStore('dashboard', () => {
  const data = ref<DashboardData | null>(null) // /api/dashboard 最近一次成功响应
  const rowsData = ref<RowsData | null>(null) // /api/dashboard/rows 最近一次成功响应
  const fetching = ref(false) // 汇总请求去重
  const rowsFetching = ref(false) // 明细请求去重 + 翻页条 loading 态
  const summaryError = ref<string | null>(null) // 仅首次(无旧数据)加载失败时展示
  const rowsError = ref<string | null>(null)
  const curPage = ref(1)
  const filters = reactive({
    consistency: new Set<ConsistencyKey>(),
    review: new Set<ReviewStatus>(),
    editedOnly: false,
    name: '',
  })
  // 正在提交审核的 stem:重拉回包不回滚这些行的本地乐观值(同 legacy)
  const pendingReviews = reactive(new Set<string>())
  let searchTimer: ReturnType<typeof setTimeout> | null = null

  const summary = computed(() => data.value?.summary || {})
  const hasFilters = computed(
    () =>
      filters.consistency.size > 0 ||
      filters.review.size > 0 ||
      filters.editedOnly ||
      filters.name !== '',
  )

  function eventName(id: number): string {
    const names = data.value?.event_names || {}
    return names[String(id)] != null ? String(names[String(id)]) : String(id)
  }

  function namesText(ids: number[] | null | undefined): string {
    return (ids || []).map(eventName).join('、')
  }

  /* ---------- 拉取 ---------- */
  async function fetchSummary() {
    if (fetching.value) return
    fetching.value = true
    try {
      data.value = await apiFetch<DashboardData>('/dashboard')
      summaryError.value = null
    } catch (e) {
      if (!data.value) summaryError.value = (e as Error).message
      // 已有数据时静默保留,下次事件重试,不打断阅读(同 legacy)
    } finally {
      fetching.value = false
    }
  }

  function rowsQuery(page: number): string {
    const q = new URLSearchParams({ page: String(page), size: String(PAGE_SIZE) })
    if (filters.consistency.size) q.set('consistency', Array.from(filters.consistency).join(','))
    if (filters.review.size) q.set('review', Array.from(filters.review).join(','))
    if (filters.editedOnly) q.set('edited', '1')
    if (filters.name) q.set('q', filters.name)
    return `/dashboard/rows?${q.toString()}`
  }

  async function fetchRows(page: number) {
    if (rowsFetching.value) return
    rowsFetching.value = true
    try {
      let d = await apiFetch<RowsData>(rowsQuery(page))
      // 页码夹紧:请求页超出总页数(数据变少/筛选收窄)时回退最后一页重拉
      if (d.total_pages >= 1 && d.page > d.total_pages) {
        d = await apiFetch<RowsData>(rowsQuery(d.total_pages))
      }
      if (pendingReviews.size && rowsData.value) {
        d.rows.forEach((r) => {
          if (pendingReviews.has(r.stem)) {
            const cur = rowsData.value!.rows.find((x) => x.stem === r.stem)
            if (cur) r.review = cur.review
          }
        })
      }
      rowsData.value = d
      curPage.value = d.page
      rowsError.value = null
    } catch (e) {
      if (!rowsData.value) rowsError.value = (e as Error).message
    } finally {
      rowsFetching.value = false
    }
  }

  /** 首拉与 SSE dashboard.changed 共用:汇总 + 当前页明细(事件驱动,不轮询)。 */
  async function refresh() {
    await fetchSummary()
    await fetchRows(curPage.value)
  }

  /* ---------- 筛选:变更即回到第 1 页重新向服务端请求 ---------- */
  function applyFilter() {
    curPage.value = 1
    fetchRows(1)
  }

  function toggleSet<T extends string>(set: Set<T>, key: T) {
    if (set.has(key)) set.delete(key)
    else set.add(key)
  }

  function toggleConsistency(key: ConsistencyKey) {
    toggleSet(filters.consistency, key)
    applyFilter()
  }
  function toggleReview(key: ReviewStatus) {
    toggleSet(filters.review, key)
    applyFilter()
  }
  function toggleEdited() {
    filters.editedOnly = !filters.editedOnly
    applyFilter()
  }
  function clearFilters() {
    filters.consistency.clear()
    filters.review.clear()
    filters.editedOnly = false
    filters.name = ''
    applyFilter()
  }
  /** 名称搜索:300ms 防抖后触发服务端过滤(同 legacy)。 */
  function setName(v: string) {
    filters.name = v
    curPage.value = 1
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = setTimeout(() => fetchRows(1), SEARCH_DEBOUNCE_MS)
  }

  /* ---------- 行内审核:乐观更新 + 失败回滚(回滚后抛错由调用方提示) ---------- */
  function adjustSummary(key: keyof DashboardSummary, delta: number) {
    if (!data.value) return
    const s = data.value.summary || (data.value.summary = {})
    s[key] = Math.max(0, (s[key] || 0) + delta)
  }

  async function setReview(stem: string, status: ReviewStatus) {
    const row = rowsData.value?.rows.find((r) => r.stem === stem)
    if (!row || row.review === status) return
    const prev = row.review
    row.review = status
    pendingReviews.add(stem)
    adjustSummary(prev, -1)
    adjustSummary(status, 1)
    try {
      await apiFetch('/dashboard/review', {
        method: 'PUT',
        body: JSON.stringify({ stem, status }),
      })
    } catch (e) {
      row.review = prev
      adjustSummary(status, -1)
      adjustSummary(prev, 1)
      throw e
    } finally {
      pendingReviews.delete(stem)
    }
  }

  return {
    data, rowsData, fetching, rowsFetching, summaryError, rowsError,
    curPage, filters, pendingReviews, summary, hasFilters,
    eventName, namesText, fetchSummary, fetchRows, refresh,
    toggleConsistency, toggleReview, toggleEdited, clearFilters, setName, setReview,
  }
})
