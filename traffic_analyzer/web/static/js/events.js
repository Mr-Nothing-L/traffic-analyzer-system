/* ================================================================
   SSE 事件总线:全应用共享一条 EventSource('/api/events') 连接
   后端契约:GET /api/events → text/event-stream,格式
     event: <type>\ndata: <json>\n\n(15s 心跳 ": ping")
   事件类型:job.progress / job.done / dashboard.changed / presence
   认证与其他 /api/* 一致(cookie);断线由浏览器自动重连,不做自定义退避。
   mock 模式(?mock=1):EventSource 无法被 mock 层拦截,一律不建连,
   subscribe 退化为无操作(返回空退订函数),原有 mock 行为不受影响。
   ================================================================ */
import { MOCK } from './state.js';

let es = null;       // 全局唯一 EventSource,连接生命周期挂全局
const handlers = {}; // type -> Set<handler>,首个订阅时挂原生监听

function ensureSource() {
  if (es) return;
  es = new EventSource('/api/events');
  // 仅留日志,不打扰 UI;重连交由浏览器(默认约 3s)
  es.onerror = () => console.warn('[events] SSE 连接异常,等待浏览器自动重连');
}

// 订阅某类事件,handler 收到已解析的 data;返回退订函数(视图级退订登记到 state.cleanups)
export function subscribe(type, handler) {
  if (MOCK) return () => {}; // mock 模式:不建连,退订为无操作
  ensureSource();
  if (!handlers[type]) {
    handlers[type] = new Set();
    es.addEventListener(type, e => {
      let data;
      try { data = JSON.parse(e.data); } catch (err) { return; } // 非 JSON 帧忽略
      handlers[type].forEach(fn => {
        try { fn(data); } catch (err) { console.error('[events] 处理器异常(' + type + '):', err); }
      });
    });
  }
  handlers[type].add(handler);
  return () => { handlers[type].delete(handler); };
}
