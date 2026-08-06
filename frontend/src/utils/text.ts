/** 文本展示纯函数:超长路径/名称的中间省略(头尾保留、中间用 …)。
 * 抽成纯函数便于 vitest 直测;组件(title 仍给完整文本)只调用显示结果。 */

/** 中间省略:超长时保留头尾、中间以单个 … 替换,结果长度 ≤ max(按字符数)。
 * 尽量在路径分隔符 '/' 处断:头取断点前最后一个分隔符,尾取断点后最近的分隔符;
 * max < 3 放不下省略号时退化为头部截断。 */
export function ellipsisMiddle(text: string, max = 48): string {
  if (text.length <= max) return text
  if (max < 3) return text.slice(0, max)
  const keep = max - 1 // … 占 1 字符,头+尾共享剩余额度
  let head = Math.ceil(keep / 2)
  // 头:断点范围内向前找最近的 '/',断在分隔符前(至少保留 1 字符)
  const hb = text.lastIndexOf('/', head)
  if (hb >= 1) head = hb
  // 尾:拿头让出的额度,向后找最近的 '/' 作为尾部起点(不能越过额度)
  let tailStart = text.length - (keep - head)
  const tb = text.indexOf('/', tailStart)
  if (tb !== -1 && text.length - tb <= keep - head) tailStart = tb
  return `${text.slice(0, head)}…${text.slice(tailStart)}`
}
