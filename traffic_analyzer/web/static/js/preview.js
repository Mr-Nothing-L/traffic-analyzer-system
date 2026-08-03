/* ================================================================
   主区(编排:欢迎页/分析视图骨架与各卡挂载)
   ================================================================ */
import { $, esc } from './util.js';
import { state, runCleanups } from './state.js';
import { api, videoSource } from './api.js';
import { latestJobForStem, renderSidebar, invalidateSidebar, loadTree, syncButtons } from './tree.js';
import { pollJobs } from './jobs.js';
import { renderSftBody } from './sft.js';
import { renderEvidenceCard } from './evidence.js';
import { mountExpertPanel } from './expert_panel.js';
import { mountPreview } from './video_preview.js';
import { renderReportBody } from './markdown.js';

export function renderWelcome() {
  runCleanups();
  const main = $('#main');
  delete main.dataset.renderedStem; // 离开分析视图,下次 renderResults 需整体重建
  const hasWs = !!(state.workspace && state.workspace.path);
  // 树未加载(state.tree.loaded=false)时给醒目的「加载工作区」按钮:
  // 刷新后不再自动 loadTree(大工作区 >10s),由用户显式触发
  const treeLoaded = state.tree && state.tree.loaded;
  main.innerHTML =
    '<div class="welcome">'
    + '<div class="hero">'
    + '<h1>高速交通事件分析台</h1>'
    + (hasWs
      ? '<p>当前工作区:<span class="hint-kbd">' + esc(state.workspace.path) + '</span></p>'
        + (treeLoaded
          ? '<p>在左侧勾选视频后点击「开始推理」;点击视频名查看 SFT 标注、分析报告与可视化证据。</p>'
          : '<p><button id="btn-load-workspace" class="btn btn-primary"'
            + ' style="font-size:17px;padding:12px 36px;">加载工作区</button></p>'
            + '<p>大工作区加载需要一些时间;「数据看板」无需等待加载,可直接打开。</p>')
      : '<p>请先点击顶部「选择工作区…」按钮,选择包含视频文件的目录。</p>')
    + '<p>开发模式:在地址后追加 <span class="hint-kbd">?mock=1</span> 可使用内置模拟数据。</p>'
    + '</div>'
    + '</div>';
  const loadBtn = $('#btn-load-workspace');
  if (loadBtn) loadBtn.addEventListener('click', loadWorkspaceOnDemand);
}

// 欢迎页「加载工作区」:显式触发 tree/videos 加载 + 任务轮询首轮
async function loadWorkspaceOnDemand() {
  const btn = $('#btn-load-workspace');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '正在加载工作区,请稍候…';
  }
  await loadTree();
  await pollJobs();
  renderWelcome(); // tree.loaded → 恢复常规操作提示
  syncButtons();
}

function skeletons() {
  const sk = '<div class="skel" style="height:16px;width:%W%"></div>';
  return '<div class="cards">'
    + '<div class="skel-card">' + sk.replace('%W', '40%') + sk.replace('%W', '90%') + sk.replace('%W', '75%') + '</div>'
    + '<div class="skel-card">' + sk.replace('%W', '30%') + sk.replace('%W', '95%') + sk.replace('%W', '60%') + '</div>'
    + '<div class="skel-card">' + sk.replace('%W', '50%') + sk.replace('%W', '85%') + '</div>'
    + '</div>';
}

export async function selectVideo(rel) {
  const v = state.videos.find(v => v.rel === rel);
  if (!v) return;
  const prevRel = state.currentRel;
  state.currentStem = v.stem;
  state.currentRel = v.rel;
  state.evTabIdx = 0;
  state.results = null;
  state.evidenceDraft = null;
  state.evidenceDirty = false;
  state.sftDraft = null;    // 切换视频时丢弃未保存的 SFT 草稿,避免幽灵 dirty 态
  state.sftSavedSig = '';
  invalidateSidebar(); renderSidebar();
  runCleanups();
  const main = $('#main');
  // 跨目录同 stem 切换(rel 不同):旧 contenteditable 残留绑定的是已丢弃草稿,
  // 复用 DOM 会空指针——清 renderedStem 强制整体重建(同 rel 重进仍保留 DOM 防闪白)
  if (prevRel && prevRel !== v.rel) delete main.dataset.renderedStem;
  // 注:跨目录同 stem 时草稿按 rel 键迁移暂不做,切换即丢弃草稿
  if (main.dataset.renderedStem !== v.stem) {
    // 同 stem 重进(queued→running/完成后自动重载):保留现有 DOM(含视频预览),
    // 不铺骨架,避免全屏闪白;renderResults 会只重建卡片区
    main.innerHTML = skeletons();
  }
  try {
    const results = await api('/api/results/' + encodeURIComponent(v.stem));
    if (state.currentRel !== rel) return; // 期间切换了视频,过期响应不得写入共享状态
    state.results = results;
    if (results.evidence) state.evidenceDraft = JSON.parse(JSON.stringify(results.evidence));
  } catch (e) {
    if (state.currentRel !== rel) return; // 过期请求的失败不覆盖当前视图
    $('#main').innerHTML = '<div class="cards"><div class="card"><div class="card-body empty-note">加载结果失败:' + esc(e.message) + '</div></div></div>';
    return;
  }
  renderResults();
}

function renderResults() {
  runCleanups();
  const stem = state.currentStem;
  const source = videoSource({ stem: state.currentStem, rel: state.currentRel });
  const r = state.results || {};
  const hasResults = !!(r.sft_label || r.report_md || r.evidence);
  const main = $('#main');

  // stem 未变且骨架仍在:保留 #pane-top 视频预览 DOM(不重载视频,避免闪白),
  // 仅重建下方卡片区;stem 变了(或骨架被其他视图替换)才整体重建
  if (main.dataset.renderedStem === stem && $('#pane-top') && $('#pane-cards')) {
    renderResultCards(stem, source, r, hasResults);
    return;
  }
  main.dataset.renderedStem = stem;
  main.innerHTML =
    '<div class="split-col">'
    + '<div class="pane-top" id="pane-top">'
    + '<div class="card" id="card-preview"><div class="card-head"><span class="card-title">视频预览</span>'
    + '<span class="card-sub">' + esc(stem) + '</span></div>'
    + '<div class="card-body" id="preview-body"></div></div>'
    + '</div>'
    + '<div class="hsplit" id="hsplit" title="拖动调整预览高度,双击复位"><span></span></div>'
    + '<div class="pane-bottom" id="pane-bottom">'
    + '<div class="cards" id="pane-cards"></div></div>'
    + '</div>';

  mountPreview(source, r.evidence && r.evidence.video);
  initHSplit(); // hsplit 分隔条只在整体重建时初始化
  renderResultCards(stem, source, r, hasResults);
}

// 重建 #pane-cards 卡片区(SFT/报告/证据/专家工作间),并重新挂载各卡内容
function renderResultCards(stem, source, r, hasResults) {
  const cards = $('#pane-cards');
  if (!cards) return;
  let html = '';
  if (hasResults) {
    html +=
      '<div class="card" id="card-sft"><div class="card-head"><span class="card-title">SFT 标注详情</span>'
      + '<span class="card-sub">' + esc(stem) + '</span></div>'
      + '<div class="card-body" id="sft-body"></div></div>'

      + '<div class="card" id="card-report"><div class="card-head"><span class="card-title">分析报告</span></div>'
      + '<div class="card-body" id="report-body"></div></div>'

      + '<div class="card" id="card-evidence"><div class="card-head"><span class="card-title">证据编辑</span>'
      + '<span class="card-sub">拖拽多边形端点 / 证据框角点进行调整</span><span class="spacer"></span>'
      + '<span class="dirty-flag" id="dirty-flag" hidden>● 未保存</span>'
      + '<button class="btn btn-ghost btn-sm" id="btn-ev-reset" disabled>重置</button>'
      + '<button class="btn btn-primary btn-sm" id="btn-ev-save" disabled>保存</button></div>'
      + '<div id="ev-tabs" class="ev-tabs"></div>'
      + '<div class="card-body" id="ev-body"></div></div>';
  } else {
    const job = latestJobForStem(stem);
    if (job && job.status === 'running') {
      // 推理进行中:empty-note 替换为「专家工作间」面板(泳道进度由 mountExpertPanel 驱动)
      html += '<div class="card" id="card-experts"><div class="card-head">'
        + '<span class="card-title">专家工作间</span>'
        + '<span class="card-sub">' + esc(stem) + '</span></div>'
        + '<div class="card-body" id="experts-body"></div></div>';
    } else {
      const note = job && job.status === 'queued'
        ? '该视频正在推理队列中,完成后此处将展示 SFT 标注、分析报告与证据。'
        : (job && job.status === 'failed'
          ? '该视频上次推理未完成(已停止或失败),暂无分析结果。可在左侧点击 ↻ 重试,或重新勾选后点击「开始推理」。'
          : '该视频尚未推理,暂无分析结果。在左侧勾选后点击「开始推理」即可分析。');
      html += '<div class="card"><div class="card-body empty-note">' + esc(note) + '</div></div>';
    }
  }
  cards.innerHTML = html;

  if (hasResults) {
    renderSftBody(r.sft_label);
    renderReportBody(r.report_md, stem);
    renderEvidenceCard(stem, source);
  } else {
    const job = latestJobForStem(stem);
    if (job && job.status === 'running') mountExpertPanel(job);
  }
}

/* ------------------------------------------------------------ 上下分隔条(预览常驻) */
const HSPLIT_KEY = 'ta_preview_split';
const HSPLIT_DEFAULT = 0.46;
const HSPLIT_MIN_PX = 150;
const HSPLIT_MAX_RATIO = 0.8;

function applyPaneTopRatio(ratio) {
  const paneTop = $('#pane-top');
  if (paneTop) paneTop.style.height = (ratio * 100).toFixed(2) + '%';
}

function initHSplit() {
  const hsplit = $('#hsplit');
  const paneTop = $('#pane-top');
  if (!hsplit || !paneTop) return;
  const main = $('#main');

  // 整体重建(切换 stem)后需重新应用持久化比例(存比例而非像素,窗口缩放按比例重算)
  const saved = parseFloat(localStorage.getItem(HSPLIT_KEY));
  applyPaneTopRatio(!isNaN(saved) && saved > 0 && saved <= 1 ? saved : HSPLIT_DEFAULT);

  let startY = 0, startHeight = 0;

  function onMove(e) {
    const mainH = main.getBoundingClientRect().height;
    const h = Math.max(HSPLIT_MIN_PX, Math.min(mainH * HSPLIT_MAX_RATIO, startHeight + e.clientY - startY));
    paneTop.style.height = Math.round(h) + 'px';
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    hsplit.classList.remove('dragging');
    document.body.classList.remove('hsplit-dragging');
    const mainH = main.getBoundingClientRect().height;
    if (mainH > 0) {
      localStorage.setItem(HSPLIT_KEY, String(paneTop.getBoundingClientRect().height / mainH));
    }
  }

  hsplit.addEventListener('mousedown', e => {
    if (!hsplit.isConnected) return; // 主区已重建,残留节点不再响应拖拽
    startY = e.clientY;
    startHeight = paneTop.getBoundingClientRect().height;
    hsplit.classList.add('dragging');
    document.body.classList.add('hsplit-dragging'); // 拖拽中禁止选中文字
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  });

  // 主区重渲染时兜底摘除 document 级监听(拖拽中途视图被替换的场景;未注册时为 no-op)
  state.cleanups.push(() => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  });

  hsplit.addEventListener('dblclick', () => {
    applyPaneTopRatio(HSPLIT_DEFAULT); // 双击复位默认比例
    localStorage.setItem(HSPLIT_KEY, String(HSPLIT_DEFAULT));
  });
}
