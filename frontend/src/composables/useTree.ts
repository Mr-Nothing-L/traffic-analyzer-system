/** 树的展示计算:状态徽标 / 过滤 / 排序(迁移自 legacy tree.js 的纯计算部分)。 */
import { useJobsStore } from '../stores/jobs'
import type { TreeEntry, VideoInfo } from '../stores/workspace'
import { useWorkspaceStore } from '../stores/workspace'

export interface VideoStatus {
  cls: 'st-running' | 'st-queued' | 'st-done' | 'st-failed' | 'st-none'
  text: string
}

/** 「状态」排序优先级:失败最前,已完成最后(同 legacy STATUS_ORDER)。 */
const STATUS_ORDER: Record<VideoStatus['cls'], number> = {
  'st-failed': 0,
  'st-running': 1,
  'st-queued': 2,
  'st-none': 3,
  'st-done': 4,
}

/** legacy util.js fmtBytes。 */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + units[i]
}

/** 状态判定优先级:运行中 > 排队 > 已完成 > 失败 > 未推理(同 legacy videoStatus)。 */
export function videoStatusOf(
  v: Pick<VideoInfo, 'stem' | 'has_results'>,
  job: { status: string } | null,
): VideoStatus {
  if (job && job.status === 'running') return { cls: 'st-running', text: '运行中' }
  if (job && job.status === 'queued') return { cls: 'st-queued', text: '排队中' }
  if (v.has_results) return { cls: 'st-done', text: '已完成' }
  if (job && job.status === 'failed') return { cls: 'st-failed', text: '失败' }
  return { cls: 'st-none', text: '未推理' }
}

/** 渲染行视图模型:视频行预取 video/状态/进度(模板每行只算一次,不再重复 videoFor)。 */
export interface ViewRow {
  e: TreeEntry
  video: VideoInfo | null // 视频行非空
  status: VideoStatus | null // 视频行非空
  fraction: number | null // 运行中行的像素条进度
}

export function useTreeView() {  const ws = useWorkspaceStore()
  const jobs = useJobsStore()

  /** 子串过滤,忽略大小写(仅影响展示,不改勾选)。 */
  function nameMatches(name: string): boolean {
    return !ws.filter || String(name || '').toLowerCase().includes(ws.filter.toLowerCase())
  }

  /** 过滤:仅保留匹配的视频行及其祖先目录;非视频文件在过滤时隐藏。 */
  function filterLevel(entries: TreeEntry[]): TreeEntry[] {
    if (!ws.filter) return entries
    return entries.filter((e) => {
      if (e.type === 'dir') {
        // 已加载子级时递归判断;未加载/折叠时用全量视频列表判断后代是否含匹配
        const kids = ws.children[e.rel]
        if (kids && ws.expanded.has(e.rel)) return filterLevel(kids).length > 0
        return ws.videos.some(
          (v) => v.rel.startsWith(e.rel + '/') && nameMatches(v.name),
        )
      }
      return !!e.is_video && nameMatches(e.name)
    })
  }

  /** 排序:目录恒在视频之前且保持原顺序;视频按所选键排序。 */
  function sortLevel(entries: TreeEntry[]): TreeEntry[] {
    if (ws.sort === 'name') return entries // 后端返回即名称序
    const dirs = entries.filter((e) => e.type === 'dir')
    const vids = entries.filter((e) => e.is_video)
    const files = entries.filter((e) => e.type !== 'dir' && !e.is_video)
    const key = {
      mtime: (e: TreeEntry) => -(e.mtime || 0), // 最新在前
      size: (e: TreeEntry) => -(e.size || 0), // 最大在前
      status: (e: TreeEntry) => STATUS_ORDER[videoStatus(videoFor(e)).cls],
    }[ws.sort]
    vids.sort(
      (a, b) => key(a) - key(b) || String(a.name).localeCompare(String(b.name)),
    )
    return dirs.concat(vids, files)
  }

  /** 当前层级实际要展示的条目(先过滤后排序)。 */
  function viewEntries(entries: TreeEntry[]): TreeEntry[] {
    return sortLevel(filterLevel(entries))
  }
  /** 树条目 → 视频信息:优先全量列表索引(O(1)),缺失时由条目合成(任意深度,同 legacy)。 */
  function videoFor(e: TreeEntry): VideoInfo {
    return (
      ws.videoByRel.get(e.rel) || {
        stem: e.stem || e.name.replace(/\.[^.]+$/, ''),
        rel: e.rel,
        name: e.name,
        size: e.size || 0,
        mtime: e.mtime || 0,
        has_results: !!e.has_results,
      }
    )
  }

  function videoStatus(v: Pick<VideoInfo, 'stem' | 'has_results'>): VideoStatus {
    return videoStatusOf(v, jobs.latestJobForStem(v.stem))
  }

  /** 当前层级的渲染行(先过滤排序,再为视频行预取状态;任务进度变化时整体重算)。 */
  function viewRows(entries: TreeEntry[]): ViewRow[] {
    return viewEntries(entries).map((e) => {
      if (e.type === 'dir' || !e.is_video) {
        return { e, video: null, status: null, fraction: null }
      }
      const video = videoFor(e)
      const job = jobs.latestJobForStem(video.stem)
      return {
        e,
        video,
        status: videoStatusOf(video, job),
        fraction: job && job.progress ? job.progress.fraction : null,
      }
    })
  }

  return { nameMatches, viewEntries, viewRows, videoFor, videoStatus }
}
