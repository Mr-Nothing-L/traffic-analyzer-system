/** 证据画布几何与绘制(从 EvidenceCanvas.vue 拆出,逻辑同 legacy evidence.js):
 * 形状构建、命中测试、单点移动、canvas 绘制。颜色由调用方从 token 读入。 */
import type { EvidenceEvent, EvidenceRegion } from '../../api/results'

export interface PolyShape {
  type: 'poly'
  kind: 'emergency' | 'chevron'
  pts: number[][]
}
export interface BoxShape {
  type: 'box'
  region: EvidenceRegion
}
export type Shape = PolyShape | BoxShape

export interface CanvasColors {
  emergency: string
  chevron: string
  box: string
  onAccent: string
}

export const clamp01 = (v: number) => Math.max(0, Math.min(1, v))

/** 端点/角点把手:可 focus 的 DOM 元素(拖拽 + 键盘微调),x/y 为归一化坐标。 */
export interface Handle {
  key: string
  shape: Shape
  kind: 'vertex' | 'corner'
  idx: number
  x: number
  y: number
  cls: string
  label: string
}

/** 从形状列表构建把手(顶点 + 证据框角点)。 */
export function buildHandles(shapes: Shape[]): Handle[] {
  const out: Handle[] = []
  shapes.forEach((sh, si) => {
    if (sh.type === 'poly') {
      const zh = sh.kind === 'emergency' ? '应急车道' : '导流区'
      sh.pts.forEach((p, i) =>
        out.push({
          key: si + ':v' + i, shape: sh, kind: 'vertex', idx: i,
          x: p[0], y: p[1], cls: 'ev-handle-' + sh.kind, label: zh + '端点 ' + (i + 1),
        }),
      )
    } else {
      boxCorners(sh.region.box_rel!).forEach((c, i) =>
        out.push({
          key: si + ':c' + i, shape: sh, kind: 'corner', idx: i,
          x: c[0], y: c[1], cls: 'ev-handle-box', label: '证据框角点 ' + (i + 1),
        }),
      )
    }
  })
  return out
}

/** 证据框四角,顺序:0 左上 1 右上 2 右下 3 左下(同 legacy)。 */
export function boxCorners(b: number[]): number[][] {
  return [
    [b[0], b[1]],
    [b[2], b[1]],
    [b[2], b[3]],
    [b[0], b[3]],
  ]
}

/** 从事件 draft 构建形状列表(直接引用 draft 数据,拖拽即改 draft)。 */
export function buildShapes(ev: EvidenceEvent): Shape[] {
  const out: Shape[] = []
  const calib = ev.calibration || {}
  if (Array.isArray(calib.emergency_polygon_rel))
    out.push({ type: 'poly', kind: 'emergency', pts: calib.emergency_polygon_rel })
  if (Array.isArray(calib.chevron_polygon_rel))
    out.push({ type: 'poly', kind: 'chevron', pts: calib.chevron_polygon_rel })
  ;(Array.isArray(ev.evidence_regions) ? ev.evidence_regions : []).forEach((r) => {
    if (r && Array.isArray(r.box_rel)) out.push({ type: 'box', region: r })
  })
  return out
}

/** 单点移动(归一化坐标):vertex 直接改;corner 改后规范化回 [x1,y1,x2,y2]。 */
export function movePoint(sh: Shape, kind: 'vertex' | 'corner', idx: number, fx: number, fy: number) {
  fx = clamp01(fx)
  fy = clamp01(fy)
  if (kind === 'vertex' && sh.type === 'poly') {
    sh.pts[idx][0] = fx
    sh.pts[idx][1] = fy
  } else if (kind === 'corner' && sh.type === 'box') {
    const b = sh.region.box_rel!
    const c = boxCorners(b)
    c[idx] = [fx, fy]
    const xs = c.map((q) => q[0])
    const ys = c.map((q) => q[1])
    b[0] = Math.min(...xs)
    b[1] = Math.min(...ys)
    b[2] = Math.max(...xs)
    b[3] = Math.max(...ys)
  }
}

/** 身体整体平移(归一化增量)。 */
export function moveBody(sh: Shape, dx: number, dy: number) {
  if (sh.type === 'poly') {
    sh.pts.forEach((pt) => {
      pt[0] = clamp01(pt[0] + dx)
      pt[1] = clamp01(pt[1] + dy)
    })
  } else {
    const b = sh.region.box_rel!
    const w = b[2] - b[0]
    const h = b[3] - b[1]
    b[0] = clamp01(b[0] + dx)
    b[1] = clamp01(b[1] + dy)
    b[2] = clamp01(b[0] + w)
    b[3] = clamp01(b[1] + h)
  }
}

export function pointInPoly(px: number, py: number, pts: number[][], W: number, H: number): boolean {
  let inside = false
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const xi = pts[i][0] * W
    const yi = pts[i][1] * H
    const xj = pts[j][0] * W
    const yj = pts[j][1] * H
    if (yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

/** 身体命中(顶点/角点由 DOM 把手覆盖,canvas 只测身体),后画优先。 */
export function hitBody(shapes: Shape[], x: number, y: number, W: number, H: number): Shape | null {
  for (let i = shapes.length - 1; i >= 0; i--) {
    const sh = shapes[i]
    if (sh.type === 'poly') {
      if (pointInPoly(x, y, sh.pts, W, H)) return sh
    } else {
      const b = sh.region.box_rel!
      if (x >= b[0] * W && x <= b[2] * W && y >= b[1] * H && y <= b[3] * H) return sh
    }
  }
  return null
}

/** 绘制全部形状(端点/角点把手由 DOM 渲染,canvas 只画本体)。 */
export function drawShapes(
  ctx: CanvasRenderingContext2D,
  shapes: Shape[],
  W: number,
  H: number,
  selected: BoxShape | null,
  colors: CanvasColors,
) {
  ctx.clearRect(0, 0, W, H)
  shapes.forEach((sh) => {
    const color = sh.type === 'poly' ? colors[sh.kind] : colors.box
    ctx.lineWidth = 2
    ctx.strokeStyle = color
    ctx.fillStyle = color
    if (sh.type === 'poly') {
      const pts = sh.pts
      if (!pts.length) return
      ctx.beginPath()
      ctx.moveTo(pts[0][0] * W, pts[0][1] * H)
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0] * W, pts[i][1] * H)
      ctx.closePath()
      ctx.globalAlpha = 0.13
      ctx.fill()
      ctx.globalAlpha = 1
      ctx.stroke()
    } else {
      const b = sh.region.box_rel!
      const x = b[0] * W
      const y = b[1] * H
      const w = (b[2] - b[0]) * W
      const h = (b[3] - b[1]) * H
      ctx.globalAlpha = 0.13
      ctx.fillRect(x, y, w, h)
      ctx.globalAlpha = 1
      if (sh === selected) ctx.setLineDash([6, 4])
      ctx.strokeRect(x, y, w, h)
      ctx.setLineDash([])
      if (sh.region.label) {
        ctx.font = '12px sans-serif'
        const tw = ctx.measureText(sh.region.label).width
        const ly = Math.max(14, y - 6)
        ctx.fillRect(x, ly - 13, tw + 10, 16)
        ctx.fillStyle = colors.onAccent
        ctx.fillText(sh.region.label, x + 5, ly - 1)
      }
    }
  })
}
