/* ================================================================
   内联 SVG 图标(统一替代 emoji/符号字符)
   风格与 tree.js 原 CHECK_SVG 一致:fill none / stroke currentColor /
   stroke-linecap,linejoin round;默认 16px、viewBox 24、stroke-width 1.8。
   用法:import { icon } from './icons.js';  html += icon('folder');
   ================================================================ */

const PATHS = {
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file: '<path d="M6 3h8l4 4v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v4h4"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.5v5l4.5-2.5z"/>',
  up: '<path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  close: '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
  retry: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1"/>',
  home: '<path d="M4 11l8-7 8 7"/><path d="M6 9.5V20h12V9.5"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/>'
    + '<rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  logo: '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/><path d="M3.5 9.5h17"/><path d="M9.5 9.5V20.5"/>'
    + '<path d="M15 3.5v6"/><path d="M9.5 15H20.5"/>',
};

// size 默认 16px;小字号场景(徽章/树行/行内键)按需传 11~13
export function icon(name, size) {
  const s = size || 16;
  return '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round"'
    + ' stroke-linejoin="round" aria-hidden="true">' + PATHS[name] + '</svg>';
}

// 已完成徽标的 ✓:SVG 路径配合 stroke-dashoffset 动画一次性描边绘制(原 tree.js CHECK_SVG)
export const CHECK_SVG = '<svg class="badge-check" viewBox="0 0 12 12" aria-hidden="true">'
  + '<path d="M2.5 6.4 5 8.9 9.5 3.6"/></svg>';
