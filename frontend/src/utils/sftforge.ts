/** SFT Forge 面板纯逻辑(零 DOM,可单测):
 * 轮次 kind 元信息、think 段落切分与纠错目标段落高亮判定、JSONL 导出文本。
 * 段落切分口径与 sft/model.parseSftDescription 一致(空行分段 + 「事件名:」前缀)。 */

/** 轮次类型徽标:label 展示文案,cls 配色类(视图映射到 token 化样式)。 */
export const FORGE_KIND_META: Record<string, { label: string; cls: string }> = {
  forward: { label: '正向盲推', cls: 'forward' },
  correct_missing: { label: '漏检纠错', cls: 'missing' },
  review_extra: { label: '误报复核', cls: 'extra' },
}

export function forgeKindLabel(kind: string): string {
  return FORGE_KIND_META[kind]?.label ?? kind
}

export function forgeKindCls(kind: string): string {
  return FORGE_KIND_META[kind]?.cls ?? 'other'
}

/** think 段落:prefix 为「事件名:」前缀(无则 null),body 为整段原文。 */
export interface ThinkParagraph {
  prefix: string | null
  body: string
  /** 本段是否为该轮纠错目标事件的段落(需高亮的「修复段落」)。 */
  hit: boolean
}

/** 切分响应文本的 <think> 段落,并标记属于目标事件名集合的段落。
 * 无 <think> 段时返回空数组(调用方回退展示原文)。 */
export function thinkParagraphs(text: string, targetNames: Set<string>): ThinkParagraph[] {
  const m = String(text || '').match(/<think>([\s\S]*?)<\/think>/)
  if (!m) return []
  const out: ThinkParagraph[] = []
  m[1]
    .trim()
    .split(/\n\s*\n/)
    .forEach((para) => {
      const body = para.trim()
      if (!body) return
      const pm = body.match(/^([^：\n]{1,30})：/)
      const prefix = pm ? pm[1] : null
      out.push({ prefix, body, hit: prefix != null && targetNames.has(prefix) })
    })
  return out
}

/** 视频绝对路径 → 展示用文件名(末段)。 */
export function videoBaseName(path: string): string {
  const parts = String(path || '').split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : path
}

/** 训练行数组 → JSONL 文本(每行一个 JSON 对象,与原 lora 训练文件格式一致)。 */
export function buildJsonlText(rows: Record<string, unknown>[]): string {
  return rows.map((r) => JSON.stringify(r)).join('\n') + (rows.length ? '\n' : '')
}
