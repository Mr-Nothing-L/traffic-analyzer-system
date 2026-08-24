/** 快速对话:来源(上传视频/图片、工作区视频)+ 消息历史 + SSE 流式提问。
 * 后端契约:GET /api/chat/state、POST /api/chat/upload(FormData 字段 files)、
 * POST /api/chat/source({rel})、POST /api/chat/ask({question},SSE 流)、
 * DELETE /api/chat/history(204)。 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiError, apiFetch } from '../api/client'

export interface ChatSource {
  kind: 'upload_video' | 'upload_images' | 'workspace_video'
  display_name: string
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'divider'
  content: string
  think: string
  images: string[]
  created_at: number
}

interface ChatState {
  source: ChatSource | null
  messages: ChatMessage[]
  has_summary: boolean
}

/** 非 2xx 响应统一成 ApiError(detail 取 body.detail,同 client.ts 语义)。 */
async function toApiError(res: Response): Promise<ApiError> {
  if (res.status === 401 && window.location.pathname !== '/login') {
    window.location.href = '/login'
  }
  let detail = res.statusText
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (body?.detail != null) detail = String(body.detail)
  } catch {
    // 非 JSON 错误响应,保留 statusText
  }
  return new ApiError(res.status, detail)
}

export const useChatStore = defineStore('chat', () => {
  const source = ref<ChatSource | null>(null)
  const messages = ref<ChatMessage[]>([])
  /** 流式中的 assistant 气泡(未落进 messages);think/content/images 实时累加。 */
  const current = ref<ChatMessage | null>(null)
  const sending = ref(false)

  let ctrl: AbortController | null = null

  function applyState(s: ChatState) {
    source.value = s.source
    messages.value = s.messages || []
    current.value = null
  }

  /** 进入页面回填:消息历史 + 当前来源。 */
  async function fetchState() {
    applyState(await apiFetch<ChatState>('/chat/state'))
  }

  /** 上传视频/图片:FormData 走原生 fetch(apiFetch 会强制 JSON header,不能带 boundary)。 */
  async function upload(files: FileList) {
    const fd = new FormData()
    for (const f of Array.from(files)) fd.append('files', f)
    let res: Response
    try {
      res = await fetch('/api/chat/upload', { method: 'POST', body: fd })
    } catch {
      throw new ApiError(0, '网络错误,无法连接后端')
    }
    if (!res.ok) throw await toApiError(res)
    applyState((await res.json()) as ChatState)
  }

  /** 从工作区选择视频作为来源。 */
  async function setSource(rel: string) {
    applyState(
      await apiFetch<ChatState>('/chat/source', {
        method: 'POST',
        body: JSON.stringify({ rel }),
      }),
    )
  }

  /** 流式中的部分气泡落进消息列表:完成/出错/停止都保留已收到文本。幂等。 */
  function finalizeCurrent() {
    const c = current.value
    if (c && (c.content || c.think || c.images.length)) messages.value.push(c)
    current.value = null
  }

  /** 停止当前流(组件卸载/再次发送前也调用)。 */
  function stop() {
    ctrl?.abort()
  }

  /** 提问:POST /api/chat/ask 的 SSE 流按行解析;think/delta/images 增量进 current。
   * error 事件:保留已收到文本并抛错(调用方提示);stop() 中断:静默收尾。 */
  async function ask(question: string) {
    if (sending.value) stop()
    const now = Date.now() / 1000
    messages.value.push({ role: 'user', content: question, think: '', images: [], created_at: now })
    current.value = { role: 'assistant', content: '', think: '', images: [], created_at: now }
    sending.value = true
    ctrl = new AbortController()
    try {
      let res: Response
      try {
        res = await fetch('/api/chat/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question }),
          signal: ctrl.signal,
        })
      } catch (e) {
        if ((e as Error).name === 'AbortError') return
        throw new ApiError(0, '网络错误,无法连接后端')
      }
      if (!res.ok || !res.body) throw await toApiError(res)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let errMsg: string | null = null
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE 事件以空行(\n\n)分隔;每行 data: {json}
        let sep: number
        while ((sep = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, sep)
          buf = buf.slice(sep + 2)
          for (const line of raw.split('\n')) {
            if (!line.startsWith('data:')) continue
            let ev: { type?: string; text?: string; urls?: string[]; message?: string }
            try {
              ev = JSON.parse(line.slice(5).trim())
            } catch {
              continue // 非 JSON 行(注释/心跳)忽略
            }
            const c = current.value
            if (!c) break
            if (ev.type === 'think') c.think += ev.text || ''
            else if (ev.type === 'delta') c.content += ev.text || ''
            else if (ev.type === 'images') c.images.push(...(ev.urls || []))
            // done 携带剥离 ```json 画框块后的干净正文,替换原始流文本
            else if (ev.type === 'done' && ev.text != null) c.content = ev.text
            else if (ev.type === 'error') errMsg = ev.message || '对话出错'
          }
        }
      }
      if (errMsg) throw new Error(errMsg)
    } catch (e) {
      if ((e as Error).name === 'AbortError') return // stop() 中断:静默收尾,已收到文本在 finally 落库
      throw e
    } finally {
      finalizeCurrent()
      sending.value = false
      ctrl = null
    }
  }

  /** 清空对话记忆(保留当前来源)。 */
  async function clear() {
    await apiFetch('/chat/history', { method: 'DELETE' })
    messages.value = []
    current.value = null
  }

  return { source, messages, current, sending, fetchState, upload, setSource, ask, stop, clear }
})
