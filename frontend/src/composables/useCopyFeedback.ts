/** 「复制成功 → ✓ → 1 秒后复原」反馈状态机:copyText 成功置 copiedKey,
 * 1 秒后复位;失败静默保持原样(低调调试辅助口径)。同一 composable 实例
 * 内多次复制互斥(新复制重置计时),以 key 区分同一组件内多处复制按钮。 */
import { ref } from 'vue'
import { copyText } from '../utils/chatDisplay'

export function useCopyFeedback() {
  const copiedKey = ref<string | null>(null)
  let timer: ReturnType<typeof setTimeout> | null = null

  async function copyWithFeedback(key: string, text: string): Promise<void> {
    try {
      await copyText(text)
    } catch {
      return
    }
    copiedKey.value = key
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => {
      copiedKey.value = null
      timer = null
    }, 1000)
  }

  return { copiedKey, copyWithFeedback }
}
