/* ================================================================
   主区
   ================================================================ */
import { $, esc } from './util.js';
import { MOCK, state } from './state.js';
import { api, videoSource, metaUrl, sourceFrameUrl, imageUrl } from './api.js';
import { latestJobForStem, renderSidebar, invalidateSidebar } from './tree.js';
import { renderSftBody } from './sft.js';
import { renderEvidenceCard } from './evidence.js';
import { renderEvalCard, cancelJob, jobStepText } from './jobs.js';

export function runCleanups() {
  state.cleanups.forEach(fn => { try { fn(); } catch (e) { /* ignore */ } });
  state.cleanups = [];
}

export function renderWelcome() {
  runCleanups();
  const main = $('#main');
  delete main.dataset.renderedStem; // 离开分析视图,下次 renderResults 需整体重建
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
  state.sftDraft = null;    // 切换视频时丢弃未保存的 SFT 草稿,避免幽灵 dirty 态
  state.sftSavedSig = '';
  invalidateSidebar(); renderSidebar();
  runCleanups();
  const main = $('#main');
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

// 重建 #pane-cards 卡片区(SFT/报告/证据/专家工作间/评估卡),并重新挂载各卡内容
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
  html += '<div id="eval-card-slot"></div>';
  cards.innerHTML = html;

  if (hasResults) {
    renderSftBody(r.sft_label);
    renderReportBody(r.report_md, stem);
    renderEvidenceCard(stem, source);
  } else {
    const job = latestJobForStem(stem);
    if (job && job.status === 'running') mountExpertPanel(job);
  }
  renderEvalCard();
}

/* ------------------------------------------------------------ 专家工作间(推理进行面板) */
// GET /api/expert-phases 的阶段定义缓存(每类别 [{fraction, label}]);404 时记 null,走内置 fallback 封顶
let expertPhasesPromise = null;
function loadExpertPhases() {
  if (!expertPhasesPromise) {
    expertPhasesPromise = api('/api/expert-phases')
      .then(d => (d && d.categories) || d || null)
      .catch(() => null);
  }
  return expertPhasesPromise;
}

// 泳道 displayed 到达 target 后的缓行封顶:阶段序列中 displayed 之后的下一个里程碑(绝不越过);
// 无阶段定义(/api/expert-phases 404)时封顶在当前 fraction+0.1 或 1.0
function nextMilestone(phases, name, displayed, target) {
  const seq = phases && phases[name];
  if (Array.isArray(seq)) {
    const next = seq.map(s => s.fraction)
      .filter(f => typeof f === 'number' && f > displayed + 0.005)
      .sort((a, b) => a - b)[0];
    if (next != null) return Math.min(next, 1);
  }
  return Math.min(Math.max(target, displayed) + 0.1, 1);
}

// 泳道状态 → 样式类:queued 灰 / running 橙 / done+detected 绿 / done+undetected 灰绿 / error 红
function expertLaneCls(ex) {
  if (ex.status === 'running') return 'lane-running';
  if (ex.status === 'done') return ex.detected ? 'lane-detected' : 'lane-clear';
  if (ex.status === 'error') return 'lane-error';
  return 'lane-queued';
}

const EXPERT_CATCH_RATE = 0.9;  // displayed 线性逼近 target 的恒定速率(fraction/秒)
const EXPERT_CREEP_RATE = 0.04; // 到达 target 且仍 running 时,向下个里程碑缓行的速率
const LANE_CELLS = 16;  // 每条泳道的像素列数(3×N 网格,方块间拉开间隔铺满卡宽)

// 分段像素条 HTML:cells 列,每列 3 个均等方块像素(从上到下堆叠,点亮顺序亦从上到下)
function pixelBarHtml(cells) {
  const cell = '<span class="pixel-cell">'
    + '<span class="pixel-sub"></span>'.repeat(3) + '</span>';
  return cell.repeat(cells);
}

// 按 displayed(0..1)点亮像素条:displayed×列数 = 整列数 + 列内小数;
// 整列 3 像素全亮,frontier 列按小数×3 从上到下点亮;
// 下一个待点亮像素加 .frontier 明暗脉冲(仅 opts.running 时)
function paintPixelBar(cells, displayed, opts) {
  const n = cells.length;
  if (!n) return;
  const pos = Math.max(0, Math.min(1, displayed)) * n;
  const full = Math.min(n, Math.floor(pos));
  const litInFrontier = Math.min(2, Math.floor((pos - full) * 3));
  const running = !!(opts && opts.running);
  for (let i = 0; i < n; i++) {
    const lit = i < full ? 3 : (i === full ? litInFrontier : 0);
    const subs = cells[i].children;
    for (let s = 0; s < subs.length; s++) {
      const frontier = running && i === full && full < n && s === lit;
      subs[s].classList.toggle('on', s < lit || frontier);
      subs[s].classList.toggle('frontier', frontier);
    }
  }
}

function mountExpertPanel(job) {
  const body = $('#experts-body');
  if (!body) return;
  const stem = job.stem;
  body.innerHTML =
    '<div class="expert-panel">'
    + '<div class="expert-head">'
    + '<span class="expert-step" id="exp-step"></span>'
    + '<button class="btn btn-ghost btn-sm stop-btn" id="exp-stop" title="停止推理">■ 停止推理</button>'
    + '</div>'
    + '<div class="expert-lanes" id="exp-lanes"></div>'
    + '</div>';
  $('#exp-stop').addEventListener('click', () => cancelJob(job.id));
  // 初次插入淡入(CSS opacity 0 → 1,transition 见 .expert-panel)
  const panelEl = body.firstElementChild;
  requestAnimationFrame(() => { if (panelEl) panelEl.style.opacity = '1'; });

  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let phases = null;
  loadExpertPhases().then(d => { phases = d; });

  const lanes = new Map(); // name -> {displayed, row, cells, phaseEl, cls}
  let laneSig = '';
  let lastT = performance.now();
  let rafId = 0;

  // 泳道 DOM 按专家名单签名重建(后端首次透出 experts 前可能为空)
  function syncLanes(list) {
    const sig = list.map(e => e.name).join('|');
    if (sig === laneSig) return;
    laneSig = sig;
    lanes.clear();
    const wrap = $('#exp-lanes');
    if (!wrap) return;
    wrap.innerHTML = list.length
      ? list.map(ex =>
          '<div class="expert-lane lane-queued' + (ex.name === '裁决' ? ' lane-judge' : '')
          + '" data-lane="' + esc(ex.name) + '">'
          + '<div class="lane-top">'
          + '<span class="lane-dot"></span>'
          + '<span class="expert-name" title="' + esc(ex.name) + '">' + esc(ex.name) + '</span>'
          + '<span class="expert-phase"></span>'
          + '</div>'
          + '<div class="pixel-bar">' + pixelBarHtml(LANE_CELLS) + '</div>'
          + '</div>').join('')
      : '<div class="empty-note">等待后端推送专家进度…</div>';
    list.forEach(ex => {
      const row = wrap.querySelector('[data-lane="' + CSS.escape(ex.name) + '"]');
      if (row) {
        lanes.set(ex.name, {
          displayed: 0, cls: 'lane-queued', row: row,
          judge: ex.name === '裁决',
          cells: Array.from(row.querySelector('.pixel-bar').children),
          phaseEl: row.querySelector('.expert-phase'),
        });
      }
    });
  }

  function stopLoop() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
  }

  function frame(now) {
    const dt = Math.min(0.1, (now - lastT) / 1000);
    lastT = now;
    const cur = latestJobForStem(stem); // 轮询写入的 state.jobs 是进度的唯一来源
    if (!cur || cur.status !== 'running' || !document.body.contains(body)) {
      stopLoop();
      // 失败(含用户停止):原地换成失败提示;done 由轮询触发的 selectVideo 重载为结果卡
      if (cur && cur.status === 'failed' && document.body.contains(body)) {
        body.innerHTML = '<div class="empty-note">推理已停止或失败,暂无分析结果。'
          + '可在左侧点击 ↻ 重试,或重新勾选后点击「开始推理」。</div>';
      }
      return;
    }
    const cp = cur.progress || {};
    const list = Array.isArray(cp.experts) ? cp.experts : [];
    syncLanes(list);
    list.forEach(ex => {
      const lane = lanes.get(ex.name);
      if (!lane) return;
      const target = ex.status === 'queued' ? 0
        : (typeof ex.fraction === 'number' ? ex.fraction : (ex.status === 'done' ? 1 : 0));
      let d = lane.displayed;
      if (ex.status === 'queued') d = 0;
      else if (reduced) d = target; // reduced-motion:无动画,直接到位
      else if (d < target) d = Math.min(target, d + EXPERT_CATCH_RATE * dt);
      else if (ex.status === 'running') {
        const cap = nextMilestone(phases, ex.name, d, target);
        if (d < cap) d = Math.min(cap, d + EXPERT_CREEP_RATE * dt);
      }
      lane.displayed = d;
      paintPixelBar(lane.cells, d, { running: ex.status === 'running' });
      lane.phaseEl.textContent = ex.label || '';
      lane.phaseEl.title = ex.label || '';
      const cls = expertLaneCls(ex);
      if (cls !== lane.cls) {
        lane.cls = cls;
        lane.row.className = 'expert-lane ' + cls + (lane.judge ? ' lane-judge' : '');
      }
    });
    const stepEl = $('#exp-step');
    if (stepEl) stepEl.textContent = jobStepText(cur);
    rafId = requestAnimationFrame(frame);
  }

  state.cleanups.push(stopLoop); // 主区重渲染前自动停帧
  rafId = requestAnimationFrame(frame);
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
