<script setup lang="ts">
/** 证据画布:帧图 + 多边形/证据框叠加。形状本体画在 canvas;
 * 端点/角点是可 focus 的 DOM 把手(拖拽 + 方向键微调 1px,Shift=10px)。
 * 交互迁移自 legacy evidence.js mountEvidencePane;
 * 几何/绘制见 evidenceGeo.ts,拖拽/选中见 useEvDrag.ts。 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import type { EvidenceEvent, EvidenceVideoInfo, VideoSource } from '../../api/results'
import { frameUrl, resultFileUrl } from '../../api/results'
import { useEvidenceStore } from '../../stores/evidence'
import type { CanvasColors, Handle, Shape } from './evidenceGeo'
import { buildHandles, buildShapes, drawShapes, movePoint } from './evidenceGeo'
import { useEvDrag } from './useEvDrag'

const props = defineProps<{
  stem: string
  source: VideoSource
  ev: EvidenceEvent
  videoInfo: EvidenceVideoInfo
}>()
const store = useEvidenceStore()

const shapes = computed<Shape[]>(() => buildShapes(props.ev))
const regions = computed(() =>
  Array.isArray(props.ev.evidence_regions) ? props.ev.evidence_regions : [],
)
const galleryImgs = computed<string[]>(() => {
  const out: string[] = []
  regions.value.forEach((r) => {
    if (r && r.image) out.push(r.image)
  })
  if (Array.isArray(props.ev.gallery_images)) out.push(...props.ev.gallery_images)
  return out
})
const hasGeom = computed(() => shapes.value.length > 0)
const maxFrame = computed(() =>
  Math.max(0, Math.round((props.videoInfo.duration_sec || 0) * (props.videoInfo.fps || 0)) - 1),
)
const initFrame = (() => {
  const calib = props.ev.calibration || {}
  if (calib.frame_index != null) return calib.frame_index
  const r = regions.value.find((x) => x && x.frame_index != null)
  return r && r.frame_index != null ? r.frame_index : 0
})()
const frameIdx = ref(initFrame)
const imgSrc = ref(frameUrl(props.source, initFrame))

/* ---- 画布 ---- */
const stageEl = ref<HTMLElement | null>(null)
const imgEl = ref<HTMLImageElement | null>(null)
const canvasEl = ref<HTMLCanvasElement | null>(null)
let W = 0
let H = 0 // CSS 像素尺寸
const geomTick = ref(0) // 几何变更计数:触发 canvas 重绘
const colors: CanvasColors = { emergency: '', chevron: '', box: '', onAccent: '' }

function draw() {
  const canvas = canvasEl.value
  if (!canvas || !W || !H) return
  const ctx = canvas.getContext('2d')
  if (ctx) drawShapes(ctx, shapes.value, W, H, drag.selectedBox.value, colors)
}
watch(geomTick, draw)

function fit() {
  const img = imgEl.value
  const canvas = canvasEl.value
  if (!img || !canvas) return
  const w = img.clientWidth
  const h = img.clientHeight
  if (!w || !h) return
  W = w
  H = h
  const dpr = window.devicePixelRatio || 1
  canvas.width = Math.round(w * dpr)
  canvas.height = Math.round(h * dpr)
  canvas.style.width = w + 'px'
  canvas.style.height = h + 'px'
  canvas.getContext('2d')?.setTransform(dpr, 0, 0, dpr, 0, 0)
  draw()
}

/* ---- 拖拽 / 选中(逻辑在 useEvDrag) ---- */
const drag = useEvDrag({
  stageEl,
  canvasEl,
  getShapes: () => shapes.value,
  getSize: () => ({ W, H }),
  onGeom: () => geomTick.value++,
  onDirty: () => store.markDirty(),
})

/* ---- 把手(端点/角点):可 focus,拖拽或方向键微调 ---- */
const handles = computed<Handle[]>(() => buildHandles(shapes.value))

function onHandleKey(h: Handle, e: KeyboardEvent) {
  const step = e.shiftKey ? 10 : 1
  const dirs: Record<string, [number, number]> = {
    ArrowLeft: [-step / (W || 1), 0],
    ArrowRight: [step / (W || 1), 0],
    ArrowUp: [0, -step / (H || 1)],
    ArrowDown: [0, step / (H || 1)],
  }
  const d = dirs[e.key]
  if (!d) return
  e.preventDefault()
  movePoint(h.shape, h.kind, h.idx, h.x + d[0], h.y + d[1])
  geomTick.value++
  store.markDirty()
}

/* ---- 帧切换 / 画廊 ---- */
function setFrame(i: number) {
  frameIdx.value = Math.max(0, Math.min(maxFrame.value || i, i))
  imgSrc.value = frameUrl(props.source, frameIdx.value)
}

function onFrameInput(e: Event) {
  setFrame(+(e.target as HTMLInputElement).value || 0)
}

function onGalImg(e: Event) {
  ;(e.target as HTMLImageElement).classList.add('loaded') // 模糊→清晰过渡(失败也摘模糊)
}

function openImage(name: string) {
  window.open(resultFileUrl(props.stem, name), '_blank')
}

/* ---- 生命周期 ---- */
let ro: ResizeObserver | null = null
onMounted(() => {
  const cs = getComputedStyle(document.documentElement) // canvas 只认计算色,源仍是 token
  colors.emergency = cs.getPropertyValue('--color-accent').trim()
  colors.chevron = cs.getPropertyValue('--color-blue').trim()
  colors.box = cs.getPropertyValue('--color-sage').trim()
  colors.onAccent = cs.getPropertyValue('--color-on-accent').trim()
  ro = new ResizeObserver(fit) // 分隔条拖动等容器尺寸变化不触发 window resize
  if (stageEl.value) ro.observe(stageEl.value)
  window.addEventListener('resize', fit)
})
onBeforeUnmount(() => {
  ro?.disconnect()
  window.removeEventListener('resize', fit)
})
</script>

<template>
  <div v-if="!hasGeom && !galleryImgs.length" class="ev-empty">
    该事件无可视化证据(未检出或无坐标数据)
  </div>
  <div v-else class="ev-pane">
    <div class="ev-toolbar">
      <n-button size="small" quaternary @click="setFrame(frameIdx - 1)">◀ 上一帧</n-button>
      <span>
        帧
        <input
          class="ev-frame-input"
          type="number"
          :min="0"
          :max="maxFrame"
          :value="frameIdx"
          @change="onFrameInput"
        />
        {{ maxFrame ? ' / ' + maxFrame : '' }}
      </span>
      <n-button size="small" quaternary @click="setFrame(frameIdx + 1)">下一帧 ▶</n-button>
      <span class="ev-legend">
        <span><i class="sw sw-emergency"></i>应急车道</span>
        <span><i class="sw sw-chevron"></i>导流区</span>
        <span><i class="sw sw-box"></i>证据框</span>
      </span>
    </div>
    <div v-if="hasGeom" ref="stageEl" class="ev-stage">
      <img ref="imgEl" class="ev-img" :src="imgSrc" alt="帧图" @load="fit" />
      <canvas
        ref="canvasEl"
        class="ev-canvas"
        @mousedown="drag.onCanvasDown"
        @mousemove="drag.onCanvasMove"
      />
      <button
        v-for="h in handles"
        :key="h.key"
        type="button"
        class="ev-handle"
        :class="h.cls"
        :style="{ left: h.x * 100 + '%', top: h.y * 100 + '%' }"
        :aria-label="h.label"
        :title="h.label + '(方向键微调,Shift 加速)'"
        @mousedown="drag.onHandleDown(h, $event)"
        @keydown="onHandleKey(h, $event)"
      />
    </div>
    <div v-if="drag.selectedBox.value" class="ev-labelbar">
      <span>证据框标签</span>
      <input
        v-model="drag.labelText.value"
        class="ev-label-input"
        type="text"
        @input="drag.onLabelInput"
      />
      <n-button size="small" quaternary @click="drag.selectBox(null)">取消选择</n-button>
    </div>
    <div v-if="galleryImgs.length" class="ev-gallery">
      <img
        v-for="name in galleryImgs"
        :key="name"
        :src="resultFileUrl(stem, name)"
        :alt="name"
        :title="name"
        loading="lazy"
        @load="onGalImg"
        @error="onGalImg"
        @click="openImage(name)"
      />
    </div>
  </div>
</template>
