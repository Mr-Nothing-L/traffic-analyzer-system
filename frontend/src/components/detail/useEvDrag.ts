/** 证据画布拖拽与选中(从 EvidenceCanvas.vue 拆出,行数纪律):
 * 顶点/角点/身体三类拖拽,document 级 move/up(拖出把手不断线);
 * 空拖(未移动)点击证据框身体 → 选中并弹出标签条。 */
import { onBeforeUnmount, ref } from 'vue'
import type { Ref } from 'vue'
import type { BoxShape, Handle, Shape } from './evidenceGeo'
import { clamp01, hitBody, moveBody, movePoint } from './evidenceGeo'

interface DragState {
  shape: Shape
  kind: 'vertex' | 'corner' | 'body'
  idx: number
  moved: boolean
  last: [number, number]
}

export interface EvDragOptions {
  stageEl: Ref<HTMLElement | null>
  canvasEl: Ref<HTMLCanvasElement | null>
  getShapes: () => Shape[]
  /** 当前舞台 CSS 像素尺寸(W/H 为 0 时说明帧图未加载,拖拽不会发生)。 */
  getSize: () => { W: number; H: number }
  onGeom: () => void // 几何变化(触发重绘)
  onDirty: () => void // 标 dirty
}

export function useEvDrag(opts: EvDragOptions) {
  const selectedBox = ref<BoxShape | null>(null)
  const labelText = ref('')
  let drag: DragState | null = null

  function posOf(e: MouseEvent): { x: number; y: number } {
    const rect = opts.stageEl.value!.getBoundingClientRect()
    return { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  function selectBox(sh: BoxShape | null) {
    selectedBox.value = sh
    labelText.value = sh ? sh.region.label || '' : ''
    opts.onGeom()
  }

  function onLabelInput() {
    if (!selectedBox.value) return
    selectedBox.value.region.label = labelText.value
    opts.onDirty()
    opts.onGeom()
  }

  function onDocMove(e: MouseEvent) {
    if (!drag) return
    drag.moved = true
    const { W, H } = opts.getSize()
    const p = posOf(e)
    const fx = clamp01(p.x / W)
    const fy = clamp01(p.y / H)
    if (drag.kind !== 'body') {
      movePoint(drag.shape, drag.kind, drag.idx, fx, fy)
    } else {
      moveBody(drag.shape, fx - drag.last[0], fy - drag.last[1])
      drag.last = [fx, fy]
    }
    opts.onGeom()
  }

  function onDocUp() {
    document.removeEventListener('mousemove', onDocMove)
    document.removeEventListener('mouseup', onDocUp)
    if (drag) {
      if (drag.moved) opts.onDirty()
      else if (drag.kind === 'body' && drag.shape.type === 'box') selectBox(drag.shape)
      drag = null
    }
  }

  function startDrag(shape: Shape, kind: DragState['kind'], idx: number, e: MouseEvent) {
    const { W, H } = opts.getSize()
    const p = posOf(e)
    drag = { shape, kind, idx, moved: false, last: [clamp01(p.x / W), clamp01(p.y / H)] }
    document.addEventListener('mousemove', onDocMove)
    document.addEventListener('mouseup', onDocUp)
    e.preventDefault()
  }

  function onHandleDown(h: Handle, e: MouseEvent) {
    startDrag(h.shape, h.kind, h.idx, e)
  }

  function onCanvasDown(e: MouseEvent) {
    const { W, H } = opts.getSize()
    const p = posOf(e)
    const hit = hitBody(opts.getShapes(), p.x, p.y, W, H)
    if (hit) startDrag(hit, 'body', -1, e)
    else if (selectedBox.value) selectBox(null)
  }

  function onCanvasMove(e: MouseEvent) {
    if (drag || !opts.canvasEl.value) return
    const { W, H } = opts.getSize()
    const p = posOf(e)
    opts.canvasEl.value.style.cursor = hitBody(opts.getShapes(), p.x, p.y, W, H)
      ? 'move'
      : 'crosshair'
  }

  onBeforeUnmount(() => {
    document.removeEventListener('mousemove', onDocMove)
    document.removeEventListener('mouseup', onDocUp)
  })

  return {
    selectedBox, labelText, selectBox, onLabelInput,
    onHandleDown, onCanvasDown, onCanvasMove,
  }
}
