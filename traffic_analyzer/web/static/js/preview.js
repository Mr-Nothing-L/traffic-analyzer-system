/* ================================================================
   主区
   ================================================================ */
import { $, esc } from './util.js';
import { MOCK, state } from './state.js';
import { api, videoSource, metaUrl, sourceFrameUrl, imageUrl } from './api.js';
import { latestJobForStem, renderSidebar, invalidateSidebar } from './tree.js';
import { renderSftBody } from './sft.js';
import { renderEvidenceCard } from './evidence.js';
import { renderEvalCard } from './jobs.js';

export function runCleanups() {
  state.cleanups.forEach(fn => { try { fn(); } catch (e) { /* ignore */ } });
  state.cleanups = [];
}

export function renderWelcome() {
  runCleanups();
  const main = $('#main');
  main.innerHTML =
    '<div class="welcome">'
    + '<div class="hero">'
    + '<h1>高速交通事件分析台</h1>'
    + (state.workspace && state.workspace.path
      ? '<p>当前工作区:<span class="hint-kbd">' + esc(state.workspace.path) + '</span></p>'
        + '<p>在左侧勾选视频后点击「开始推理」;点击视频名查看 SFT 标注、分析报告与可视化证据。</p>'
      : '<p>请先点击顶部「选择工作区…」按钮,选择包含视频文件的目录。</p>')
    + '<p>开发模式:在地址后追加 <span class="hint-kbd">?mock=1</span> 可使用内置模拟数据。</p>'
    + '</div>'
    + '<div id="eval-card-slot"></div>'
    + '</div>';
  renderEvalCard();
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
  state.currentStem = v.stem;
  state.currentRel = v.rel;
  state.evTabIdx = 0;
  state.results = null;
  state.evidenceDraft = null;
  state.evidenceDirty = false;
  invalidateSidebar(); renderSidebar();
  runCleanups();
  $('#main').innerHTML = skeletons();
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
  let html =
    '<div class="split-col">'
    + '<div class="pane-top" id="pane-top">'
    + '<div class="card" id="card-preview"><div class="card-head"><span class="card-title">视频预览</span>'
    + '<span class="card-sub">' + esc(stem) + '</span></div>'
    + '<div class="card-body" id="preview-body"></div></div>'
    + '</div>'
    + '<div class="hsplit" id="hsplit" title="拖动调整预览高度,双击复位"><span></span></div>'
    + '<div class="pane-bottom" id="pane-bottom">'
    + '<div class="cards">';

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
    const note = job && (job.status === 'running' || job.status === 'queued')
      ? '该视频正在推理队列中,完成后此处将展示 SFT 标注、分析报告与证据。'
      : '该视频尚未推理,暂无分析结果。在左侧勾选后点击「开始推理」即可分析。';
    html += '<div class="card"><div class="card-body empty-note">' + esc(note) + '</div></div>';
  }

  html += '<div id="eval-card-slot"></div></div></div></div>';
  main.innerHTML = html;

  mountPreview(source, r.evidence && r.evidence.video);
  if (hasResults) {
    renderSftBody(r.sft_label);
    renderReportBody(r.report_md, stem);
    renderEvidenceCard(stem, source);
  }
  renderEvalCard();
  initHSplit();
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

  // renderResults 每次重建 innerHTML,需重新应用持久化比例(存比例而非像素,窗口缩放按比例重算)
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
    startY = e.clientY;
    startHeight = paneTop.getBoundingClientRect().height;
    hsplit.classList.add('dragging');
    document.body.classList.add('hsplit-dragging'); // 拖拽中禁止选中文字
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  });

  hsplit.addEventListener('dblclick', () => {
    applyPaneTopRatio(HSPLIT_DEFAULT); // 双击复位默认比例
    localStorage.setItem(HSPLIT_KEY, String(HSPLIT_DEFAULT));
  });
}

/* ------------------------------------------------------------ 视频预览卡 */
function streamUrl(source, ss) {
  if (MOCK) return null; // mock 模式无真实视频流,直接走逐帧预览
  let url = source.rel != null
    ? '/api/workspace/stream?path=' + encodeURIComponent(source.rel)
    : '/api/videos/' + encodeURIComponent(source.stem) + '/stream';
  if (ss != null && ss > 0) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'ss=' + ss.toFixed(2);
  return url;
}

function mountPreview(source, videoInfo) {
  const body = $('#preview-body');
  if (!body) return;
  const url = streamUrl(source);
  body.innerHTML =
    '<div class="pv-wrap" id="pv-wrap">'
    + '<video id="pv-video" controls preload="metadata" playsinline></video></div>'
    + '<div id="pv-stepper" hidden></div>';

  const showStepper = hint => {
    $('#pv-wrap').hidden = true;
    mountFrameStepper($('#pv-stepper'), source, hint,
      () => mountPreview(source, videoInfo), videoInfo);
  };

  if (!url) { showStepper('模拟模式下无真实视频流,以下为逐帧预览。'); return; }

  const video = $('#pv-video');
  video.addEventListener('error', () => {
    showStepper('浏览器无法直接播放该视频(编码不受支持或转码服务不可用),已切换为逐帧预览。');
  });
  video.src = url;
}

// 逐帧预览:先取真实帧数元数据,失败则显示错误态(不出现滑块/黑框)
function mountFrameStepper(mount, source, hint, onRetry, videoInfo) {
  mount.hidden = false;
  if (MOCK) {
    // mock 模式无元数据接口:沿用 evidence 估算帧数的画布帧
    const total = videoInfo && videoInfo.duration_sec && videoInfo.fps
      ? Math.max(1, Math.round(videoInfo.duration_sec * videoInfo.fps)) : 300;
    buildStepper(mount, source, total, hint, onRetry);
    return;
  }
  mount.innerHTML = '<div class="pv-hint"><span>' + esc(hint) + '</span></div>'
    + '<div class="empty-note">读取视频信息…</div>';
  api(metaUrl(source)).then(meta => {
    if (!mount.isConnected) return; // 期间切换了视图
    buildStepper(mount, source, meta.frame_count, hint, onRetry);
  }).catch(() => {
    if (!mount.isConnected) return;
    mount.innerHTML =
      '<div class="pv-hint"><span>无法读取该视频的帧(文件损坏或编码无法识别)</span>'
      + '<button class="btn btn-ghost btn-sm" id="pv-retry">重试播放</button></div>';
    $('#pv-retry', mount).addEventListener('click', onRetry);
  });
}

function buildStepper(mount, source, total, hint, onRetry) {
  mount.innerHTML =
    '<div class="pv-hint"><span>' + esc(hint) + '</span>'
    + '<button class="btn btn-ghost btn-sm" id="pv-retry">重试播放</button></div>'
    + '<div class="pv-stage"><img id="pv-img" alt="帧预览">'
    + '<span class="pv-frame-err" id="pv-frame-err" hidden>帧读取失败</span></div>'
    + '<div class="pv-slider-row">'
    + '<input type="range" id="pv-slider" min="0" max="' + (total - 1) + '" value="0" step="1">'
    + '<span class="pv-idx" id="pv-idx">0 / ' + (total - 1) + '</span></div>';

  const img = $('#pv-img', mount);
  const frameErr = $('#pv-frame-err', mount);
  const slider = $('#pv-slider', mount);
  const idxLabel = $('#pv-idx', mount);
  img.src = sourceFrameUrl(source, 0);
  slider.addEventListener('input', () => {
    const idx = +slider.value;
    idxLabel.textContent = idx + ' / ' + slider.max;
    img.src = sourceFrameUrl(source, idx);
  });
  // 单帧读取失败:仅显示占位提示,不改动滑块范围
  img.addEventListener('load', () => { img.hidden = false; frameErr.hidden = true; });
  img.addEventListener('error', () => { img.hidden = true; frameErr.hidden = false; });
  $('#pv-retry', mount).addEventListener('click', onRetry);
}

/* ------------------------------------------------------------ 报告卡 */
/* 极小 markdown → html:标题/加粗/表格/列表/代码块/图片/引用/分割线 */
function mdInline(text, resolveImg) {
  let s = esc(text);
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => {
    const url = /^https?:|^data:/.test(src) ? src : (resolveImg ? resolveImg(src) : src);
    return '<img alt="' + alt + '" src="' + esc(url) + '">';
  });
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  return s;
}

function mdToHtml(md, resolveImg) {
  const lines = String(md || '').split('\n');
  const out = [];
  let i = 0;
  let listStack = null; // 'ul' | 'ol'
  const closeList = () => { if (listStack) { out.push('</' + listStack + '>'); listStack = null; } };

  while (i < lines.length) {
    const line = lines[i];

    // 代码块
    if (/^```/.test(line)) {
      closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
      continue;
    }
    // 表格
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const parseRow = l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const head = parseRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(parseRow(lines[i])); i++; }
      out.push('<table><thead><tr>' + head.map(h => '<th>' + mdInline(h, resolveImg) + '</th>').join('')
        + '</tr></thead><tbody>'
        + rows.map(r => '<tr>' + r.map(c => '<td>' + mdInline(c, resolveImg) + '</td>').join('') + '</tr>').join('')
        + '</tbody></table>');
      continue;
    }
    // 标题
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); out.push('<h' + h[1].length + '>' + mdInline(h[2], resolveImg) + '</h' + h[1].length + '>'); i++; continue; }
    // 分割线
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { closeList(); out.push('<hr>'); i++; continue; }
    // 引用
    if (/^>\s?/.test(line)) { closeList(); out.push('<blockquote>' + mdInline(line.replace(/^>\s?/, ''), resolveImg) + '</blockquote>'); i++; continue; }
    // 列表
    let m = line.match(/^\s*[-*+]\s+(.*)$/);
    if (m) {
      if (listStack !== 'ul') { closeList(); out.push('<ul>'); listStack = 'ul'; }
      out.push('<li>' + mdInline(m[1], resolveImg) + '</li>'); i++; continue;
    }
    m = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (m) {
      if (listStack !== 'ol') { closeList(); out.push('<ol>'); listStack = 'ol'; }
      out.push('<li>' + mdInline(m[1], resolveImg) + '</li>'); i++; continue;
    }
    // 空行 / 段落
    closeList();
    if (line.trim() === '') { i++; continue; }
    out.push('<p>' + mdInline(line, resolveImg) + '</p>');
    i++;
  }
  closeList();
  return '<div class="md">' + out.join('\n') + '</div>';
}

function renderReportBody(reportMd, stem) {
  const body = $('#report-body');
  if (!body) return;
  if (!reportMd) { body.innerHTML = '<div class="empty-note">无分析报告</div>'; return; }
  body.innerHTML = mdToHtml(reportMd, src => imageUrl(stem, src));
}
