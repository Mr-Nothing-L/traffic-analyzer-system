/** 对话视图展示纯函数:工具名中文映射 / Enter 发送判定 / 工作区视频预览地址推导。
 * 抽成纯函数便于 vitest 直测;ChatView 只接线调用。 */

/** 工具名 → 中文名;未知工具不在表内,显示时回退原名。 */
export const TOOL_LABELS: Record<string, string> = {
  video_meta: '视频元信息',
  extract_frames: '抽帧',
  draw_boxes: '画框标注',
  track_suspects: '定向跟踪',
  read_file: '读文件',
  write_file: '写文件',
  run_script: '运行脚本',
  run_command: '运行命令',
  job_list: '后台任务列表',
  job_output: '后台任务输出',
  job_kill: '终止后台任务',
  edit_file: '编辑文件',
  glob_files: '文件搜索',
  grep_files: '文本搜索',
  todo_write: '任务清单',
  web_fetch: '网页抓取',
  submit_detection: '提交检测结果',
  load_video: '加载视频',
  spawn_subagent: '派生子代理',
  subagent_list: '子代理列表',
  subagent_report: '子代理报告',
}

/** 工具条目显示名:已知工具「中文名(原名)」,未知工具回退原名。 */
export function toolLabel(name: string): string {
  const label = TOOL_LABELS[name]
  return label ? `${label}(${name})` : name
}

/** 时间线渲染条目:剔除 tool 条目——工具调用统一走分析链路节点展示
 * (链路节点即工具条目,展开看明细),审批/系统/检测等条目原位保留。 */
export function timelineEntries<T extends { kind: string }>(entries: readonly T[]): T[] {
  return entries.filter((e) => e.kind !== 'tool')
}

/** 工具轮扫描(chatDisplay 内部共用):轮 = 相邻 user 条目之间(首条 user 前的
 * 条目也算独立一轮),与 buildAnalysisFlow 的区间边界同口径。对每一轮回调
 * 轮内数据与是否最后一轮。 */
interface ToolRound {
  /** 轮内全部 assistant 条目 id(按序)。 */
  assistantIds: string[]
  /** 其中位于 detection 条目之后的 id(submit_detection 之后的收尾条目)。 */
  afterDetectionIds: string[]
  hasTool: boolean
  hasDetection: boolean
}

function scanToolRounds(
  entries: ReadonlyArray<{ kind: string; id: string }>,
  visit: (round: ToolRound, isLast: boolean) => void,
): void {
  const empty = (): ToolRound => ({
    assistantIds: [],
    afterDetectionIds: [],
    hasTool: false,
    hasDetection: false,
  })
  let round = empty()
  for (const e of entries) {
    if (e.kind === 'user') {
      visit(round, false)
      round = empty()
    } else if (e.kind === 'assistant') {
      round.assistantIds.push(e.id)
      if (round.hasDetection) round.afterDetectionIds.push(e.id)
    } else if (e.kind === 'tool') {
      round.hasTool = true
    } else if (e.kind === 'detection') {
      round.hasDetection = true
    }
  }
  visit(round, true)
}

/** 轮次的链路是否有面板承接:轮内有工具调用 **且** 轮次进行中(实时面板)或
 * 轮内已有 detection 条目(冻结面板)。 */
function roundPanelized(round: ToolRound, isLast: boolean, live: boolean | undefined): boolean {
  return round.hasTool && (round.hasDetection || (isLast && live === true))
}

/** 时间线气泡 thinking 应隐藏(改由分析链路面板呈现)的 assistant 条目 id 集合。
 * 口径:轮内有工具调用 **且** 该轮的思考有链路面板承载——轮次进行中(实时面板)
 * 或轮内已有 detection 条目(冻结面板);且仅覆盖位于 detection 条目**之前**的
 * 条目——detection 之后的收尾条目不在面板区间内,其 thinking 须在气泡中原样显示,
 * 否则两处都看不到。有工具但既未进行中也无 detection 的轮次(如中途调过工具的
 * 追问),没有面板承接,气泡保持原样。轮次 = 相邻 user 条目之间(首条 user 前的
 * 条目也算独立一轮),与 buildAnalysisFlow 的区间边界同口径;live=true 时最后
 * 一轮视为进行中。 */
export function toolRoundAssistantIds(
  entries: ReadonlyArray<{ kind: string; id: string }>,
  options: { live?: boolean } = {},
): Set<string> {
  const ids = new Set<string>()
  scanToolRounds(entries, (round, isLast) => {
    if (roundPanelized(round, isLast, options.live)) {
      const after = new Set(round.afterDetectionIds)
      for (const id of round.assistantIds) if (!after.has(id)) ids.add(id)
    }
  })
  return ids
}

/** 气泡正文应隐藏(改由分析链路面板「说明」节点呈现)的 assistant 条目 id 集合。
 * 口径与 toolRoundAssistantIds 一致,但 submit_detection 之后的收尾正文(位于
 * detection 条目之后的 assistant 条目)不隐藏——它在面板区间之外,仍作普通气泡
 * 跟在检测卡后;纯问答轮次(无工具)不出面板,正文照常显示。 */
export function toolRoundAssistantTextIds(
  entries: ReadonlyArray<{ kind: string; id: string }>,
  options: { live?: boolean } = {},
): Set<string> {
  const ids = new Set<string>()
  scanToolRounds(entries, (round, isLast) => {
    if (!roundPanelized(round, isLast, options.live)) return
    const after = new Set(round.afterDetectionIds)
    for (const id of round.assistantIds) {
      if (!after.has(id)) ids.add(id)
    }
  })
  return ids
}

/** 本轮真实起点:最后一条 user 条目的 at(轮次秒表从提问时刻起算,切出再切回
 * 正在分析的会话不重计);无 user 条目或缺 at(旧数据)返回 null,调用方回退。 */
export function lastUserEntryAt(
  entries: ReadonlyArray<{ kind: string; at?: number }>,
): number | null {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i]!
    if (e.kind === 'user' && typeof e.at === 'number') return e.at
  }
  return null
}

/** 复制文本到剪贴板:优先异步 Clipboard API;非安全上下文(如局域网 IP
 * 直连,navigator.clipboard 为 undefined)回退隐藏 textarea + execCommand('copy')。
 * 失败抛错,成功/失败提示由调用方负责。 */
export async function copyText(text: string): Promise<void> {
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined
  if (clipboard?.writeText) {
    await clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  // 不可见但保持可选取(display:none 的元素 select() 无效)
  ta.style.position = 'fixed'
  ta.style.top = '0'
  ta.style.left = '0'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.focus()
  ta.select()
  try {
    if (!document.execCommand('copy')) throw new Error('execCommand copy 返回 false')
  } finally {
    ta.remove()
  }
}

/** composer 的 Enter 是否触发发送:Shift+Enter 换行不发送;
 * 中文输入法合成态(isComposing / keyCode 229)中的 Enter 是选词上屏,不发送。 */
export function shouldSendOnEnter(ev: {
  shiftKey: boolean
  isComposing?: boolean
  keyCode?: number
}): boolean {
  if (ev.shiftKey) return false
  if (ev.isComposing || ev.keyCode === 229) return false
  return true
}

/** 思考折叠行的单行摘要:运行中显示最后一个非空行(跟随最新进展),
 * 结束后显示第一个非空行(概括思考起点);无内容返回空串。 */
export function thinkSummaryLine(think: string, running: boolean): string {
  const lines = think.split('\n').filter((l) => l.trim())
  if (!lines.length) return ''
  return (running ? lines[lines.length - 1] : lines[0]).trim()
}

/** 正文折叠预览(链路面板「说明」节点):前 N 个非空行(默认两行),
 * 行首尾空白裁掉;无内容返回空串。 */
export function textPreviewLines(text: string, max = 2): string {
  return text
    .split('\n')
    .filter((l) => l.trim())
    .slice(0, max)
    .map((l) => l.trim())
    .join('\n')
}

/** 工具失败摘要:结果文本首个非空行(截断 80 字),无内容回退「未知错误」。 */
export function toolErrorSummary(result: string): string {
  const line = (result.split('\n').find((l) => l.trim()) ?? '').trim()
  if (!line) return '未知错误'
  return line.length > 80 ? `${line.slice(0, 80)}…` : line
}

/** user 气泡视频预览地址:当次上传附件有 src 直接用;
 * 否则由 videoPath 确定性推 /api/workspace/stream —— 工作区相对路径直接用,
 * 工作区内绝对路径(如 .agent/uploads 落盘文件)剥掉工作区前缀转相对;
 * 推不出(工作区外绝对路径)返回 null,调用方回退路径 chip。
 * 同一路径恒得同一地址,历史会话重载后仍可显示。 */
export function workspaceVideoSrc(
  videoPath?: string,
  videoSrc?: string,
  workspaceDir?: string | null,
): string | null {
  if (videoSrc) return videoSrc
  if (!videoPath) return null
  let rel = videoPath
  const isAbs = videoPath.startsWith('/') || /^[A-Za-z]:[\\/]/.test(videoPath)
  if (isAbs) {
    if (!workspaceDir) return null
    const root = workspaceDir.replace(/[\\/]+$/, '')
    if (rel !== root && !rel.startsWith(`${root}/`) && !rel.startsWith(`${root}\\`)) {
      return null
    }
    rel = rel.slice(root.length).replace(/^[\\/]+/, '')
    if (!rel) return null
  }
  return `/api/workspace/stream?path=${encodeURIComponent(rel)}`
}

/** track_suspects 取证产物视图:从工具结果文本解析产物行并推导叠加视频地址。
 * 产物行由 agent/src/tools/builtin/trackSuspects.ts 以纯文本附在输出末:
 * 「取证产物已保存:目录 <dir>;轨迹片段 <clip>;数据表 <csv>(供用户复核与引用)」
 * (全半角冒号/分号/括号兼容)。三段路径 toolserver 端可能为 None,经 agent
 * 模板插值成字面量「null」,按缺失处理;行不存在(业务失败回退文本、其他工具、
 * 旧会话数据)整体返回 null。clip 是工作区内跟踪叠加视频相对路径
 * (.agent/tracks/<stem>/<ts>/track_overlay.mp4),地址推导复用 workspaceVideoSrc,
 * 保持前端路径解析单一来源。 */
export interface TrackSuspectsView {
  dir: string | null
  clip: string | null
  csv: string | null
  /** 叠加视频可播放地址;clip 缺失或推不出时为 null(UI 降级只显示路径文本)。 */
  videoSrc: string | null
}

const TRACK_ARTIFACTS_LINE_RE =
  /取证产物已保存[:：]\s*目录\s*(.+?)\s*[;；]\s*轨迹片段\s*(.+?)\s*[;；]\s*数据表\s*(.+?)\s*(?:[(（][^()（）]*[)）])?\s*$/m

export function trackSuspectsView(
  result: string,
  workspaceDir?: string | null,
): TrackSuspectsView | null {
  const m = TRACK_ARTIFACTS_LINE_RE.exec(result)
  if (!m) return null
  const [rawDir = '', rawClip = '', rawCsv = ''] = m.slice(1)
  const part = (raw: string): string | null => {
    const t = raw.trim()
    return t && t !== 'null' ? t : null
  }
  const dir = part(rawDir)
  const clip = part(rawClip)
  const csv = part(rawCsv)
  if (!dir && !clip && !csv) return null
  return {
    dir,
    clip,
    csv,
    videoSrc: clip ? workspaceVideoSrc(clip, undefined, workspaceDir ?? null) : null,
  }
}
