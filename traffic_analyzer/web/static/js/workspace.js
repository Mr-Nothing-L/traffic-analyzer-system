/* ================================================================
   动作:工作区
   ================================================================ */
import { $, $$, esc, toast } from './util.js';
import { state } from './state.js';
import { api } from './api.js';
import { loadTree, renderSidebar, syncButtons } from './tree.js';
import { loadEvalLatest } from './jobs.js';
import { renderWelcome } from './preview.js';

// 顶栏工作区按钮两段式:主标签「工作区」+ 次级详细路径(未选择时只有「选择工作区…」)
export function setWorkspaceLabel(ws) {
  const label = $('#ws-label');
  const pathEl = $('#ws-path');
  const path = ws && ws.path;
  label.textContent = path ? '工作区' : '选择工作区…';
  pathEl.hidden = !path;
  if (path) {
    pathEl.textContent = path;
    pathEl.title = path;
  }
}

// 工作区已切换后的统一刷新(目录弹窗确认后调用)
export async function applyWorkspace(ws) {
  state.workspace = ws;
  setWorkspaceLabel(ws);
  state.currentStem = null;
  state.currentRel = null;
  state.checked.clear();
  state.evalData = null;
  // 工作区切换:丢弃一切与当前视频绑定的草稿/dirty 态,避免 hasUnsavedEdits() 幽灵为真
  state.results = null;
  state.evidenceDraft = null;
  state.evidenceDirty = false;
  state.sftDraft = null;
  state.sftSavedSig = '';
  await Promise.all([loadTree(), loadEvalLatest()]);
  renderWelcome();
  renderSidebar();
  syncButtons();
}

/* ================================================================
   工作区目录弹窗(浏览服务器文件系统,替代原生系统对话框)
   ================================================================ */
const dirModal = { open: false, cwd: null, parent: null, dirs: [], selected: null, loading: false };

/* ---- 最近使用的工作区(localStorage 持久化,最新在前,去重,最多 8 条) ---- */
const RECENT_WS_KEY = 'ta_recent_workspaces';
const RECENT_WS_MAX = 8;

function loadRecentWorkspaces() {
  try {
    const arr = JSON.parse(localStorage.getItem(RECENT_WS_KEY) || '[]');
    return Array.isArray(arr) ? arr.filter(p => typeof p === 'string') : [];
  } catch (e) { return []; }
}

function pushRecentWorkspace(path) {
  const arr = loadRecentWorkspaces().filter(p => p !== path);
  arr.unshift(path);
  try { localStorage.setItem(RECENT_WS_KEY, JSON.stringify(arr.slice(0, RECENT_WS_MAX))); } catch (e) { /* 存储不可用时静默忽略 */ }
}

// 渲染「最近使用」下拉:当前工作区 + 历史路径 + 主目录(始终非空)
function renderDirRecent() {
  const sel = $('#dir-recent-select');
  const cur = state.workspace && state.workspace.path;
  let html = '<option value="">快速跳转到…</option>';
  if (cur) html += '<option value="__current__">当前工作区 (' + esc(cur) + ')</option>';
  loadRecentWorkspaces().forEach(p => {
    html += '<option value="' + esc(p) + '">' + esc(p) + '</option>';
  });
  html += '<option value="__home__">主目录</option>';
  sel.innerHTML = html;
  sel.value = '';
}

// 下拉选中某项:跳转目录(不自动确认),随后复位到占位项,避免误显示当前目录
export function onDirRecentChange(e) {
  const v = e.target.value;
  e.target.value = '';
  if (v === '__home__') navDir(null);
  else if (v === '__current__' && state.workspace) navDir(state.workspace.path);
  else if (v) navDir(v);
}

// 点击「选择工作区…」:打开页内目录导航弹窗
export function browseWorkspace() {
  dirModal.open = true;
  $('#dir-modal').hidden = false;
  $('#dir-input').hidden = true;
  $('#dir-crumbs').hidden = false;
  renderDirRecent();
  $('.dir-dialog').focus();
  const start = state.workspace && state.workspace.path ? state.workspace.path : null;
  navDir(start); // 无 path 时后端回退到当前工作区或用户主目录
}

export function closeDirModal() {
  dirModal.open = false;
  $('#dir-modal').hidden = true;
  $('#btn-workspace').focus();
}

// 目录导航竞态防护:并发 navDir 时只落地最后一次响应(参照 selectVideo 的 currentRel 模式)
let navSeq = 0;

export async function navDir(path) {
  const seq = ++navSeq;
  dirModal.loading = true;
  renderDirList();
  let data = null;
  try {
    data = await api(path ? '/api/fs/list?path=' + encodeURIComponent(path) : '/api/fs/list');
  } catch (e) {
    if (seq === navSeq) toast('读取目录失败(' + e.status + '):' + e.message, 'err');
  }
  if (seq !== navSeq) return; // 已有更新的导航请求:过期响应丢弃
  dirModal.loading = false;
  if (data) {
    dirModal.cwd = data.path;
    dirModal.parent = data.parent;
    dirModal.dirs = data.dirs || [];
    dirModal.selected = data.path; // 当前目录即默认选择
    $('#dir-input').hidden = true;
    $('#dir-crumbs').hidden = false;
    renderDirCrumbs();
  }
  renderDirList();
  renderDirFoot();
}

function renderDirCrumbs() {
  const cwd = dirModal.cwd || '/';
  const parts = cwd.split('/').filter(Boolean);
  let html = '<span class="dir-crumb" data-path="/" title="/">/</span>';
  let acc = '';
  parts.forEach((p, i) => {
    acc += '/' + p;
    html += '<span class="dir-crumb-sep">›</span>'
      + '<span class="dir-crumb' + (i === parts.length - 1 ? ' current' : '') + '" data-path="'
      + esc(acc) + '" title="' + esc(acc) + '">' + esc(p) + '</span>';
  });
  const crumbs = $('#dir-crumbs');
  crumbs.innerHTML = html;
  $$('.dir-crumb', crumbs).forEach(c => c.addEventListener('click', () => navDir(c.dataset.path)));
}

function renderDirList() {
  const list = $('#dir-list');
  if (dirModal.loading) {
    list.innerHTML = '<div class="dir-state"><div class="dir-spinner"></div>加载中…</div>';
    return;
  }
  if (!dirModal.cwd) {
    list.innerHTML = '<div class="dir-state">无法读取目录,可点击 ✎ 手动输入路径</div>';
    return;
  }
  let html = '';
  if (dirModal.parent) {
    html += '<div class="dir-row dir-up" data-path="' + esc(dirModal.parent) + '">'
      + '<span class="dir-ico">⬆</span><span class="dir-name">..</span></div>';
  }
  html += dirModal.dirs.map(d =>
    '<div class="dir-row' + (dirModal.selected === d.path ? ' selected' : '')
    + '" data-path="' + esc(d.path) + '" data-dir="1">'
    + '<span class="dir-ico">📁</span><span class="dir-name">' + esc(d.name) + '</span></div>'
  ).join('');
  if (!dirModal.dirs.length) html += '<div class="dir-state">此目录没有子文件夹</div>';
  list.innerHTML = html;

  $$('.dir-row', list).forEach(row => {
    row.addEventListener('click', () => {
      if (row.dataset.dir) selectDirRow(row); // 单击选中
      else navDir(row.dataset.path);          // 「..」直接进入上级
    });
    row.addEventListener('dblclick', () => {
      if (row.dataset.dir) navDir(row.dataset.path); // 双击进入
    });
  });
}

function selectDirRow(row) {
  dirModal.selected = row.dataset.path;
  $$('#dir-list .dir-row').forEach(r => r.classList.toggle('selected', r === row));
  renderDirFoot();
}

function renderDirFoot() {
  const el = $('#dir-selected');
  el.textContent = dirModal.selected || dirModal.cwd || '';
  el.title = el.textContent;
}

export function showDirInput() {
  const input = $('#dir-input');
  $('#dir-crumbs').hidden = true;
  input.hidden = false;
  input.value = dirModal.cwd || '';
  input.focus();
  input.select();
}

export async function confirmDir() {
  const path = dirModal.selected || dirModal.cwd;
  if (!path) return;
  const btn = $('#dir-confirm');
  btn.disabled = true;
  try {
    const ws = await api('/api/workspace', { method: 'POST', body: { path: path } });
    pushRecentWorkspace(path); // 确认成功后记入「最近使用」
    await applyWorkspace(ws);
    closeDirModal();
    toast('已选择工作区:' + ws.path);
  } catch (e) {
    toast('设置工作区失败(' + e.status + '):' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// ESC 关闭;Tab 在弹窗内循环(简易焦点陷阱)
export function dirModalKeys(e) {
  if (!dirModal.open) return;
  if (e.key === 'Escape') { closeDirModal(); return; }
  if (e.key !== 'Tab') return;
  const els = $$('#dir-modal button, #dir-modal input, #dir-modal select').filter(el => !el.hidden && !el.disabled);
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
  else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
}
