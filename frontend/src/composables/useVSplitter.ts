/** 详情页上下分隔条(垂直版,迁移自 legacy preview.js initHSplit):
 * 比例持久化 localStorage key 沿用 ta_preview_split;默认 0.46;
 * 上栏最小 150px、最大 80%;双击复位。 */
import { onUnmounted, ref } from 'vue'
import type { Ref } from 'vue'

const HSPLIT_KEY = 'ta_preview_split'
const HSPLIT_DEFAULT = 0.46
const HSPLIT_MIN_PX = 150
const HSPLIT_MAX_RATIO = 0.8

export function useVSplitter(container: Ref<HTMLElement | null>) {
  const saved = parseFloat(localStorage.getItem(HSPLIT_KEY) || '')
  const ratio = ref(!isNaN(saved) && saved > 0 && saved <= 1 ? saved : HSPLIT_DEFAULT)
  const dragging = ref(false)

  let startY = 0
  let startHeight = 0

  function containerH(): number {
    return container.value ? container.value.getBoundingClientRect().height : 0
  }

  function onMove(e: MouseEvent) {
    const h = containerH()
    if (!h) return
    const px = Math.max(HSPLIT_MIN_PX, Math.min(h * HSPLIT_MAX_RATIO, startHeight + e.clientY - startY))
    ratio.value = px / h
  }

  function onUp() {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    dragging.value = false
    document.body.classList.remove('hsplit-dragging')
    const h = containerH()
    if (h > 0) {
      // 存比例而非像素,窗口缩放按比例重算(同 legacy)
      const topPx = container.value?.querySelector('.pane-top')?.getBoundingClientRect().height
      localStorage.setItem(HSPLIT_KEY, String(topPx ? topPx / h : ratio.value))
    }
  }

  function onPointerDown(e: MouseEvent) {
    startY = e.clientY
    startHeight = ratio.value * containerH()
    dragging.value = true
    document.body.classList.add('hsplit-dragging') // 拖拽中禁止选中文字
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    e.preventDefault()
  }

  /** 双击复位默认比例(同 legacy)。 */
  function reset() {
    ratio.value = HSPLIT_DEFAULT
    localStorage.setItem(HSPLIT_KEY, String(HSPLIT_DEFAULT))
  }

  onUnmounted(() => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.classList.remove('hsplit-dragging')
  })

  return { ratio, dragging, onPointerDown, reset }
}
