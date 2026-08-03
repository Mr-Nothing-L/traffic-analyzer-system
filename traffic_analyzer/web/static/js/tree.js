/* ================================================================
   侧栏
   ================================================================ */
import { $, $$, esc, fmtBytes, toast } from './util.js';
import { state } from './state.js';
import { api } from './api.js';
import { selectVideo } from './preview.js';
import { retryInfer, cancelJob } from './jobs.js';
import { pixelBarHtml, paintPixelBar } from './pixel_bar.js';
import { presenceBadgeHtml, presenceUsers } from './presence.js';
import { icon, CHECK_SVG } from './icons.js';

export function latestJobForStem(stem) {
  for (let i = state.jobs.length - 1; i >= 0; i--) {
    const j = state.jobs[i];
    if (j.kind === 'infer' && j.stem === stem) return j;
  }
  return null;
}

function videoStatus(v) {
  const job = latestJobForStem(v.stem);
  if (job && job.status === 'running') return { cls: 'st-running', text: '运行中' };
  if (job && job.status === 'queued') return { cls: 'st-queued', text: '排队中' };
  if (v.has_results) return { cls: 'st-done', text: '已完成' };
  if (job && job.status === 'failed') return { cls: 'st-failed', text: '失败' };
  return { cls: 'st-none', text: '未推理' };
}

let sidebarSnapshot = '';

// 使侧栏快照失效,强制下次 renderSidebar 重建 DOM(模块外只能通过此函数复位)
export function invalidateSidebar() {
  sidebarSnapshot = '';
}

/* ------------------------------------------------------------ 窄屏抽屉 */
// 414–767px(及更小)档位侧栏改为顶部抽屉(style.css 响应式专节),开关状态挂在
// body.drawer-open 类名上;桌面视口下该类名无任何样式效果,不影响桌面端选择器与布局
export function toggleSidebarDrawer() {
  document.body.classList.toggle('drawer-open');
}

// 选中视频后自动收起抽屉(桌面端调用无副作用:该类名本来就不会存在/无样式)
export function closeSidebarDrawer() {
  document.body.classList.remove('drawer-open');
}

/* ------------------------------------------------------------ 过滤/排序 */
export const SIDE_FILTER_KEY = 'ta_sidebar_filter';
export const SIDE_SORT_KEY = 'ta_sidebar_sort';
// 「状态」排序的优先级:失败最前,已完成最后
const STATUS_ORDER = { 'st-failed': 0, 'st-running': 1, 'st-queued': 2, 'st-none': 3, 'st-done': 4 };

function videoNameMatches(name) {
  return !state.filter || String(name || '').toLowerCase().includes(state.filter.toLowerCase());
}

// 过滤:仅保留匹配的视频行及其祖先目录;非视频文件在过滤时隐藏
function filterLevel(entries) {
  if (!state.filter) return entries;
  return entries.filter(e => {
    if (e.type === 'dir') {
      // 已加载子级时递归判断;未加载/折叠时用全量视频列表判断后代是否含匹配
      const kids = state.tree.children[e.rel];
      if (kids && state.tree.expanded.has(e.rel)) return filterLevel(kids).length > 0;
      return state.videos.some(v => v.rel.startsWith(e.rel + '/') && videoNameMatches(v.name));
    }
    return e.is_video && videoNameMatches(e.name);
  });
}

// 排序:目录恒在视频之前且保持原顺序;视频按所选键排序
function sortLevel(entries) {
  if (state.sort === 'name') return entries; // 后端返回即名称序,无需重排
  const dirs = entries.filter(e => e.type === 'dir');
  const vids = entries.filter(e => e.is_video);
  const files = entries.filter(e => e.type !== 'dir' && !e.is_video);
  const key = {
    mtime: e => -(e.mtime || 0),            // 最新在前
    size: e => -(e.size || 0),              // 最大在前
    status: e => {
      const v = state.videos.find(v => v.rel === e.rel) || e;
      return STATUS_ORDER[videoStatus(v).cls];
    },
  }[state.sort];
  vids.sort((a, b) => (key(a) - key(b)) || String(a.name).localeCompare(String(b.name)));
  return dirs.concat(vids, files);
}

// 当前层级实际要展示的条目(先过滤后排序)
function viewEntries(entries) {
  return sortLevel(filterLevel(entries));
}

// 8 格迷你像素条(格内 3 子条,与主区像素条同构;实现见公共模块 pixel_bar.js)
const MINI_CELLS = 8;

// 递归渲染一层树节点;depth 控制缩进(每级 14px)
function treeRowsHtml(entries, depth) {
  let html = '';
  viewEntries(entries).forEach(e => {
    const pad = 'style="padding-left:' + (8 + depth * 14) + 'px"';
    if (e.type === 'dir') {
      const open = state.tree.expanded.has(e.rel);
      html += '<div class="tree-row tree-dir" data-dir="' + esc(e.rel) + '" ' + pad + '>'
        + '<span class="tree-caret' + (open ? ' open' : '') + '">▸</span>'
        + '<span class="tree-ico">' + icon('folder', 12) + '</span>'
        + '<span class="tree-name" title="' + esc(e.rel) + '">' + esc(e.name) + '</span></div>';
      if (open) {
        const kids = state.tree.children[e.rel];
        const childPad = 'style="padding-left:' + (8 + (depth + 1) * 14) + 'px"';
        let kidsHtml;
        if (kids && kids.length) kidsHtml = treeRowsHtml(kids, depth + 1);
        else if (kids) kidsHtml = '<div class="tree-empty" ' + childPad + '>空目录</div>';
        else kidsHtml = '<div class="tree-empty" ' + childPad + '>加载中…</div>';
        // 子级包一层容器,供展开/收起的高度动画裁剪使用
        html += '<div class="tree-kids">' + kidsHtml + '</div>';
      }
    } else if (e.is_video) {
      // 视频(任意深度):勾选键为 rel,徽标/点击与顶层一致,均进入分析视图
      const rel = e.rel;
      const v = state.videos.find(v => v.rel === rel) || {
        stem: e.stem || e.name.replace(/\.[^.]+$/, ''), rel: rel, has_results: !!e.has_results,
      };
      const st = videoStatus(v);
      // 运行中:视频名右侧渲染迷你像素进度条(fraction 点亮格数;fraction 为 null 时不定态波浪)
      //   + 行内停止键;排队/完成/失败保持徽标
      // 已完成:徽标内嵌 ✓ SVG(描边动画);失败:徽标旁附重试按钮,点击仅对该视频重新提交推理
      const job = latestJobForStem(v.stem);
      const frac = job && job.progress ? job.progress.fraction : null;
      const statusHtml = st.cls === 'st-running'
        ? '<span class="mini-prog pixel-bar' + (frac == null ? ' indet' : '')
          + '" data-prog-stem="' + esc(v.stem) + '" title="推理中">'
          + pixelBarHtml(MINI_CELLS) + '</span>'
          + '<button class="stop-btn" data-stop="' + esc(job.id) + '" title="停止推理">' + icon('stop', 11) + '</button>'
        : (st.cls === 'st-done'
            ? '<span class="badge st-done">' + CHECK_SVG + esc(st.text) + '</span>'
            : '<span class="badge ' + st.cls + '">' + st.text + '</span>')
          + (st.cls === 'st-failed'
            ? '<button class="retry-btn" data-retry="' + esc(rel) + '" title="重新推理">' + icon('retry', 11) + '</button>'
            : '');
      html += '<div class="video-item' + (state.currentRel === rel ? ' active' : '')
        + '" data-rel="' + esc(rel) + '" ' + pad + '>'
        + '<input type="checkbox" data-check="' + esc(rel) + '"' + (state.checked.has(rel) ? ' checked' : '') + '>'
        + '<span class="tree-ico">' + icon('video', 12) + '</span>'
        + '<div class="video-meta"><div class="video-name" title="' + esc(rel) + '">' + esc(e.name) + '</div>'
        + '<div class="video-sub">' + fmtBytes(e.size) + '</div></div>'
        + presenceBadgeHtml(rel)
        + statusHtml
        + '</div>';
    } else {
      // 非视频文件:仅展示,不可勾选/选中
      html += '<div class="tree-row tree-file" ' + pad + ' title="' + esc(e.rel) + '">'
        + '<span class="tree-caret"></span>'
        + '<span class="tree-ico">' + icon('file', 12) + '</span>'
        + '<span class="tree-name">' + esc(e.name) + '</span></div>';
    }
  });
  return html;
}

// 仅进度数值变化时快照不变:原地刷新迷你像素条点亮态/不定态,避免整树重建打断交互
function updateMiniProgress() {
  $$('#video-list .mini-prog[data-prog-stem]').forEach(el => {
    const job = latestJobForStem(el.dataset.progStem);
    const frac = job && job.progress ? job.progress.fraction : null;
    if (frac == null) {
      el.classList.add('indet');
    } else {
      el.classList.remove('indet');
      paintPixelBar(el.children, frac); // 不传 opts:无 frontier 脉冲
    }
  });
}

export function renderSidebar() {
  const list = $('#video-list');
  if (!state.workspace || !state.workspace.path) {
    sidebarSnapshot = '';
    list.innerHTML = '<div class="side-empty">设置工作区后列出文件</div>';
    return;
  }
  if (!state.tree.loaded) {
    sidebarSnapshot = '';
    // 刷新后不再自动 loadTree:引导用户去主区点「加载工作区」
    list.innerHTML = '<div class="side-empty">尚未加载:请点击主区「加载工作区」</div>';
    return;
  }
  // 快照对比,避免每次轮询重建 DOM(防止打断勾选/展开);过滤/排序也计入快照
  // latestJobForStem 的 job id 一并纳入:任务更替(重试/重提交)时 id 变化触发重建,
  // 行内停止键不会持过期 id
  const snap = JSON.stringify([
    state.tree.root, state.tree.children, Array.from(state.tree.expanded),
    state.filter, state.sort, presenceUsers(),
    state.videos.map(v => [v.rel, v.has_results, videoStatus(v).text,
      state.checked.has(v.rel), state.currentRel === v.rel,
      (latestJobForStem(v.stem) || {}).id || 0]),
  ]);
  if (snap === sidebarSnapshot) {
    updateMiniProgress(); // 快照不含 fraction:原地更新迷你进度条,避免重建整棵侧栏
    return;
  }
  sidebarSnapshot = snap;

  if (!state.tree.root.length) {
    list.innerHTML = '<div class="side-empty">工作区目录为空</div>';
    $('#check-all').checked = false;
    return;
  }
  // 过滤后整个工作区无匹配视频:显示空提示(勾选状态不受影响)
  if (state.filter && !state.videos.some(v => videoNameMatches(v.name))) {
    list.innerHTML = '<div class="side-empty">无匹配视频</div>';
    return;
  }
  list.innerHTML = treeRowsHtml(state.tree.root, 0);

  $$('#video-list .tree-dir').forEach(row => {
    row.addEventListener('click', () => toggleDir(row.dataset.dir));
  });
  $$('#video-list input[data-check]').forEach(cb => {
    cb.addEventListener('click', e => e.stopPropagation());
    cb.addEventListener('change', () => {
      if (cb.checked) state.checked.add(cb.dataset.check);
      else state.checked.delete(cb.dataset.check);
      syncButtons();
    });
  });
  $$('#video-list .retry-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation(); // 不触发所在行的视频选中
      retryInfer(btn.dataset.retry);
    });
  });
  $$('#video-list .stop-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation(); // 不触发所在行的视频选中
      cancelJob(btn.dataset.stop);
    });
  });
  $$('#video-list .video-item').forEach(item => {
    item.addEventListener('click', () => {
      selectVideo(item.dataset.rel);
      closeSidebarDrawer(); // 窄屏抽屉:选中视频后自动收起(桌面端无副作用)
    });
  });
  updateMiniProgress(); // 重建后像素条初始为全暗,立即按当前 fraction 点亮
  $('#check-all').checked = state.videos.length > 0 && state.videos.every(v => state.checked.has(v.rel));
}

// 展开/收起目录;首次展开时懒加载子级并缓存,再次展开直接用缓存
/* ---- 展开/收起动画(reduced-motion 下跳过,直接切换) ---- */
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');

// 目录行紧邻的 .tree-kids 容器 / 行内箭头
function treeRowParts(rel) {
  const row = document.querySelector('#video-list .tree-dir[data-dir="' + CSS.escape(rel) + '"]');
  if (!row) return {};
  const next = row.nextElementSibling;
  return {
    caret: row.querySelector('.tree-caret'),
    kids: next && next.classList.contains('tree-kids') ? next : null,
  };
}

// 展开:容器高度 0→实际高度 + 淡入,箭头 ▸ 旋转 90°
function animTreeExpand(rel) {
  if (REDUCED_MOTION.matches) return;
  const p = treeRowParts(rel);
  if (p.caret) p.caret.animate(
    [{ transform: 'rotate(0deg)' }, { transform: 'rotate(90deg)' }],
    { duration: 150, easing: 'ease' });
  if (p.kids) p.kids.animate(
    [{ maxHeight: '0px', opacity: 0 }, { maxHeight: p.kids.scrollHeight + 'px', opacity: 1 }],
    { duration: 180, easing: 'ease' });
}

// 收起:容器高度→0 + 淡出,箭头旋回;动画结束后再由调用方重渲染
async function animTreeCollapse(rel) {
  if (REDUCED_MOTION.matches) return;
  const p = treeRowParts(rel);
  if (p.caret) p.caret.animate(
    [{ transform: 'rotate(90deg)' }, { transform: 'rotate(0deg)' }],
    { duration: 150, easing: 'ease' });
  if (!p.kids) return;
  const anim = p.kids.animate(
    [{ maxHeight: p.kids.scrollHeight + 'px', opacity: 1 }, { maxHeight: '0px', opacity: 0 }],
    { duration: 180, easing: 'ease', fill: 'forwards' });
  try { await anim.finished; } catch (e) { /* 动画被中断则直接继续 */ }
}

async function toggleDir(rel) {
  if (state.tree.expanded.has(rel)) {
    await animTreeCollapse(rel); // 先播收起动画再重渲染
    state.tree.expanded.delete(rel);
    sidebarSnapshot = ''; renderSidebar();
    return;
  }
  state.tree.expanded.add(rel);
  sidebarSnapshot = ''; renderSidebar(); // 先展示「加载中…」占位
  if (!state.tree.children[rel]) {
    try {
      const data = await api('/api/workspace/tree?path=' + encodeURIComponent(rel));
      state.tree.children[rel] = data.entries || [];
    } catch (e) {
      state.tree.expanded.delete(rel);
      toast('读取目录失败(' + e.status + '):' + e.message, 'err');
    }
    sidebarSnapshot = ''; renderSidebar();
  }
  animTreeExpand(rel); // 渲染完成后播展开动画(未展开成功时无对应元素,自动跳过)
}

export function syncButtons() {
  const hasWs = !!(state.workspace && state.workspace.path);
  $('#btn-infer').disabled = !hasWs || state.checked.size === 0
    || state.jobs.some(j => j.kind === 'infer' && (j.status === 'running' || j.status === 'queued'));
}

// 加载工作区文件树;preserve 时保留已展开目录(轮询刷新用)
// state.videos 来自递归的 /api/workspace/videos(任意深度,含 stem+rel)
export async function loadTree(preserve) {
  const prevExpanded = preserve ? Array.from(state.tree.expanded) : [];
  state.tree.loaded = false;
  state.tree.children = {};
  state.tree.expanded = new Set(prevExpanded);
  try {
    const data = await api('/api/workspace/tree');
    state.tree.root = data.entries || [];
  } catch (e) {
    state.tree.root = [];
  }
  try {
    state.videos = await api('/api/workspace/videos') || [];
  } catch (e) {
    state.videos = [];
  }
  // 已展开目录重新拉取子级(可能新增/删除了文件)
  await Promise.all(prevExpanded.map(async rel => {
    try {
      const data = await api('/api/workspace/tree?path=' + encodeURIComponent(rel));
      state.tree.children[rel] = data.entries || [];
    } catch (e) {
      state.tree.expanded.delete(rel);
    }
  }));
  state.tree.loaded = true;
  renderSidebar();
}
