// 专家泳道面板纯逻辑(零 DOM):单帧推进 / 里程碑封顶 / 状态类 / 像素格点亮。
// 逐语义移植自 legacy expert_panel.js 与 pixel_bar.js,供 ExpertPanel.vue 与测试复用。
import type { ExpertLane } from '../../../stores/jobs'

// GET /api/expert-phases 的阶段定义(每类别 [{fraction, label}])
export type ExpertPhases = Record<string, { fraction: number; label?: string }[]> | null

export const EXPERT_CATCH_RATE = 0.3 // displayed 线性逼近 target 的恒定速率(fraction/秒)
export const EXPERT_CREEP_RATE = 0.015 // 到达 target 且仍 running 时,向下个里程碑缓行的速率
export const LANE_CELLS = 18 // 每条泳道的像素列数(3×N 网格,大方块窄间隔铺满卡宽)

// 泳道 displayed 到达 target 后的缓行封顶:阶段序列中 displayed 之后的下一个里程碑(绝不越过);
// 无阶段定义(/api/expert-phases 404)时封顶在当前 fraction+0.1 或 1.0
export function nextMilestone(
  phases: ExpertPhases, name: string, displayed: number, target: number,
): number {
  const seq = phases && phases[name]
  if (Array.isArray(seq)) {
    const next = seq
      .map((s) => s.fraction)
      .filter((f) => typeof f === 'number' && f > displayed + 0.005)
      .sort((a, b) => a - b)[0]
    if (next != null) return Math.min(next, 1)
  }
  return Math.min(Math.max(target, displayed) + 0.1, 1)
}

// 泳道状态 → 样式类:queued 灰 / running 橙 / done+detected 绿 / done+undetected 灰绿 / error 红
export function expertLaneCls(ex: ExpertLane): string {
  if (ex.status === 'running') return 'lane-running'
  if (ex.status === 'done') return ex.detected ? 'lane-detected' : 'lane-clear'
  if (ex.status === 'error') return 'lane-error'
  return 'lane-queued'
}

// 单帧推进:queued 归零;reduced-motion 直接到位;未达 target 按 CATCH 线性逼近;
// 已达 target 且仍 running 按 CREEP 向下个里程碑缓行
export function advanceLane(
  displayed: number, ex: ExpertLane, dt: number, phases: ExpertPhases, reduced: boolean,
): number {
  const target = ex.status === 'queued'
    ? 0
    : typeof ex.fraction === 'number'
      ? ex.fraction
      : ex.status === 'done'
        ? 1
        : 0
  let d = displayed
  if (ex.status === 'queued') return 0
  if (reduced) return target
  if (d < target) d = Math.min(target, d + EXPERT_CATCH_RATE * dt)
  else if (ex.status === 'running') {
    const cap = nextMilestone(phases, ex.name, d, target)
    if (d < cap) d = Math.min(cap, d + EXPERT_CREEP_RATE * dt)
  }
  return d
}

// queued 泳道仍渲染完整卡片 + 全灭像素格阵列,仅阶段文案固定为「等待调度」;
// running/done 阶段文案照旧用 label
export function phaseText(ex: ExpertLane): string {
  return ex.status === 'queued' ? '等待调度' : ex.label || ''
}

export interface PixelCell {
  lit: number // 已点亮子像素数
  frontier: number // frontier 子像素下标(-1 无)
}

// 按 displayed(0..1)点亮像素条:displayed×列数 = 整列数 + 列内小数;
// 整列子格全亮,frontier 列按小数×子格数 从上到下点亮;
// 下一个待点亮像素为 frontier(running 时 CSS 明暗脉冲)
export function laneCells(displayed: number, cells: number, subs: number, running: boolean): PixelCell[] {
  const out: PixelCell[] = []
  const pos = Math.max(0, Math.min(1, displayed)) * cells
  const full = Math.min(cells, Math.floor(pos))
  const litInFrontier = Math.min(subs - 1, Math.floor((pos - full) * subs))
  for (let i = 0; i < cells; i++) {
    const lit = i < full ? subs : i === full ? litInFrontier : 0
    const frontier = running && i === full && full < cells ? lit : -1
    out.push({ lit, frontier })
  }
  return out
}
