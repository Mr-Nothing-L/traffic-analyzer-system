/** agent 会话的 API 往返与条目映射(自 stores/agentchat.ts 拆出,纯函数):
 * toApiError/postJson/reqJson(fetch 封装)、formatToolOutput/resolveMediaUrl/
 * extractToolImages/hasToolVideo(工具结果的展示投影)、nextEntryId(前端条目
 * id 发号器)、mapHistoryEntry(history/events 条目 → 本地时间线条目)。 */
import { ApiError } from '../api/client'
import type { AgentAccess, AgentApprovalEntry, AgentEntry, AgentPreviewContent } from './types'

export async function toApiError(res: Response): Promise<ApiError> {
  if (res.status === 401 && window.location.pathname !== '/login') {
    window.location.href = '/login'
  }
  let detail = res.statusText
  try {
    const body = (await res.json()) as {
      detail?: unknown
      error?: { message?: unknown }
    }
    if (body?.detail != null) detail = String(body.detail)
    else if (body?.error?.message != null) detail = String(body.error.message)
  } catch {
    // 非 JSON 错误响应,保留 statusText
  }
  return new ApiError(res.status, detail)
}

/** JSON POST 封装(错误体两种形态兼容;401 跳登录由 toApiError 处理)。 */
export async function postJson(path: string, body: unknown): Promise<unknown> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, '网络错误,无法连接后端')
  }
  if (!res.ok) throw await toApiError(res)
  return res.json()
}

/** GET/DELETE 封装(与 postJson 同一套错误语义)。 */
export async function reqJson(path: string, method: 'GET' | 'DELETE'): Promise<unknown> {
  let res: Response
  try {
    res = await fetch(path, { method })
  } catch {
    throw new ApiError(0, '网络错误,无法连接后端')
  }
  if (!res.ok) throw await toApiError(res)
  return res.json()
}

/** tool_result 的 output:string 原样;ContentPart[] 取 text 拼接;其他 JSON 化。 */
export function formatToolOutput(output: unknown): string {
  if (typeof output === 'string') return output
  if (Array.isArray(output)) {
    const texts = output
      .map((p) =>
        p && typeof p === 'object' && 'text' in p ? String((p as { text: unknown }).text) : '',
      )
      .filter(Boolean)
    if (texts.length) return texts.join('\n')
  }
  try {
    return JSON.stringify(output, null, 2) ?? ''
  } catch {
    return String(output ?? '')
  }
}

/** 服务端媒体引用(/sessions/{id}/media/{hash})→ 经 /api/agent 代理的展示
 * 地址;dataURL(旧条目)与其他形态原样返回。服务端把工具输出/检测载荷里的
 * 图片 dataURL 落盘为内容寻址文件后,条目里只携带引用 URL(见 agent
 * mediaStore.ts),前端在渲染层统一解析。 */
export function resolveMediaUrl(url: string): string {
  return url.startsWith('/') ? `/api/agent${url}` : url
}

/** tool_result 的 output 为 ContentPart[] 时,提取 image_url 部分的 url 并解析
 * 为可展示地址(dataURL 原样,media 引用加 /api/agent 代理前缀)。
 * kosong 的 ImageURLPart 使用 camelCase 的 imageUrl 键名。 */
export function extractToolImages(output: unknown): string[] {
  if (!Array.isArray(output)) return []
  const urls: string[] = []
  for (const p of output) {
    if (!p || typeof p !== 'object') continue
    const part = p as {
      type?: unknown
      imageUrl?: { url?: unknown }
    }
    if (part.type !== 'image_url') continue
    const url = part.imageUrl?.url
    if (typeof url === 'string' && url) urls.push(resolveMediaUrl(url))
  }
  return urls
}

/** tool_result 的 output 是否含 video part(load_video 整段视频直传)。
 * 只作标记供 UI 显示静态提示;视频 dataURL 体积巨大,绝不当图渲染。 */
export function hasToolVideo(output: unknown): boolean {
  if (!Array.isArray(output)) return false
  return output.some(
    (p) => p !== null && typeof p === 'object' && (p as { type?: unknown }).type === 'video_url',
  )
}

/** detection 载荷的逐事件 annotated_image 解析为可展示地址(服务端新条目是
 * /sessions/{id}/media/{hash} 引用,旧条目是 dataURL,均原样/加代理前缀);
 * 只在进入前端时间线时转换一次,其余字段不动。非对象形态(legacy 字符串)原样返回。 */
export function mapDetectionData(data: unknown): unknown {
  if (!data || typeof data !== 'object' || !Array.isArray((data as { events?: unknown }).events)) {
    return data
  }
  const d = data as { events?: Array<Record<string, unknown>> }
  return {
    ...d,
    events: d.events?.map((ev) => {
      const url = ev?.['annotated_image']
      return typeof url === 'string' ? { ...ev, annotated_image: resolveMediaUrl(url) } : ev
    }),
  }
}

/** 前端条目 id 发号器(模块级递增,跨会话全局唯一):折叠态/复制态在会话切换
 * 的窗口期仍持有旧条目 id,id 全局唯一才不会串到新会话的同名条目上。 */
let entryIdSeq = 0
export function nextEntryId(): string {
  entryIdSeq += 1
  return `e${entryIdSeq}`
}

/** GET history/events 的条目 → 本地时间线条目:进入时间线即赋前端条目 id;
 * seq 给定时记录落盘序号(history 按数组位置、events 按事件携带的 seq)。
 * 不认识/缺 kind 的条目丢弃。 */
export function mapHistoryEntry(raw: unknown, seq?: number): AgentEntry | null {
  if (!raw || typeof raw !== 'object') return null
  const e = raw as Record<string, unknown>
  if (e.kind === 'user') {
    return {
      id: nextEntryId(),
      kind: 'user',
      text: String(e.text ?? ''),
      ...(seq != null ? { seq } : {}),
      ...(Array.isArray(e.images) && e.images.length
        ? { images: e.images.map(String) }
        : {}),
      ...(e.videoPath != null ? { videoPath: String(e.videoPath) } : {}),
      ...(e.videoSrc != null ? { videoSrc: String(e.videoSrc) } : {}),
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  if (e.kind === 'assistant') {
    return {
      id: nextEntryId(),
      kind: 'assistant',
      text: String(e.text ?? ''),
      think: String(e.think ?? ''),
      ...(typeof e.generateMs === 'number' ? { generateMs: e.generateMs } : {}),
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  if (e.kind === 'tool') {
    return {
      id: nextEntryId(),
      kind: 'tool',
      callId: String(e.toolCallId ?? ''),
      name: String(e.name ?? ''),
      args:
        typeof e.arguments === 'string' ? e.arguments : JSON.stringify(e.arguments ?? ''),
      result: formatToolOutput(e.output),
      images: extractToolImages(e.output),
      hasVideo: hasToolVideo(e.output),
      children: [],
      isError: e.isError === true,
      done: true,
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  if (e.kind === 'approval') {
    const decision = e.decision != null ? String(e.decision) : undefined
    const pending = e.pending === true
    const accesses = Array.isArray(e.accesses)
      ? (e.accesses as AgentAccess[])
      : ([] as AgentAccess[])
    const preview =
      e.preview && typeof e.preview === 'object'
        ? ({
            language: String((e.preview as Record<string, unknown>).language ?? 'text'),
            content: String((e.preview as Record<string, unknown>).content ?? ''),
            truncated: (e.preview as Record<string, unknown>).truncated === true,
          } as AgentPreviewContent)
        : undefined
    return {
      id: nextEntryId(),
      kind: 'approval',
      requestId: String(e.requestId ?? ''),
      toolName: String(e.toolName ?? ''),
      approvalRule: String(e.approvalRule ?? ''),
      ...(e.description != null ? { description: String(e.description) } : {}),
      accesses,
      ...(preview ? { preview } : {}),
      // 未决审批仍存在于服务端 pending 集合时渲染为可操作面板,否则历史里无 decision 的挂起审批标记失效
      ...(decision ? { decision } : pending ? {} : { stale: true }),
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  if (e.kind === 'detection') {
    return {
      id: nextEntryId(),
      kind: 'detection',
      data: mapDetectionData(e.data),
      ...(typeof e.at === 'number' ? { at: e.at } : {}),
    }
  }
  return null
}
