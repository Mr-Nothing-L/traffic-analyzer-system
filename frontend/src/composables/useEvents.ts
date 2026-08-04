import { onUnmounted } from 'vue'
import { useAppStore } from '../stores/app'

/** /api/events SSE 封装。
 * 后端用命名事件(`event: <type>` + JSON data,见 web/realtime.py);
 * 模块级共享一条 EventSource,subscribe(type, handler) 返回退订函数,
 * 组件内由 onUnmounted 自动退订;最后一个订阅退订后关闭连接。
 */

type Handler = (data: unknown) => void

let source: EventSource | null = null
let refCount = 0
const handlers = new Map<string, Set<Handler>>()
const attachedTypes = new Set<string>() // 已挂到当前 source 的事件类型(防重复挂)

function attach(type: string) {
  if (!source || attachedTypes.has(type)) return
  attachedTypes.add(type)
  source.addEventListener(type, (ev) => {
    let data: unknown = (ev as MessageEvent).data
    try {
      data = JSON.parse((ev as MessageEvent).data)
    } catch {
      // 非 JSON 负载按原样透传
    }
    for (const handler of handlers.get(type) || []) handler(data)
  })
}

function ensureSource(): EventSource {
  if (source) return source
  source = new EventSource('/api/events')
  attachedTypes.clear()
  const store = useAppStore()
  source.onopen = () => store.setSseConnected(true)
  source.onerror = () => store.setSseConnected(false) // EventSource 自动重连
  for (const type of handlers.keys()) attach(type)
  return source
}

export function useEvents() {
  refCount += 1

  function subscribe(type: string, handler: Handler): () => void {
    let set = handlers.get(type)
    if (!set) {
      set = new Set()
      handlers.set(type, set)
    }
    set.add(handler)
    ensureSource()
    attach(type) // 连接已存在时,新事件类型也要挂监听器(否则收不到)
    return () => {
      set.delete(handler)
      if (set.size === 0) handlers.delete(type)
    }
  }

  onUnmounted(() => {
    refCount -= 1
    if (refCount <= 0 && source) {
      source.close()
      source = null
      attachedTypes.clear()
      refCount = 0
      useAppStore().setSseConnected(false)
    }
  })

  return { subscribe }
}
