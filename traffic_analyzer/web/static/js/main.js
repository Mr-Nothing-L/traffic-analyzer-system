/* ==========================================================================
   高速交通事件分析台 — 前端(原生 JS,无框架无构建)
   消费契约 REST API;开发时追加 ?mock=1 使用内置模拟数据。
   ========================================================================== */
import { $ } from './util.js';
import { MOCK, state } from './state.js';
import { api } from './api.js';
import { mockTick } from './mock.js';
import {
  SIDE_FILTER_KEY, SIDE_SORT_KEY,
  loadTree, renderSidebar, syncButtons, invalidateSidebar,
} from './tree.js';
import { renderWelcome } from './preview.js';
import { loadEvalLatest, pollJobs, schedulePoll, startInfer, runEvaluate } from './jobs.js';
import {
  browseWorkspace, closeDirModal, confirmDir, showDirInput,
  onDirRecentChange, dirModalKeys, navDir,
} from './workspace.js';

/* ================================================================
   侧栏拖动分隔条
   ================================================================ */
const SIDEBAR_WIDTH_KEY = 'ta_sidebar_width';
const SIDEBAR_DEFAULT_WIDTH = 264;
const SIDEBAR_MIN_WIDTH = 180;
const SIDEBAR_MAX_WIDTH = 560;

function applySidebarWidth(px) {
  const w = Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.round(px)));
  const sidebar = $('#sidebar');
  sidebar.style.width = w + 'px';
  sidebar.style.flex = '0 0 ' + w + 'px';
  return w;
}

function initSplitter() {
  const splitter = $('#splitter');
  // 恢复上次保存的宽度
  const saved = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY), 10);
  if (!isNaN(saved)) applySidebarWidth(saved);

  let startX = 0, startWidth = 0;

  function onMove(e) {
    applySidebarWidth(startWidth + e.clientX - startX);
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    splitter.classList.remove('dragging');
    document.body.classList.remove('splitter-dragging');
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(parseInt($('#sidebar').style.width, 10)));
  }

  splitter.addEventListener('mousedown', e => {
    startX = e.clientX;
    startWidth = $('#sidebar').getBoundingClientRect().width;
    splitter.classList.add('dragging');
    document.body.classList.add('splitter-dragging'); // 拖拽中禁止选中文字
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  });

  splitter.addEventListener('dblclick', () => {
    applySidebarWidth(SIDEBAR_DEFAULT_WIDTH); // 双击复位默认宽度
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(SIDEBAR_DEFAULT_WIDTH));
  });
}

/* ================================================================
   初始化
   ================================================================ */
function initToolbar() {
  if (MOCK) $('#mock-badge').hidden = false;

  $('#btn-workspace').addEventListener('click', browseWorkspace);
  $('#dir-close').addEventListener('click', closeDirModal);
  $('#dir-cancel').addEventListener('click', closeDirModal);
  $('#dir-confirm').addEventListener('click', confirmDir);
  $('#dir-edit').addEventListener('click', showDirInput);
  $('#dir-recent-select').addEventListener('change', onDirRecentChange);
  $('#dir-modal').addEventListener('mousedown', e => {
    if (e.target === e.currentTarget) closeDirModal(); // 点击遮罩关闭
  });
  $('#dir-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const p = e.target.value.trim();
      if (p) navDir(p);
      e.stopPropagation();
    }
    if (e.key === 'Escape') {
      $('#dir-input').hidden = true;
      $('#dir-crumbs').hidden = false;
      e.stopPropagation(); // 仅退出输入态,不关闭弹窗
    }
  });
  document.addEventListener('keydown', dirModalKeys);

  // 侧栏分隔条
  initSplitter();

  $('#btn-infer').addEventListener('click', startInfer);
  $('#btn-evaluate').addEventListener('click', runEvaluate);

  $('#check-all').addEventListener('change', e => {
    state.checked.clear();
    if (e.target.checked) state.videos.forEach(v => state.checked.add(v.rel));
    invalidateSidebar();
    renderSidebar();
    syncButtons();
  });

  // 侧栏过滤/排序:恢复上次持久化值,变更时仅重渲染(不影响勾选状态)
  const filterInput = $('#side-filter-input');
  const sortSelect = $('#side-sort-select');
  state.filter = localStorage.getItem(SIDE_FILTER_KEY) || '';
  state.sort = localStorage.getItem(SIDE_SORT_KEY) || 'name';
  filterInput.value = state.filter;
  sortSelect.value = state.sort;
  filterInput.addEventListener('input', () => {
    state.filter = filterInput.value.trim();
    localStorage.setItem(SIDE_FILTER_KEY, state.filter);
    invalidateSidebar();
    renderSidebar();
  });
  sortSelect.addEventListener('change', () => {
    state.sort = sortSelect.value;
    localStorage.setItem(SIDE_SORT_KEY, state.sort);
    invalidateSidebar();
    renderSidebar();
  });
}

async function init() {
  initToolbar();
  if (MOCK) setInterval(mockTick, 700);

  try {
    state.workspace = await api('/api/workspace');
    if (state.workspace && state.workspace.path) {
      $('#ws-path').textContent = state.workspace.path;
      await Promise.all([loadTree(), loadEvalLatest(), pollJobs()]);
    }
  } catch (e) {
    // 后端未就绪:保持初始界面
  }
  renderWelcome();
  renderSidebar();
  syncButtons();
  schedulePoll(); // setTimeout 链:活动任务 1.5s / 空闲 5s,页面隐藏时暂停
}

init();
