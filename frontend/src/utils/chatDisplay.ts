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
