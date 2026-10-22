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

/* ---------- JSON 主响应路径(生产端点实际输出,见 sftforge/parser.py) ---------- */

/** 轮次响应的展示段落(统一 JSON 主路径与 <think> 回退的渲染模型):
 * label 为事件中文名/verdict 等段首标签(无则 null),hit 为需高亮的段落。 */
export interface ResponseSegment {
  label: string | null
  body: string
  hit: boolean
}

/** 尽力从响应文本解析 JSON 对象:直解析 → markdown 代码块 → 首尾大括号截取。
 * 失败返回 null(不抛异常;对应后端 _try_extract_json 的容错口径)。 */
function tryParseJson(text: string): Record<string, unknown> | null {
  const raw = String(text || '').trim()
  if (!raw) return null
  const candidates = [raw]
  const fence = raw.match(/```(?:json)?\s*([\s\S]*?)```/)
  if (fence) candidates.push(fence[1].trim())
  const first = raw.indexOf('{')
  const last = raw.lastIndexOf('}')
  if (first >= 0 && last > first) candidates.push(raw.slice(first, last + 1))
  for (const c of candidates) {
    try {
      const data = JSON.parse(c) as unknown
      if (data && typeof data === 'object' && !Array.isArray(data)) {
        return data as Record<string, unknown>
      }
    } catch {
      // 尝试下一个候选
    }
  }
  return null
}

/** 轮次响应 → 展示段落序列(主路径优先,逐层回退):
 * 1. forward JSON({event_thoughts:[{event_id, present, thinking}]}):
 *    逐事件一段,label 为事件中文名;present=true 或本轮纠错目标事件的段落 hit;
 * 2. 纠错 JSON({thinking, verdict?}):单段,verdict 作 label,整段 hit
 *    (本轮即针对目标事件的修复/复核输出,按 kind 配色整体高亮);
 * 3. 回退 <think> 文本切分(thinkParagraphs 同口径);
 * 4. 均不可用返回空数组(调用方展示原文)。 */
export function responseSegments(
  text: string,
  targetNames: Set<string>,
  eventNames: Record<number, string>,
): ResponseSegment[] {
  const data = tryParseJson(text)
  if (data) {
    if (Array.isArray(data.event_thoughts)) {
      const out: ResponseSegment[] = []
      for (const item of data.event_thoughts) {
        if (!item || typeof item !== 'object') continue
        const t = item as Record<string, unknown>
        const body = String(t.thinking || '').trim()
        if (!body) continue
        const eid = typeof t.event_id === 'number' ? t.event_id : null
        const label = eid != null ? eventNames[eid] || `事件${eid}` : null
        out.push({ label, body, hit: t.present === true || (label != null && targetNames.has(label)) })
      }
      if (out.length) return out
    }
    const thinking = String(data.thinking || '').trim()
    if (thinking) {
      const verdict = typeof data.verdict === 'string' ? data.verdict : null
      return [{ label: verdict, body: thinking, hit: true }]
    }
  }
  return thinkParagraphs(text, targetNames).map((p) => ({
    label: p.prefix,
    body: p.body,
    hit: p.hit,
  }))
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
