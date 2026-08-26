/** 流式增量 markdown 渲染(思路参考 deepseek-harness 的 incremental.ts,
 * 适配本项目 mdToHtml 的字符串→HTML 方案):
 * 每条 text_delta 都整体重跑 mdToHtml 是 O(n²),长报告会卡。这里按空行(\n\n)
 * 把已累计文本切成块,除末尾 UNSTABLE_TAIL_BLOCKS 个块外的已完成块只渲染一次
 * 并缓存 HTML(冻结),后续增量只重渲染尾部。
 *
 * 安全边界:代码围栏(``` 行)未闭合时,围栏内的空行不构成块边界——
 * 避免把未闭合的 ``` 冻结进缓存(冻结后围栏永远闭不上,后续文本全被吞)。
 * 围栏闭合后的下一个空行才恢复可切。
 *
 * 冻结块以「块在源码中的起始偏移」为 key:追加式流不会改已冻结区,偏移稳定,
 * Vue 按条目 id 复用组件实例、按偏移复用 DOM 而不是重挂(每个 assistant 条目
 * 一个渲染器实例,随条目生灭;换会话/撤回后是新条目新实例,不再需要 generation
 * 之类的重置补偿)。
 * settled(轮次结束)后由调用方一次性 mdToHtml 完整渲染,自愈增量期的边界偏差。 */
import { mdToHtml } from './markdown'

/** 末尾保留的不稳定块数:追加文本最多重塑最后一个块,多留一个作安全边距。 */
const UNSTABLE_TAIL_BLOCKS = 2

/** 一个源码块:[start, end) 为块文本在完整源码中的偏移(不含块间空行)。 */
export interface MdBlock {
  start: number
  end: number
}

/** 按空行切块;围栏内的空行不切(并入当前块)。返回块按起始偏移升序。 */
export function splitMdBlocks(text: string): MdBlock[] {
  const blocks: MdBlock[] = []
  let inFence = false
  let blockStart = 0
  let offset = 0
  for (const line of text.split('\n')) {
    const lineStart = offset
    offset += line.length + 1 // +1 为被 split 吃掉的 \n;末行多算无妨(仅用于后续 blockStart)
    if (/^```/.test(line)) inFence = !inFence
    if (!inFence && line.trim() === '') {
      // 块结束于空行之前的那个 \n(lineStart-1),块文本不含行尾换行
      if (lineStart - 1 > blockStart) blocks.push({ start: blockStart, end: lineStart - 1 })
      blockStart = offset
    }
  }
  if (text.length > blockStart) blocks.push({ start: blockStart, end: text.length })
  return blocks
}

/** 冻结块:HTML 字符串 + 稳定渲染 key(源码起始偏移)。 */
export interface FrozenMdBlock {
  key: number
  html: string
}

export interface IncrementalMdResult {
  /** 已冻结块(只增不改);追加式流下 html 不重复计算。 */
  frozen: FrozenMdBlock[]
  /** 末尾不稳定区(至多 UNSTABLE_TAIL_BLOCKS 个块 + 增长)的整体渲染。 */
  tailHtml: string
}

/** 单条流式 assistant 文本的增量渲染器;一个实例跟随一条消息(随条目生灭)。 */
export class IncrementalMd {
  private prevText = ''
  /** 冻结缓存:块起始偏移 → HTML(追加流下偏移不变,命中即不重解析)。 */
  private cache = new Map<number, string>()
  private cached: IncrementalMdResult | null = null

  /** 折叠当前已累计文本,返回 冻结块 + 尾部 HTML。同文本幂等(渲染路径可反复调)。 */
  update(text: string): IncrementalMdResult {
    if (this.cached !== null && text === this.prevText) return this.cached
    if (!text.startsWith(this.prevText)) {
      this.cache.clear() // 非追加输入(异常输入):整体重置缓存
    }
    this.prevText = text
    const blocks = splitMdBlocks(text)
    const frozenCount = Math.max(0, blocks.length - UNSTABLE_TAIL_BLOCKS)
    const frozen: FrozenMdBlock[] = []
    for (const b of blocks.slice(0, frozenCount)) {
      let html = this.cache.get(b.start)
      if (html === undefined) {
        html = mdToHtml(text.slice(b.start, b.end))
        this.cache.set(b.start, html)
      }
      frozen.push({ key: b.start, html })
    }
    const tailStart = frozenCount > 0 ? blocks[frozenCount].start : 0
    const tailHtml = mdToHtml(text.slice(tailStart))
    this.cached = { frozen, tailHtml }
    return this.cached
  }
}
