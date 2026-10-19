/** transport 类错误(网络/代理层)的显式重试入口。
 * naive-ui message 不支持动作按钮,用 dialog 提供「重试」;文案全中文、动词开头
 * (design.md 诚实文案),不做静默自动重试。 */
import { useDialog } from 'naive-ui'

export function useRetryAction() {
  const dialog = useDialog()

  /** 提示「疑似网络/代理层错误,请重试」并提供重试动作;onRetry 再次失败时
   * 由调用方自身的错误处理递归回到这里(天然形成「失败→可再重试」循环)。 */
  function offerRetry(onRetry: () => void | Promise<void>) {
    dialog.error({
      title: '操作未完成',
      content: '疑似网络/代理层错误,请重试。',
      positiveText: '重试',
      negativeText: '取消',
      onPositiveClick: () => onRetry(),
    })
  }

  return { offerRetry }
}
