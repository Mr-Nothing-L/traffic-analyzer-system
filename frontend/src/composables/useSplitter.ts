/** 侧栏拖拽分隔条:宽度 180–560px,localStorage 持久化,双击复位 264。
 * key 沿用 legacy main.js 的 ta_sidebar_width。 */
import { onUnmounted, ref } from 'vue'

const SIDEBAR_WIDTH_KEY = 'ta_sidebar_width'
const SIDEBAR_DEFAULT_WIDTH = 264
const SIDEBAR_MIN_WIDTH = 180
const SIDEBAR_MAX_WIDTH = 560

function clamp(px: number): number {
  return Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.round(px)))
}

export function useSplitter() {
  const saved = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY) || '', 10)
  const width = ref(isNaN(saved) ? SIDEBAR_DEFAULT_WIDTH : clamp(saved))
  const dragging = ref(false)

  let startX = 0
  let startWidth = 0

  function onMove(e: MouseEvent) {
    width.value = clamp(startWidth + e.clientX - startX)
  }

  function onUp() {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    dragging.value = false
    document.body.classList.remove('splitter-dragging')
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(width.value))
  }

  function onPointerDown(e: MouseEvent) {
    startX = e.clientX
    startWidth = width.value
    dragging.value = true
    document.body.classList.add('splitter-dragging') // 拖拽中禁止选中文字
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    e.preventDefault()
  }

  /** 双击复位默认宽度(同 legacy)。 */
  function reset() {
    width.value = SIDEBAR_DEFAULT_WIDTH
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(SIDEBAR_DEFAULT_WIDTH))
  }

  onUnmounted(() => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.classList.remove('splitter-dragging')
  })

  return { width, dragging, onPointerDown, reset }
}
