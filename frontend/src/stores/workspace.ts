/** 工作区/文件树/勾选状态(迁移自 legacy tree.js + workspace.js + state.js)。 */
import { computed, reactive, ref, shallowReactive, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api/client'

/** GET /api/workspace/tree 单层条目(目录或文件;视频带 stem/has_results)。 */
export interface TreeEntry {
  name: string
  rel: string
  type: 'dir' | 'file'
  is_video?: boolean
  size?: number
  mtime?: number
  stem?: string
  has_results?: boolean
}

/** GET /api/workspace/videos 递归视频条目。 */
export interface VideoInfo {
  name: string
  stem: string
  rel: string
  size: number
  mtime: number
  has_results: boolean
}

export type SortKey = 'name' | 'mtime' | 'size' | 'status'

// localStorage key 与 legacy 完全一致(tree.js / workspace.js),迁移后沿用用户既有偏好
export const SIDE_FILTER_KEY = 'ta_sidebar_filter'
export const SIDE_SORT_KEY = 'ta_sidebar_sort'
const RECENT_WS_KEY = 'ta_recent_workspaces'
const RECENT_WS_MAX = 8

export const useWorkspaceStore = defineStore('workspace', () => {
  const path = ref<string | null>(null) // 当前工作区绝对路径(未设置为 null)
  const loaded = ref(false) // 树是否已加载(刷新后由 TreeView 自动恢复加载)
  const treeLoading = ref(false) // loadTree 进行中(欢迎卡据此显示加载态)
  const root = ref<TreeEntry[]>([])
  // 浅响应式:条目对象不做深代理,数据变更一律整体替换(大工作区显著降开销)
  const children = shallowReactive<Record<string, TreeEntry[]>>({}) // dir rel → 子层(懒加载缓存)
  const expanded = reactive(new Set<string>())
  const videos = shallowRef<VideoInfo[]>([])
  const checked = reactive(new Set<string>()) // 勾选的视频 rel
  const currentRel = ref<string | null>(null) // 当前选中视频 rel(行高亮 + presence viewing)
  const filter = ref(localStorage.getItem(SIDE_FILTER_KEY) || '')
  const sort = ref<SortKey>((localStorage.getItem(SIDE_SORT_KEY) as SortKey) || 'name')

  const hasWorkspace = computed(() => !!path.value)
  /** rel → 视频信息索引(videos 整体替换时重建;videoFor/选中查询走 O(1))。 */
  const videoByRel = computed(() => {
    const m = new Map<string, VideoInfo>()
    for (const v of videos.value) m.set(v.rel, v)
    return m
  })
  const allChecked = computed(
    () => videos.value.length > 0 && videos.value.every((v) => checked.has(v.rel)),
  )
  const someChecked = computed(
    () => !allChecked.value && videos.value.some((v) => checked.has(v.rel)),
  )

  /** 进页面拉一次工作区;不自动 loadTree(大工作区 >10s,由用户点「加载工作区」)。 */
  async function fetchWorkspace() {
    try {
      const ws = await apiFetch<{ path: string | null }>('/workspace')
      path.value = ws.path
    } catch {
      path.value = null // 后端未就绪:保持欢迎卡
    }
  }

  /** 加载文件树;preserve 时保留已展开目录并逐层重拉。 */
  async function loadTree(preserve = false) {
    const prevExpanded = preserve ? Array.from(expanded) : []
    treeLoading.value = true
    loaded.value = false
    Object.keys(children).forEach((k) => delete children[k])
    expanded.clear()
    prevExpanded.forEach((r) => expanded.add(r))
    try {
      try {
        const data = await apiFetch<{ entries: TreeEntry[] }>('/workspace/tree')
        root.value = data.entries || []
      } catch {
        root.value = []
      }
      try {
        videos.value = (await apiFetch<VideoInfo[]>('/workspace/videos')) || []
      } catch {
        videos.value = []
      }
      await Promise.all(
        prevExpanded.map(async (rel) => {
          try {
            const data = await apiFetch<{ entries: TreeEntry[] }>(
              `/workspace/tree?path=${encodeURIComponent(rel)}`,
            )
            children[rel] = data.entries || []
          } catch {
            expanded.delete(rel) // 目录已消失:放弃展开
          }
        }),
      )
      loaded.value = true
    } finally {
      treeLoading.value = false
    }
  }

  /** 展开/收起目录;首次展开懒加载子层。失败回滚并抛错(调用方提示)。 */
  async function toggleDir(rel: string) {
    if (expanded.has(rel)) {
      expanded.delete(rel)
      return
    }
    expanded.add(rel) // 先置展开态,TreeNode 显示「加载中…」
    if (!children[rel]) {
      try {
        const data = await apiFetch<{ entries: TreeEntry[] }>(
          `/workspace/tree?path=${encodeURIComponent(rel)}`,
        )
        children[rel] = data.entries || []
      } catch (e) {
        expanded.delete(rel)
        throw e
      }
    }
  }

  /* ---- 静默刷新:任务终态(完成/失败/停止)后对齐 has_results 与徽标 ----
   * 与 loadTree 不同:不动 loaded/勾选,先拉取成功再整体替换;主请求失败保留旧树,树绝不消失
   * (旧版 job.done → loadTree(preserve) 先置 loaded=false 并清空树,重拉慢/失败时卡初始态)。 */
  let refreshing = false // 在途去重;在途期间又有终态则 refreshQueued 补一轮
  let refreshQueued = false

  /** 任务终态后静默重拉树/视频列表/已展开子层;未加载时不刷(不擅自加载大工作区)。 */
  async function refreshTree() {
    if (!loaded.value) return
    if (refreshing) {
      refreshQueued = true
      return
    }
    refreshing = true
    try {
      do {
        refreshQueued = false
        await refreshOnce()
      } while (refreshQueued)
    } finally {
      refreshing = false
    }
  }

  /** 单轮静默刷新:树+视频任一失败整轮放弃;已展开目录消失时放弃其展开态。 */
  async function refreshOnce() {
    let entries: TreeEntry[]
    let vids: VideoInfo[]
    try {
      const [tree, videosResp] = await Promise.all([
        apiFetch<{ entries: TreeEntry[] }>('/workspace/tree'),
        apiFetch<VideoInfo[]>('/workspace/videos'),
      ])
      entries = tree.entries || []
      vids = videosResp || []
    } catch {
      return // 拉取失败:保留旧树
    }
    const dirs = Array.from(expanded)
    const kids = await Promise.all(
      dirs.map(async (rel): Promise<{ rel: string; entries: TreeEntry[] | null }> => {
        try {
          const data = await apiFetch<{ entries: TreeEntry[] }>(
            `/workspace/tree?path=${encodeURIComponent(rel)}`,
          )
          return { rel, entries: data.entries || [] }
        } catch {
          return { rel, entries: null } // 目录已消失
        }
      }),
    )
    root.value = entries
    videos.value = vids
    Object.keys(children).forEach((k) => delete children[k])
    for (const k of kids) {
      if (k.entries) children[k.rel] = k.entries
      else expanded.delete(k.rel)
    }
  }

  /** 目录弹窗确认后的统一切换:POST + 重置勾选/选中 + 重载树。 */
  async function applyWorkspace(newPath: string) {
    const ws = await apiFetch<{ path: string }>('/workspace', {
      method: 'POST',
      body: JSON.stringify({ path: newPath }),
    })
    path.value = ws.path
    checked.clear()
    currentRel.value = null
    await loadTree()
    return ws
  }

  function setFilter(v: string) {
    filter.value = v.trim()
    localStorage.setItem(SIDE_FILTER_KEY, filter.value)
  }

  function setSort(v: SortKey) {
    sort.value = v
    localStorage.setItem(SIDE_SORT_KEY, v)
  }

  function setChecked(rel: string, on: boolean) {
    if (on) checked.add(rel)
    else checked.delete(rel)
  }

  /** 全选/取消全选(legacy check-all:作用于全部视频,不受过滤影响)。 */
  function setAllChecked(on: boolean) {
    checked.clear()
    if (on) videos.value.forEach((v) => checked.add(v.rel))
  }

  /* ---- 最近使用的工作区(最新在前,去重,最多 8 条,同 legacy) ---- */
  const recentTick = ref(0) // localStorage 非响应式:pushRecent 后 +1 触发依赖重算

  function loadRecent(): string[] {
    recentTick.value // 建立响应式依赖
    try {
      const arr = JSON.parse(localStorage.getItem(RECENT_WS_KEY) || '[]')
      return Array.isArray(arr) ? arr.filter((p) => typeof p === 'string') : []
    } catch {
      return []
    }
  }

  function pushRecent(p: string) {
    const arr = loadRecent().filter((x) => x !== p)
    arr.unshift(p)
    try {
      localStorage.setItem(RECENT_WS_KEY, JSON.stringify(arr.slice(0, RECENT_WS_MAX)))
    } catch {
      // 存储不可用时静默忽略
    }
    recentTick.value += 1
  }

  return {
    path, loaded, treeLoading, root, children, expanded, videos, checked, currentRel,
    filter, sort, hasWorkspace, videoByRel, allChecked, someChecked,
    fetchWorkspace, loadTree, refreshTree, toggleDir, applyWorkspace,
    setFilter, setSort, setChecked, setAllChecked, loadRecent, pushRecent,
  }
})
