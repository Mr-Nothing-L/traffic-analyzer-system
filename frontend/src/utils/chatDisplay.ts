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
}

/** 工具条目显示名:已知工具「中文名(原名)」,未知工具回退原名。 */
export function toolLabel(name: string): string {
  const label = TOOL_LABELS[name]
  return label ? `${label}(${name})` : name
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
