/** 对话视图展示纯函数:工具名中文映射 / Enter 发送判定 / 工作区视频预览地址推导。
 * 抽成纯函数便于 vitest 直测;ChatView 只接线调用。 */

/** 工具名 → 中文名;未知工具不在表内,显示时回退原名。 */
export const TOOL_LABELS: Record<string, string> = {
  video_meta: '视频元信息',
  extract_frames: '抽帧',
  draw_boxes: '画框标注',
  read_file: '读文件',
  write_file: '写文件',
  run_script: '运行脚本',
  submit_detection: '提交检测结果',
  load_video: '加载视频',
  spawn_subagent: '派生子代理',
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

/** 检测事件标注降级小字:meta.annotation_missing(画框失败)→「标注图生成失败」;
 * meta.annotation_not_provided(未给定位框)→「无定位框」;都不在返回 null。 */
export function detectionEventNote(
  meta: { annotation_not_provided?: number[]; annotation_missing?: number[] } | undefined,
  eventId: number,
): string | null {
  if (meta?.annotation_missing?.includes(eventId)) return '标注图生成失败'
  if (meta?.annotation_not_provided?.includes(eventId)) return '无定位框'
  return null
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
