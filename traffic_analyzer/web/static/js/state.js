/* ------------------------------------------------------------ 全局状态 */
export const MOCK = new URLSearchParams(location.search).get('mock') === '1';

export const state = {
  workspace: null,          // {path} | {path:null}
  videos: [],               // [{name, stem, rel, size, mtime, has_results}](全工作区递归)
  jobs: [],                 // [{id, kind, stem?, rel?, status, progress, log_tail, returncode?}]
  prevJobStatus: {},        // id -> status(用于完成转移检测)
  checked: new Set(),       // 勾选的视频 rel 路径
  currentStem: null,        // 当前视频 stem(结果按 stem 读取,契约不变)
  currentRel: null,         // 当前视频 rel(媒体按 rel 定位;顶层时 rel == name)
  results: null,            // 当前视频的 {report_md, sft_label, evidence}
  evidenceDraft: null,      // 编辑中的 evidence 深拷贝
  evidenceDirty: false,
  evTabIdx: 0,
  eventConfig: null,        // /api/config/events 缓存([{event_id, name_zh, is_active}])
  sftDraft: null,           // SFT 编辑草稿 {texts, checks, attrs, skeletons, unmatched, env}
  sftSavedSig: '',          // 已保存草稿的签名(用于 dirty 判断)
  cleanups: [],             // 主区重渲染前的清理函数
  tree: { loaded: false, root: [], children: {}, expanded: new Set() }, // 侧栏文件树
  filter: '',               // 侧栏视频过滤词(子串、忽略大小写;仅影响展示,不改勾选)
  sort: 'name',             // 侧栏排序键:name / mtime / size / status
};

// 主区重渲染前的清理:执行并清空 state.cleanups(帧循环/全局监听等)
// 定义在 state.js(叶子模块),供 preview/evidence 等使用,避免模块间循环依赖
export function runCleanups() {
  state.cleanups.forEach(fn => { try { fn(); } catch (e) { /* ignore */ } });
  state.cleanups = [];
}

// 调试句柄:仅 URL 带 ?debug=1 或 ?mock=1 时挂载 window.state,供浏览器手动调试
// (仓库内无任何代码消费 window.state;生产环境不挂载,避免全局句柄泄漏)
if (MOCK || new URLSearchParams(location.search).get('debug') === '1') {
  window.state = state;
}
