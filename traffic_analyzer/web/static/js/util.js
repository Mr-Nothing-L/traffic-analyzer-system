/* ---------------------------------------------------------------- 工具 */
export const $ = (sel, root) => (root || document).querySelector(sel);
export const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

export function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function fmtBytes(n) {
  if (n == null || isNaN(n)) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
}

export function toast(msg, kind) {
  const root = $('#toast-root');
  // 新 toast 从底部滑入;已有 toast 标记为旧,压缩淡化
  $$('.toast', root).forEach(t => t.classList.add('toast-old'));
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4200);
}

// 报告正文与证据画廊的图片:加载完成后加 .loaded,触发模糊→清晰过渡。
// 捕获阶段监听(load/error 不冒泡),同时覆盖 innerHTML 与 createElement 两种插入方式;
// 加载失败也加 .loaded,避免一直停留在模糊态。
export function markImgLoaded(e) {
  const t = e.target;
  if (t && t.tagName === 'IMG' && (t.closest('.md') || t.closest('.ev-gallery'))) {
    t.classList.add('loaded');
  }
}
document.addEventListener('load', markImgLoaded, true);
document.addEventListener('error', markImgLoaded, true);

// 保存成功反馈:按钮短暂变绿并前置 ✓,约 1s 后还原(期间若 DOM 已重建则无需还原)
export function flashSaveBtn(btn) {
  if (!btn) return;
  const orig = btn.textContent;
  btn.classList.add('btn-saved');
  btn.textContent = '已保存';
  setTimeout(() => {
    if (!btn.isConnected) return;
    btn.classList.remove('btn-saved');
    btn.textContent = orig;
  }, 1000);
}

export class ApiError extends Error {
  constructor(status, detail) { super(detail || ('HTTP ' + status)); this.status = status; }
}
