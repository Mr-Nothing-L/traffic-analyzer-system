/* ================================================================
   Mock 数据层(?mock=1)—— 聚合层:对外出口保持 mock.js 不变
   实现拆分为:mock_db.js(数据/状态)→ mock_tick.js(泳道推进)→ mock_api.js(路由/占位图)
   ================================================================ */
export { mockTick } from './mock_tick.js';
export { mockApi, mockFrameUrl, mockImageUrl } from './mock_api.js';
