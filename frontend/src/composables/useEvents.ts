import { getCurrentScope, onScopeDispose } from 'vue'
import { useAppStore } from '../stores/app'

/** /api/events SSE 封装。
 * 后端用命名事件(`event: <type>` + JSON data,见 web/realtime.py);
 * 模块级共享一条 EventSource。须在组件 setup(或 effectScope)内调用:
 * subscribe 的 handler 随当前作用域销毁(组件卸载)自动退订,
 * 也可用返回的退订函数手动提前退订;最后一个使用方销毁后关闭连接。
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
    const unsubscribe = () => {
      set.delete(handler)
      if (set.size === 0) handlers.delete(type)
    }
    // 组件 setup/effectScope 内调用时随作用域销毁自动退订(防闭包泄漏);
    // 作用域外调用则只能依赖返回的退订函数手动退订
    if (getCurrentScope()) onScopeDispose(unsubscribe)
    return unsubscribe
  }

  // 作用域销毁(组件卸载)时归还引用;归零才关闭共享连接
  if (getCurrentScope()) {
    onScopeDispose(() => {
      refCount -= 1
      if (refCount <= 0 && source) {
        source.close()
        source = null
        attachedTypes.clear()
        refCount = 0
        useAppStore().setSseConnected(false)
      }
    })
  }

  return { subscribe }
}
