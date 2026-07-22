/* ==========================================================================
   高速交通事件分析台 — 前端(原生 JS,无框架无构建)
   消费契约 REST API;开发时追加 ?mock=1 使用内置模拟数据。
   ========================================================================== */
'use strict';

/* ---------------------------------------------------------------- 工具 */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
}

function toast(msg, kind) {
  const root = $('#toast-root');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4200);
}

class ApiError extends Error {
  constructor(status, detail) { super(detail || ('HTTP ' + status)); this.status = status; }
}

/* ------------------------------------------------------------ 全局状态 */
const MOCK = new URLSearchParams(location.search).get('mock') === '1';

const STEP_LABELS = { 1: '预处理', 2: '专家分析', 3: '裁决', 4: 'SFT 标注', 5: '报告生成' };

const state = {
  workspace: null,          // {path} | {path:null}
  videos: [],               // [{name, stem, size, mtime, has_results}]
  jobs: [],                 // [{id, kind, stem?, status, progress, log_tail, returncode?}]
  prevJobStatus: {},        // id -> status(用于完成转移检测)
  checked: new Set(),       // 勾选的视频 stem
  currentStem: null,
  results: null,            // 当前视频的 {report_md, sft_label, evidence}
  evidenceDraft: null,      // 编辑中的 evidence 深拷贝
  evidenceDirty: false,
  evTabIdx: 0,
  evalData: null,           // /api/evaluate/latest 返回
  batchIds: [],             // 最近一次批量推理的 job id
  cleanups: [],             // 主区重渲染前的清理函数
};

/* ------------------------------------------------------------ API 层 */
async function api(path, opts) {
  opts = opts || {};
  if (MOCK) return mockApi(path, opts);
  const init = { method: opts.method || 'GET' };
  if (opts.body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, '网络错误:' + e.message);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || JSON.stringify(j); } catch (e) { /* ignore */ }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

function frameUrl(stem, index) {
  if (MOCK) return mockFrameUrl(stem, index);
  return '/api/videos/' + encodeURIComponent(stem) + '/frame?index=' + index;
}

function imageUrl(stem, name) {
  if (MOCK) return mockImageUrl(stem, name);
  // name 形如 "images/xxx.jpg",取文件名部分
  const base = String(name).split('/').pop();
  return '/api/results/' + encodeURIComponent(stem) + '/images/' + encodeURIComponent(base);
}

/* ================================================================
   Mock 数据层(?mock=1)
   ================================================================ */
const mockDb = {
  workspace: { path: '/mock/workspace' },
  videos: [
    { name: '01-02_Event_101_1756000000000_1.mp4', stem: '01-02_Event_101_1756000000000_1', size: 8388608, mtime: 1756000100, has_results: true },
    { name: '03_Event_102_1756000001000_1.mp4', stem: '03_Event_102_1756000001000_1', size: 12582912, mtime: 1756000200, has_results: false },
    { name: '05-07_Event_129_1756000002000_1.mp4', stem: '05-07_Event_129_1756000002000_1', size: 6291456, mtime: 1756000300, has_results: false },
  ],
  jobs: [],
  nextJobId: 1,
  results: {},
  evalLatest: null,
  tickCount: 0,
};

const EVENT_NAMES_10 = ['违法停车', '应急车道占用', '交通事故', '高速公路行人出现', '摩托车出现',
  '拥堵', '道路施工', '车辆逆行/倒车', '抛洒物', '实线变道'];

function mockEvidence(stem) {
  const events = EVENT_NAMES_10.map((name, i) => ({
    event_id: i + 1,
    name: name,
    detected: i === 1,
    calibration: { frame_index: null, emergency_polygon_rel: null, chevron_polygon_rel: null },
    evidence_regions: [],
    gallery_images: [],
  }));
  const ev = events[1];
  ev.calibration = {
    frame_index: 4,
    emergency_polygon_rel: [[0.72, 0.35], [0.98, 0.42], [0.98, 0.95], [0.68, 0.92]],
    chevron_polygon_rel: [[0.05, 0.55], [0.22, 0.50], [0.30, 0.78], [0.10, 0.85]],
  };
  ev.evidence_regions = [
    { frame_index: 4, box_rel: [0.74, 0.48, 0.90, 0.66], label: '白色小车占用应急车道', image: 'images/zoom_ev2_1.jpg' },
    { frame_index: 6, box_rel: [0.40, 0.55, 0.52, 0.70], label: null, image: null },
  ];
  ev.gallery_images = ['images/comp_ev2_1.jpg', 'images/comp_ev2_2.jpg'];
  return {
    schema_version: 1,
    video: { file_name: stem + '.mp4', duration_sec: 15.0, fps: 25.0, width: 1920, height: 1080 },
    events: events,
  };
}

function mockSft(stem) {
  return {
    chunk: 'chunk #1', idx: 1, action: [2],
    description: '<think>\n【违法停车】未发现违法停车。画面中无相关迹象。\n【应急车道占用】应急车道区域:画面最右侧白色实线以外为应急车道,无导流区;占用应急车道车辆类型:一辆白色小车;位置:去向一侧应急车道内静止。\n【交通事故】未发现交通事故。画面中无相关迹象。\n</think>\n<answer>\n最终结论:本视频块检出以下事件。\nclass2: 应急车道占用\n天气:晴天\n时间:白天\n场景:高速公路双向主路场景,车流量中等。\n</answer>',
    start_timestamp: 0.0, end_timestamp: 15.0, chunk_name: stem + '.mp4',
  };
}

function mockReport() {
  return '# 交通事件分析报告\n\n**视频**: `demo.mp4`  ** chunk**: chunk #1\n\n' +
    '## 检测结论\n\n| 事件ID | 事件名称 | 是否检出 |\n|--------|----------|----------|\n' +
    '| 1 | 违法停车 | 否 |\n| 2 | 应急车道占用 | **是** |\n| 3 | 交通事故 | 否 |\n\n' +
    '## 证据\n\n- 帧 4:白色小车静止于应急车道\n- 帧 6:车辆仍未移动\n\n```\n最终结论: class2 应急车道占用\n```\n';
}

function mockResults(stem) {
  if (!mockDb.results[stem]) {
    mockDb.results[stem] = {
      report_md: mockReport(),
      sft_label: mockSft(stem),
      evidence: mockEvidence(stem),
    };
  }
  return mockDb.results[stem];
}

const mockFrameCache = {};
function mockFrameUrl(stem, index) {
  const key = stem + '#' + index;
  if (mockFrameCache[key]) return mockFrameCache[key];
  const c = document.createElement('canvas');
  c.width = 640; c.height = 360;
  const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 0, 360);
  grad.addColorStop(0, '#9db3c8'); grad.addColorStop(0.55, '#c8cfd4'); grad.addColorStop(0.56, '#5c665f'); grad.addColorStop(1, '#434b45');
  g.fillStyle = grad; g.fillRect(0, 0, 640, 360);
  g.strokeStyle = '#e8e2d5'; g.lineWidth = 3; g.setLineDash([24, 18]);
  g.beginPath(); g.moveTo(0, 300); g.lineTo(640, 260); g.stroke();
  g.setLineDash([]);
  g.fillStyle = 'rgba(42,38,32,0.75)';
  g.font = '16px monospace';
  g.fillText(stem.slice(0, 34), 14, 28);
  g.fillText('frame ' + index, 14, 50);
  const url = c.toDataURL('image/jpeg', 0.85);
  mockFrameCache[key] = url;
  return url;
}

function mockImageUrl(stem, name) {
  const key = stem + '/' + name;
  if (mockFrameCache[key]) return mockFrameCache[key];
  const c = document.createElement('canvas');
  c.width = 320; c.height = 180;
  const g = c.getContext('2d');
  g.fillStyle = '#6B6257'; g.fillRect(0, 0, 320, 180);
  g.fillStyle = '#F7F4EE'; g.font = '13px monospace';
  g.fillText(String(name).split('/').pop(), 12, 94);
  const url = c.toDataURL('image/jpeg', 0.85);
  mockFrameCache[key] = url;
  return url;
}

function mockTick() {
  mockDb.tickCount++;
  // 推理 job:串行推进
  let running = mockDb.jobs.find(j => j.status === 'running');
  if (!running) {
    const next = mockDb.jobs.find(j => j.status === 'queued');
    if (next) { next.status = 'running'; running = next; }
  }
  if (running) {
    if (running.kind === 'infer') {
      const step = (running.progress.step_index || 0) + 1;
      if (step > 5) {
        running.status = 'done';
        running.progress = { step_label: '完成', step_index: 5, total_steps: 5, fraction: 1 };
        running.returncode = 0;
        const v = mockDb.videos.find(v => v.stem === running.stem);
        if (v) v.has_results = true;
      } else {
        running.progress = {
          step_label: STEP_LABELS[step], step_index: step, total_steps: 5, fraction: step / 5,
        };
        running.log_tail = '[mock] [' + step + '/4] ' + STEP_LABELS[step] + '...';
      }
    } else {
      running._ticks = (running._ticks || 0) + 1;
      running.progress = { step_label: '评估中', step_index: 0, total_steps: 5, fraction: null };
      running.log_tail = '[mock] evaluate tick ' + running._ticks;
      if (running._ticks >= 4) {
        running.status = 'done';
        running.returncode = 0;
        mockDb.evalLatest = mockEvalMetrics();
      }
    }
  }
}

function mockEvalMetrics() {
  const per = {};
  EVENT_NAMES_10.forEach((name, i) => {
    const tp = i === 1 ? 3 : (i % 3);
    per[String(i + 1)] = {
      name: name, gt_count: tp + (i % 2), tp: tp, fp: i === 4 ? 1 : 0, fn: i % 2,
      precision: tp / (tp + (i === 4 ? 1 : 0)) || 0,
      recall: tp / (tp + (i % 2)) || 0,
      f1: 0,
    };
    const p = per[String(i + 1)];
    p.f1 = (p.precision + p.recall) > 0 ? 2 * p.precision * p.recall / (p.precision + p.recall) : 0;
    p.precision = +p.precision.toFixed(4); p.recall = +p.recall.toFixed(4); p.f1 = +p.f1.toFixed(4);
  });
  return {
    per_event: per,
    overall: {
      macro_precision: 0.82, macro_recall: 0.75, macro_f1: 0.7833,
      micro_precision: 0.85, micro_recall: 0.77, micro_f1: 0.808,
      total_tp: 12, total_fp: 2, total_fn: 4,
    },
  };
}

async function mockApi(path, opts) {
  await new Promise(r => setTimeout(r, 60)); // 模拟网络延迟
  const method = (opts && opts.method) || 'GET';
  const body = (opts && opts.body) || {};

  if (path === '/api/workspace' && method === 'GET') return mockDb.workspace;
  if (path === '/api/workspace' && method === 'POST') {
    if (!body.path || typeof body.path !== 'string') throw new ApiError(400, 'path 必填');
    mockDb.workspace = { path: body.path };
    return mockDb.workspace;
  }
  if (path === '/api/workspace/videos') return mockDb.workspace.path ? mockDb.videos : [];

  if (path === '/api/infer' && method === 'POST') {
    const stems = Array.isArray(body.stems) ? body.stems : [];
    if (!stems.length) throw new ApiError(400, 'stems 为空');
    const ids = stems.map(stem => {
      const job = {
        id: mockDb.nextJobId++, kind: 'infer', stem: stem, status: 'queued',
        progress: { step_label: '排队中', step_index: 0, total_steps: 5, fraction: 0 },
        log_tail: '',
      };
      mockDb.jobs.push(job);
      return job.id;
    });
    return { job_ids: ids };
  }

  if (path === '/api/evaluate' && method === 'POST') {
    if (!mockDb.videos.some(v => v.has_results)) throw new ApiError(400, '无分析结果,请先推理');
    const job = {
      id: mockDb.nextJobId++, kind: 'evaluate', status: 'queued',
      progress: { step_label: '排队中', step_index: 0, total_steps: 5, fraction: null },
      log_tail: '',
    };
    mockDb.jobs.push(job);
    return { job_id: job.id };
  }

  if (path === '/api/evaluate/latest') {
    if (!mockDb.evalLatest) throw new ApiError(404, '尚无评估结果');
    return mockDb.evalLatest;
  }

  if (path === '/api/jobs') return mockDb.jobs;

  let m = path.match(/^\/api\/results\/([^/]+)$/);
  if (m && method === 'GET') {
    const stem = decodeURIComponent(m[1]);
    const v = mockDb.videos.find(v => v.stem === stem);
    if (!v || !v.has_results) return { report_md: null, sft_label: null, evidence: null };
    return mockResults(stem);
  }

  m = path.match(/^\/api\/results\/([^/]+)\/evidence$/);
  if (m && method === 'PUT') {
    const stem = decodeURIComponent(m[1]);
    if (!body || !Array.isArray(body.events)) throw new ApiError(422, 'evidence 结构不合法');
    const r = mockResults(stem);
    const old = r.evidence;
    // 简易校验:事件数一致、不可改字段未变
    if (old && body.events.length !== old.events.length) throw new ApiError(422, 'events 数量不可变');
    r.evidence = body;
    return { ok: true };
  }

  throw new ApiError(404, 'mock: 未实现 ' + method + ' ' + path);
}

/* ================================================================
   像素进度条
   ================================================================ */
const PBAR_BLOCKS = 24;

function pbarHtml(fraction, running) {
  if (fraction == null) {
    // 不定进度:滑动亮块
    let blocks = '';
    for (let i = 0; i < PBAR_BLOCKS; i++) {
      blocks += '<i class="' + (running ? 'slide" style="animation-delay:' + (i * 45) + 'ms' : '') + '"></i>';
    }
    return '<div class="pbar indeterminate">' + blocks + '</div>';
  }
  const on = Math.round(Math.max(0, Math.min(1, fraction)) * PBAR_BLOCKS);
  let blocks = '';
  for (let i = 0; i < PBAR_BLOCKS; i++) {
    let cls = i < on ? 'on' : '';
    if (running && fraction > 0 && fraction < 1 && i === on) cls = 'on pulse';
    blocks += '<i' + (cls ? ' class="' + cls + '"' : '') + '></i>';
  }
  return '<div class="pbar">' + blocks + '</div>';
}

function jobStepText(job) {
  const p = job.progress || {};
  if (job.status === 'queued') return '排队中';
  if (job.status === 'done') return '完成';
  if (job.status === 'failed') return '失败 (rc=' + (job.returncode == null ? '?' : job.returncode) + ')';
  if (job.kind === 'evaluate') return esc(p.step_label || '评估中');
  const total = p.total_steps || 5;
  const idx = p.step_index || 0;
  if (!idx) return esc(p.step_label || '启动中');
  return esc(p.step_label || STEP_LABELS[idx] || '') + ' ' + idx + '/' + total;
}

function renderProgressDock() {
  const dock = $('#progress-dock');
  const active = state.jobs.filter(j => j.status === 'running' || j.status === 'queued');
  const batchJobs = state.batchIds.length
    ? state.jobs.filter(j => state.batchIds.includes(j.id)) : [];
  const batchActive = batchJobs.some(j => j.status === 'running' || j.status === 'queued');

  if (!active.length && !batchJobs.length) { dock.hidden = true; dock.innerHTML = ''; return; }
  dock.hidden = false;
  let html = '';

  if (batchJobs.length) {
    const done = batchJobs.filter(j => j.status === 'done' || j.status === 'failed').length;
    const frac = batchJobs.length ? done / batchJobs.length : 0;
    html += '<div class="prow"><span class="prow-label">批量推理</span>'
      + pbarHtml(frac, batchActive)
      + '<span class="prow-count">' + done + '/' + batchJobs.length + '</span></div>';
  }

  active.forEach(job => {
    if (job.status === 'queued' && job.kind === 'infer' && batchJobs.includes(job)) return; // 批量行已覆盖排队
    const p = job.progress || {};
    const label = job.kind === 'evaluate'
      ? '精度评估'
      : '<span class="stem">' + esc(job.stem || '') + '</span>';
    html += '<div class="prow"><span class="prow-label">' + label + ' · ' + jobStepText(job) + '</span>'
      + pbarHtml(p.fraction, job.status === 'running')
      + '</div>';
  });

  dock.innerHTML = html;
}

/* ================================================================
   侧栏
   ================================================================ */
function latestJobForStem(stem) {
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
function renderSidebar() {
  const list = $('#video-list');
  if (!state.workspace || !state.workspace.path) {
    list.innerHTML = '<div class="side-empty">设置工作区后列出视频</div>';
    return;
  }
  if (!state.videos.length) {
    list.innerHTML = '<div class="side-empty">工作区内没有视频文件</div>';
    return;
  }
  // 快照对比,避免每次轮询重建 DOM(防止打断勾选)
  const snap = JSON.stringify(state.videos.map(v => [v.stem, v.has_results, videoStatus(v).text,
    state.checked.has(v.stem), state.currentStem === v.stem]));
  if (snap === sidebarSnapshot) return;
  sidebarSnapshot = snap;

  list.innerHTML = state.videos.map(v => {
    const st = videoStatus(v);
    return '<div class="video-item' + (state.currentStem === v.stem ? ' active' : '') + '" data-stem="' + esc(v.stem) + '">'
      + '<input type="checkbox" data-check="' + esc(v.stem) + '"' + (state.checked.has(v.stem) ? ' checked' : '') + '>'
      + '<div class="video-meta"><div class="video-name" title="' + esc(v.name) + '">' + esc(v.name) + '</div>'
      + '<div class="video-sub">' + fmtBytes(v.size) + '</div></div>'
      + '<span class="badge ' + st.cls + '">' + st.text + '</span>'
      + '</div>';
  }).join('');

  $$('#video-list input[data-check]').forEach(cb => {
    cb.addEventListener('click', e => e.stopPropagation());
    cb.addEventListener('change', () => {
      if (cb.checked) state.checked.add(cb.dataset.check);
      else state.checked.delete(cb.dataset.check);
      syncButtons();
    });
  });
  $$('#video-list .video-item').forEach(item => {
    item.addEventListener('click', () => selectVideo(item.dataset.stem));
  });
  $('#check-all').checked = state.videos.length > 0 && state.videos.every(v => state.checked.has(v.stem));
}

function syncButtons() {
  const hasWs = !!(state.workspace && state.workspace.path);
  $('#btn-infer').disabled = !hasWs || state.checked.size === 0
    || state.jobs.some(j => j.kind === 'infer' && (j.status === 'running' || j.status === 'queued'));
  $('#btn-evaluate').disabled = !hasWs
    || state.jobs.some(j => j.kind === 'evaluate' && (j.status === 'running' || j.status === 'queued'));
}

/* ================================================================
   主区
   ================================================================ */
function runCleanups() {
  state.cleanups.forEach(fn => { try { fn(); } catch (e) { /* ignore */ } });
  state.cleanups = [];
}

function renderWelcome() {
  runCleanups();
  const main = $('#main');
  main.innerHTML =
    '<div class="welcome">'
    + '<div class="hero">'
    + '<h1>高速交通事件分析台</h1>'
    + (state.workspace && state.workspace.path
      ? '<p>当前工作区:<span class="hint-kbd">' + esc(state.workspace.path) + '</span></p>'
        + '<p>在左侧勾选视频后点击「开始推理」;点击视频名查看 SFT 标注、分析报告与可视化证据。</p>'
      : '<p>请先点击顶部「未设置工作区」按钮,选择包含视频文件的目录。</p>')
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

async function selectVideo(stem) {
  const v = state.videos.find(v => v.stem === stem);
  if (!v) return;
  if (!v.has_results) {
    const job = latestJobForStem(stem);
    toast(job && (job.status === 'running' || job.status === 'queued')
      ? '该视频正在推理队列中' : '该视频尚未推理,无结果可查看');
    return;
  }
  state.currentStem = stem;
  state.evTabIdx = 0;
  state.results = null;
  state.evidenceDraft = null;
  state.evidenceDirty = false;
  sidebarSnapshot = ''; renderSidebar();
  runCleanups();
  $('#main').innerHTML = skeletons();
  try {
    state.results = await api('/api/results/' + encodeURIComponent(stem));
    if (state.results.evidence) state.evidenceDraft = JSON.parse(JSON.stringify(state.results.evidence));
  } catch (e) {
    $('#main').innerHTML = '<div class="cards"><div class="card"><div class="card-body empty-note">加载结果失败:' + esc(e.message) + '</div></div></div>';
    return;
  }
  if (state.currentStem !== stem) return; // 期间切换了视频
  renderResults();
}

function renderResults() {
  runCleanups();
  const stem = state.currentStem;
  const r = state.results || {};
  const main = $('#main');
  main.innerHTML =
    '<div class="cards">'
    + '<div class="card" id="card-sft"><div class="card-head"><span class="card-title">SFT 标注详情</span>'
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
    + '<div class="card-body" id="ev-body"></div></div>'

    + '<div id="eval-card-slot"></div>'
    + '</div>';

  renderSftBody(r.sft_label);
  renderReportBody(r.report_md, stem);
  renderEvidenceCard(stem);
  renderEvalCard();
}

/* ------------------------------------------------------------ SFT 卡 */
function renderSftBody(sft) {
  const body = $('#sft-body');
  if (!body) return;
  if (!sft) { body.innerHTML = '<div class="empty-note">无 SFT 标注</div>'; return; }

  const meta = '<div class="sft-meta">'
    + '<span>' + esc(sft.chunk || '') + '</span>'
    + '<span>idx: ' + esc(sft.idx) + '</span>'
    + '<span>' + esc(sft.start_timestamp) + 's → ' + esc(sft.end_timestamp) + 's</span>'
    + '<span>' + esc(sft.chunk_name || '') + '</span>'
    + '</div>';

  const desc = String(sft.description || '');
  const thinkM = desc.match(/<think>([\s\S]*?)<\/think>/);
  const answerM = desc.match(/<answer>([\s\S]*?)<\/answer>/);

  let thinkHtml = '';
  if (thinkM) {
    // 按空行分段;段内换行保留。【事件名】加粗高亮。
    const paras = thinkM[1].trim().split(/\n\s*\n/).map(p =>
      p.split('\n').map(line =>
        esc(line).replace(/【([^】]+)】/g, '<span class="ev-name">【$1】</span>')
      ).join('<br>')
    );
    thinkHtml = '<div class="sft-section-title">思考过程(THINK)</div><div class="think-block"><p>'
      + paras.join('</p><p>') + '</p></div>';
  }

  let answerHtml = '';
  if (answerM) {
    const rows = answerM[1].trim().split('\n').filter(l => l.trim()).map(line => {
      const t = line.trim();
      let m = t.match(/^(天气|时间|场景|最终结论)\s*[:：]\s*(.*)$/);
      if (m) {
        return '<div class="answer-row' + (m[1] === '最终结论' ? ' conclusion' : '') + '">'
          + '<span class="answer-key">' + esc(m[1]) + '</span>'
          + '<span class="answer-val">' + esc(m[2]) + '</span></div>';
      }
      m = t.match(/^(class\d+)\s*[:：]\s*(.+)$/);
      if (m) {
        return '<div class="answer-row answer-class">'
          + '<span class="answer-key">' + esc(m[1]) + '</span>'
          + '<span class="answer-val cls-name">' + esc(m[2]) + '</span></div>';
      }
      return '<div class="answer-row"><span class="answer-val">' + esc(t) + '</span></div>';
    });
    answerHtml = '<div class="sft-section-title">答案(ANSWER)</div><div class="answer-block">'
      + rows.join('') + '</div>';
  }

  const actions = Array.isArray(sft.action) ? sft.action : [];
  const actionHtml = '<div class="sft-section-title">ACTION</div><div class="chips">'
    + (actions.length ? actions.map(a => '<span class="chip">' + esc(a) + '</span>').join('')
      : '<span class="empty-note">空</span>')
    + '</div>';

  body.innerHTML = meta + thinkHtml + answerHtml + actionHtml;
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

/* ------------------------------------------------------------ 证据编辑卡 */
function markDirty() {
  state.evidenceDirty = true;
  const f = $('#dirty-flag'); if (f) f.hidden = false;
  const s = $('#btn-ev-save'); if (s) s.disabled = false;
  const r = $('#btn-ev-reset'); if (r) r.disabled = false;
}

function clearDirty() {
  state.evidenceDirty = false;
  const f = $('#dirty-flag'); if (f) f.hidden = true;
  const s = $('#btn-ev-save'); if (s) s.disabled = true;
  const r = $('#btn-ev-reset'); if (r) r.disabled = true;
}

function renderEvidenceCard(stem) {
  const tabs = $('#ev-tabs');
  const body = $('#ev-body');
  if (!tabs || !body) return;
  const draft = state.evidenceDraft;
  if (!draft || !Array.isArray(draft.events)) {
    tabs.innerHTML = '';
    body.innerHTML = '<div class="empty-note">无证据数据</div>';
    return;
  }
  if (state.evTabIdx >= draft.events.length) state.evTabIdx = 0;

  tabs.innerHTML = draft.events.map((ev, i) =>
    '<button class="ev-tab' + (i === state.evTabIdx ? ' active' : '') + '" data-tab="' + i + '">'
    + '<span class="dot' + (ev.detected ? ' detected' : '') + '"></span>'
    + esc(ev.event_id) + ' ' + esc(ev.name) + '</button>'
  ).join('');
  $$('#ev-tabs .ev-tab').forEach(btn => btn.addEventListener('click', () => {
    state.evTabIdx = +btn.dataset.tab;
    renderEvidenceCard(stem);
  }));

  const saveBtn = $('#btn-ev-save');
  const resetBtn = $('#btn-ev-reset');
  if (saveBtn) saveBtn.onclick = saveEvidence;
  if (resetBtn) resetBtn.onclick = resetEvidence;

  mountEvidencePane(body, stem, draft.events[state.evTabIdx], draft.video || {});
}

async function saveEvidence() {
  const stem = state.currentStem;
  if (!stem || !state.evidenceDraft) return;
  const btn = $('#btn-ev-save');
  if (btn) btn.disabled = true;
  try {
    await api('/api/results/' + encodeURIComponent(stem) + '/evidence', {
      method: 'PUT', body: state.evidenceDraft,
    });
    state.results.evidence = JSON.parse(JSON.stringify(state.evidenceDraft));
    clearDirty();
    toast('证据已保存', 'ok');
  } catch (e) {
    if (btn) btn.disabled = false;
    toast('保存失败(' + e.status + '):' + e.message, 'err');
  }
}

async function resetEvidence() {
  const stem = state.currentStem;
  if (!stem) return;
  try {
    const r = await api('/api/results/' + encodeURIComponent(stem));
    if (r && r.evidence) {
      state.results.evidence = r.evidence;
      state.evidenceDraft = JSON.parse(JSON.stringify(r.evidence));
    }
    clearDirty();
    renderEvidenceCard(stem);
    toast('已重置为磁盘版本');
  } catch (e) {
    toast('重置失败:' + e.message, 'err');
  }
}

/* ---------------------------------------------------- 证据画布编辑器 */
const COLOR_EMERGENCY = '#D97757';
const COLOR_CHEVRON = '#3E7CB1';
const COLOR_BOX = '#7A9B76';
const HIT_R = 8;

function mountEvidencePane(mount, stem, ev, videoInfo) {
  mount.innerHTML = '';
  const pane = document.createElement('div');
  pane.className = 'ev-pane';
  mount.appendChild(pane);

  const calib = ev.calibration || {};
  const regions = Array.isArray(ev.evidence_regions) ? ev.evidence_regions : [];
  const gallery = Array.isArray(ev.gallery_images) ? ev.gallery_images : [];
  const hasGeom = !!(calib.emergency_polygon_rel || calib.chevron_polygon_rel
    || regions.some(r => r && r.box_rel));

  const maxFrame = Math.max(0, Math.round((videoInfo.duration_sec || 0) * (videoInfo.fps || 0)) - 1);
  let frameIdx = (calib.frame_index != null) ? calib.frame_index
    : (regions.find(r => r && r.frame_index != null) || {}).frame_index;
  if (frameIdx == null) frameIdx = 0;

  // 画廊图片(region 缩放图 + gallery)
  const galleryImgs = [];
  regions.forEach(r => { if (r && r.image) galleryImgs.push(r.image); });
  gallery.forEach(g => galleryImgs.push(g));

  if (!hasGeom && !galleryImgs.length) {
    pane.innerHTML = '<div class="ev-empty">该事件无可视化证据(未检出或无坐标数据)</div>';
    return;
  }

  const toolbar = document.createElement('div');
  toolbar.className = 'ev-toolbar';
  toolbar.innerHTML =
    '<button class="btn btn-ghost btn-sm" data-a="prev">◀ 上一帧</button>'
    + '<span>帧 <input class="ev-frame-input" type="number" min="0" max="' + maxFrame + '" value="' + frameIdx + '">'
    + (maxFrame ? ' / ' + maxFrame : '') + '</span>'
    + '<button class="btn btn-ghost btn-sm" data-a="next">下一帧 ▶</button>'
    + '<span class="ev-legend">'
    + '<span><i class="sw sw-emergency"></i>应急车道</span>'
    + '<span><i class="sw sw-chevron"></i>导流区</span>'
    + '<span><i class="sw sw-box"></i>证据框</span>'
    + '</span>';
  pane.appendChild(toolbar);

  const stage = document.createElement('div');
  stage.className = 'ev-stage';
  const img = document.createElement('img');
  img.className = 'ev-img';
  img.alt = '帧图';
  const canvas = document.createElement('canvas');
  canvas.className = 'ev-canvas';
  stage.appendChild(img);
  stage.appendChild(canvas);
  pane.appendChild(stage);

  const labelbar = document.createElement('div');
  labelbar.className = 'ev-labelbar';
  labelbar.hidden = true;
  labelbar.innerHTML = '<span>证据框标签</span>'
    + '<input class="ev-label-input" type="text">'
    + '<button class="btn btn-ghost btn-sm" data-a="deselect">取消选择</button>';
  pane.appendChild(labelbar);
  const labelInput = $('.ev-label-input', labelbar);

  if (galleryImgs.length) {
    const gal = document.createElement('div');
    gal.className = 'ev-gallery';
    galleryImgs.forEach(name => {
      const t = document.createElement('img');
      t.src = imageUrl(stem, name);
      t.alt = name;
      t.loading = 'lazy';
      t.title = name;
      t.addEventListener('click', () => window.open(t.src, '_blank'));
      gal.appendChild(t);
    });
    pane.appendChild(gal);
  }

  /* ---- 形状(直接引用 draft 数据,拖拽即改 draft) ---- */
  const shapes = [];
  if (Array.isArray(calib.emergency_polygon_rel)) {
    shapes.push({ type: 'poly', kind: 'emergency', color: COLOR_EMERGENCY, pts: calib.emergency_polygon_rel });
  }
  if (Array.isArray(calib.chevron_polygon_rel)) {
    shapes.push({ type: 'poly', kind: 'chevron', color: COLOR_CHEVRON, pts: calib.chevron_polygon_rel });
  }
  regions.forEach(r => {
    if (r && Array.isArray(r.box_rel)) shapes.push({ type: 'box', color: COLOR_BOX, region: r });
  });

  const ctx = canvas.getContext('2d');
  let W = 0, H = 0;      // CSS 像素尺寸
  let hover = null;      // {shape, kind:'vertex'|'corner'|'body', idx}
  let drag = null;       // {shape, kind, idx, moved}
  let selectedBox = null;

  const nx = v => v * W, ny = v => v * H;
  const clamp01 = v => Math.max(0, Math.min(1, v));

  function fit() {
    const w = img.clientWidth, h = img.clientHeight;
    if (!w || !h) return;
    W = w; H = h;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function boxCorners(b) {
    return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]];
  }

  function drawHandle(x, y, color, big) {
    const s = big ? 11 : 7;
    ctx.fillStyle = '#FFFFFF';
    ctx.fillRect(x - s / 2 - 1, y - s / 2 - 1, s + 2, s + 2);
    ctx.fillStyle = color;
    ctx.fillRect(x - s / 2, y - s / 2, s, s);
  }

  function draw() {
    if (!W || !H) return;
    ctx.clearRect(0, 0, W, H);
    shapes.forEach(sh => {
      const isHoverShape = hover && hover.shape === sh;
      const lw = isHoverShape ? 2.5 : 2;
      ctx.lineWidth = lw;
      ctx.strokeStyle = sh.color;
      ctx.fillStyle = sh.color + '22';
      if (sh.type === 'poly') {
        const pts = sh.pts;
        if (!pts.length) return;
        ctx.beginPath();
        ctx.moveTo(nx(pts[0][0]), ny(pts[0][1]));
        for (let i = 1; i < pts.length; i++) ctx.lineTo(nx(pts[i][0]), ny(pts[i][1]));
        ctx.closePath();
        ctx.fill(); ctx.stroke();
        pts.forEach((p, i) => {
          const big = hover && hover.shape === sh && hover.kind === 'vertex' && hover.idx === i;
          drawHandle(nx(p[0]), ny(p[1]), sh.color, big);
        });
      } else {
        const b = sh.region.box_rel;
        const x = nx(b[0]), y = ny(b[1]), w = nx(b[2]) - x, h = ny(b[3]) - y;
        if (sh === selectedBox) {
          ctx.save();
          ctx.setLineDash([6, 4]);
          ctx.lineWidth = 2.5;
          ctx.strokeRect(x, y, w, h);
          ctx.restore();
        } else {
          ctx.strokeRect(x, y, w, h);
        }
        ctx.fillRect(x, y, w, h);
        boxCorners(b).forEach((c, i) => {
          const big = hover && hover.shape === sh && hover.kind === 'corner' && hover.idx === i;
          drawHandle(nx(c[0]), ny(c[1]), sh.color, big);
        });
        if (sh.region.label) {
          ctx.font = '12px sans-serif';
          const tw = ctx.measureText(sh.region.label).width;
          const ly = Math.max(14, y - 6);
          ctx.fillStyle = sh.color;
          ctx.fillRect(x, ly - 13, tw + 10, 16);
          ctx.fillStyle = '#FFFFFF';
          ctx.fillText(sh.region.label, x + 5, ly - 1);
        }
      }
    });
  }

  function posOf(e) {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function pointInPoly(px, py, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const xi = nx(pts[i][0]), yi = ny(pts[i][1]);
      const xj = nx(pts[j][0]), yj = ny(pts[j][1]);
      if ((yi > py) !== (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function hitTest(p) {
    // 顶点/角点优先
    for (const sh of shapes) {
      if (sh.type === 'poly') {
        for (let i = 0; i < sh.pts.length; i++) {
          if (Math.abs(nx(sh.pts[i][0]) - p.x) <= HIT_R && Math.abs(ny(sh.pts[i][1]) - p.y) <= HIT_R) {
            return { shape: sh, kind: 'vertex', idx: i };
          }
        }
      } else {
        const corners = boxCorners(sh.region.box_rel);
        for (let i = 0; i < 4; i++) {
          if (Math.abs(nx(corners[i][0]) - p.x) <= HIT_R && Math.abs(ny(corners[i][1]) - p.y) <= HIT_R) {
            return { shape: sh, kind: 'corner', idx: i };
          }
        }
      }
    }
    // 身体
    for (let i = shapes.length - 1; i >= 0; i--) {
      const sh = shapes[i];
      if (sh.type === 'poly') {
        if (pointInPoly(p.x, p.y, sh.pts)) return { shape: sh, kind: 'body', idx: -1 };
      } else {
        const b = sh.region.box_rel;
        if (p.x >= nx(b[0]) && p.x <= nx(b[2]) && p.y >= ny(b[1]) && p.y <= ny(b[3])) {
          return { shape: sh, kind: 'body', idx: -1 };
        }
      }
    }
    return null;
  }

  function applyDrag(p) {
    const fx = clamp01(p.x / W), fy = clamp01(p.y / H);
    const sh = drag.shape;
    if (drag.kind === 'vertex') {
      sh.pts[drag.idx][0] = fx;
      sh.pts[drag.idx][1] = fy;
    } else if (drag.kind === 'corner') {
      const b = sh.region.box_rel;
      // corners 顺序: 0左上 1右上 2右下 3左下;拖拽后规范化
      const c = boxCorners(b);
      c[drag.idx] = [fx, fy];
      const xs = c.map(q => q[0]), ys = c.map(q => q[1]);
      b[0] = Math.min(...xs); b[1] = Math.min(...ys);
      b[2] = Math.max(...xs); b[3] = Math.max(...ys);
    } else { // body
      const dx = fx - drag.last[0], dy = fy - drag.last[1];
      if (sh.type === 'poly') {
        sh.pts.forEach(pt => { pt[0] = clamp01(pt[0] + dx); pt[1] = clamp01(pt[1] + dy); });
      } else {
        const b = sh.region.box_rel;
        const w = b[2] - b[0], h = b[3] - b[1];
        b[0] = clamp01(b[0] + dx); b[1] = clamp01(b[1] + dy);
        b[2] = clamp01(b[0] + w); b[3] = clamp01(b[1] + h);
      }
      drag.last = [fx, fy];
    }
    draw();
  }

  function selectBox(sh) {
    selectedBox = sh;
    if (sh) {
      labelbar.hidden = false;
      labelInput.value = sh.region.label || '';
    } else {
      labelbar.hidden = true;
    }
    draw();
  }

  function onDown(e) {
    const p = posOf(e);
    const hit = hitTest(p);
    if (hit) {
      drag = { shape: hit.shape, kind: hit.kind, idx: hit.idx, moved: false,
               last: [clamp01(p.x / W), clamp01(p.y / H)] };
      e.preventDefault();
    } else if (selectedBox) {
      selectBox(null);
    }
  }

  function onMove(e) {
    const p = posOf(e);
    if (drag) {
      drag.moved = true;
      applyDrag(p);
      return;
    }
    const hit = hitTest(p);
    const changed = JSON.stringify(hit && { k: hit.kind, i: hit.idx, s: shapes.indexOf(hit.shape) })
      !== JSON.stringify(hover && { k: hover.kind, i: hover.idx, s: shapes.indexOf(hover.shape) });
    hover = hit;
    canvas.style.cursor = hit ? (hit.kind === 'body' ? 'move' : 'pointer') : 'crosshair';
    if (changed) draw();
  }

  function onUp() {
    if (drag) {
      if (drag.moved) markDirty();
      else if (drag.shape.type === 'box') selectBox(drag.shape);
      drag = null;
    }
  }

  canvas.addEventListener('mousedown', onDown);
  canvas.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  canvas.addEventListener('mouseleave', () => { if (!drag) { hover = null; draw(); } });

  labelInput.addEventListener('input', () => {
    if (selectedBox) { selectedBox.region.label = labelInput.value; markDirty(); draw(); }
  });
  $('[data-a="deselect"]', labelbar).addEventListener('click', () => selectBox(null));

  function setFrame(idx) {
    frameIdx = Math.max(0, Math.min(maxFrame || idx, idx));
    const input = $('.ev-frame-input', toolbar);
    if (input) input.value = frameIdx;
    img.src = frameUrl(stem, frameIdx);
  }

  $('[data-a="prev"]', toolbar).addEventListener('click', () => setFrame(frameIdx - 1));
  $('[data-a="next"]', toolbar).addEventListener('click', () => setFrame(frameIdx + 1));
  $('.ev-frame-input', toolbar).addEventListener('change', e => setFrame(+e.target.value || 0));

  const onResize = () => fit();
  window.addEventListener('resize', onResize);
  img.addEventListener('load', fit);
  img.src = frameUrl(stem, frameIdx);

  state.cleanups.push(() => {
    window.removeEventListener('mouseup', onUp);
    window.removeEventListener('resize', onResize);
  });
}

/* ------------------------------------------------------------ 精度评估卡 */
function renderEvalCard() {
  const slot = $('#eval-card-slot');
  if (!slot) return;
  const evalJob = [...state.jobs].reverse().find(j => j.kind === 'evaluate');
  const running = evalJob && (evalJob.status === 'running' || evalJob.status === 'queued');

  let inner = '<div class="card" id="card-eval"><div class="card-head">'
    + '<span class="card-title">精度评估</span>'
    + '<span class="card-sub">基于文件名真值(--gt-mode filename)</span>'
    + '<span class="spacer"></span>'
    + '<button class="btn btn-primary btn-sm" id="btn-eval-run"'
    + (running || !state.workspace || !state.workspace.path ? ' disabled' : '') + '>'
    + (running ? '评估中…' : '运行评估') + '</button></div>'
    + '<div class="card-body" id="eval-body"></div></div>';
  slot.innerHTML = inner;

  const btn = $('#btn-eval-run');
  if (btn) btn.addEventListener('click', runEvaluate);

  const body = $('#eval-body');
  if (running) {
    const p = evalJob.progress || {};
    body.innerHTML = '<div class="prow"><span class="prow-label">' + jobStepText(evalJob) + '</span>'
      + pbarHtml(p.fraction, true) + '</div>'
      + (evalJob.log_tail ? '<pre class="md" style="margin-top:10px"><code>' + esc(evalJob.log_tail) + '</code></pre>' : '');
    return;
  }
  if (!state.evalData) {
    body.innerHTML = '<div class="empty-note">尚无评估结果。完成推理后点击「运行评估」。</div>';
    return;
  }
  body.innerHTML = evalTableHtml(state.evalData);
}

function evalTableHtml(data) {
  const per = data.per_event || {};
  const overall = data.overall || {};
  const keys = Object.keys(per).sort((a, b) => {
    if (a === 'normal') return 1;
    if (b === 'normal') return -1;
    return Number(a) - Number(b);
  });
  let html = '<div class="eval-table-wrap"><table class="eval-table">'
    + '<thead><tr><th>事件ID</th><th>事件名称</th><th>GT数</th><th>TP</th><th>FP</th><th>FN</th>'
    + '<th>精确率</th><th>召回率</th><th>F1</th></tr></thead><tbody>';
  keys.forEach(k => {
    const e = per[k] || {};
    const name = k === 'normal' ? '正常(无事件)' : (e.name || '事件' + k);
    const id = k === 'normal' ? '-' : k;
    html += '<tr><td>' + esc(id) + '</td><td>' + esc(name) + '</td>'
      + '<td class="num">' + esc(e.gt_count != null ? e.gt_count : (e.total != null ? e.total : '-')) + '</td>'
      + '<td class="num">' + esc(e.tp != null ? e.tp : '-') + '</td>'
      + '<td class="num">' + esc(e.fp != null ? e.fp : '-') + '</td>'
      + '<td class="num">' + esc(e.fn != null ? e.fn : '-') + '</td>'
      + '<td class="num">' + esc(e.precision != null ? e.precision : '-') + '</td>'
      + '<td class="num">' + esc(e.recall != null ? e.recall : '-') + '</td>'
      + '<td class="num">' + esc(e.f1 != null ? e.f1 : '-') + '</td></tr>';
  });
  html += '<tr class="total"><td>-</td><td>总体(宏平均 / 微平均)</td>'
    + '<td class="num">-</td>'
    + '<td class="num">' + esc(overall.total_tp != null ? overall.total_tp : '-') + '</td>'
    + '<td class="num">' + esc(overall.total_fp != null ? overall.total_fp : '-') + '</td>'
    + '<td class="num">' + esc(overall.total_fn != null ? overall.total_fn : '-') + '</td>'
    + '<td class="num">' + esc(overall.macro_precision != null ? overall.macro_precision : '-') + ' / '
    + esc(overall.micro_precision != null ? overall.micro_precision : '-') + '</td>'
    + '<td class="num">' + esc(overall.macro_recall != null ? overall.macro_recall : '-') + ' / '
    + esc(overall.micro_recall != null ? overall.micro_recall : '-') + '</td>'
    + '<td class="num">' + esc(overall.macro_f1 != null ? overall.macro_f1 : '-') + ' / '
    + esc(overall.micro_f1 != null ? overall.micro_f1 : '-') + '</td></tr>';
  html += '</tbody></table></div>';
  return html;
}

async function runEvaluate() {
  try {
    await api('/api/evaluate', { method: 'POST', body: {} });
    toast('评估任务已提交');
    pollJobs();
  } catch (e) {
    toast('评估提交失败(' + e.status + '):' + e.message, 'err');
  }
  renderEvalCard();
  syncButtons();
}

async function loadEvalLatest() {
  try {
    state.evalData = await api('/api/evaluate/latest');
  } catch (e) {
    if (e.status !== 404) toast('读取评估结果失败:' + e.message, 'err');
    state.evalData = null;
  }
  renderEvalCard();
}

/* ================================================================
   动作:工作区 / 推理
   ================================================================ */
async function setWorkspace(path) {
  try {
    state.workspace = await api('/api/workspace', { method: 'POST', body: { path: path } });
    $('#ws-path').textContent = state.workspace.path;
    $('#ws-panel').hidden = true;
    $('#ws-error').hidden = true;
    state.currentStem = null;
    state.checked.clear();
    state.evalData = null;
    await Promise.all([loadVideos(), loadEvalLatest()]);
    renderWelcome();
    renderSidebar();
    syncButtons();
  } catch (e) {
    const err = $('#ws-error');
    err.textContent = '设置失败(' + e.status + '):' + e.message;
    err.hidden = false;
  }
}

async function loadVideos() {
  try {
    state.videos = await api('/api/workspace/videos');
  } catch (e) {
    state.videos = [];
  }
  renderSidebar();
}

async function startInfer() {
  const stems = state.videos.filter(v => state.checked.has(v.stem)).map(v => v.stem);
  if (!stems.length) return;
  try {
    const r = await api('/api/infer', { method: 'POST', body: { stems: stems } });
    state.batchIds = r.job_ids || [];
    toast('已提交 ' + stems.length + ' 个推理任务');
    pollJobs();
  } catch (e) {
    toast('推理提交失败(' + e.status + '):' + e.message, 'err');
  }
  syncButtons();
}

/* ================================================================
   轮询
   ================================================================ */
let polling = false;
async function pollJobs() {
  if (polling) return;
  polling = true;
  try {
    const jobs = await api('/api/jobs');
    const prev = state.prevJobStatus;
    state.jobs = jobs;
    const next = {};
    let needVideos = false;
    let reloadStem = null;
    jobs.forEach(j => {
      next[j.id] = j.status;
      const was = prev[j.id];
      if (was && was !== j.status && (j.status === 'done' || j.status === 'failed')) {
        needVideos = true;
        if (j.kind === 'evaluate' && j.status === 'done') loadEvalLatest();
        if (j.kind === 'evaluate' && j.status === 'failed') {
          toast('评估失败(rc=' + j.returncode + ')', 'err'); renderEvalCard();
        }
        if (j.kind === 'infer' && j.status === 'done' && j.stem === state.currentStem) {
          reloadStem = j.stem; // 刷新列表后再重载当前视频结果
        }
        if (j.kind === 'infer' && j.status === 'failed') {
          toast('推理失败:' + (j.stem || '') + ' (rc=' + j.returncode + ')', 'err');
        }
      }
      if (j.kind === 'evaluate' && (j.status === 'running' || j.status === 'queued')) {
        renderEvalCard(); // 更新评估卡进度
      }
    });
    state.prevJobStatus = next;
    renderProgressDock();
    renderSidebar();
    syncButtons();
    if (needVideos) await loadVideos();
    if (reloadStem && reloadStem === state.currentStem) selectVideo(reloadStem);
  } catch (e) {
    // 后端未启动等场景:静默,下个周期重试
  } finally {
    polling = false;
  }
}

/* ================================================================
   初始化
   ================================================================ */
function initToolbar() {
  if (MOCK) $('#mock-badge').hidden = false;

  const panel = $('#ws-panel');
  $('#btn-workspace').addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      $('#ws-input').value = (state.workspace && state.workspace.path) || '';
      $('#ws-input').focus();
    }
  });
  $('#ws-cancel').addEventListener('click', () => { panel.hidden = true; });
  $('#ws-ok').addEventListener('click', () => {
    const p = $('#ws-input').value.trim();
    if (p) setWorkspace(p);
  });
  $('#ws-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { const p = e.target.value.trim(); if (p) setWorkspace(p); }
    if (e.key === 'Escape') panel.hidden = true;
  });
  document.addEventListener('click', e => {
    if (!panel.hidden && !panel.contains(e.target) && !$('#btn-workspace').contains(e.target)) {
      panel.hidden = true;
    }
  });

  $('#btn-infer').addEventListener('click', startInfer);
  $('#btn-evaluate').addEventListener('click', runEvaluate);

  $('#check-all').addEventListener('change', e => {
    state.checked.clear();
    if (e.target.checked) state.videos.forEach(v => state.checked.add(v.stem));
    sidebarSnapshot = '';
    renderSidebar();
    syncButtons();
  });
}

async function init() {
  initToolbar();
  if (MOCK) setInterval(mockTick, 700);

  try {
    state.workspace = await api('/api/workspace');
    if (state.workspace && state.workspace.path) {
      $('#ws-path').textContent = state.workspace.path;
      await Promise.all([loadVideos(), loadEvalLatest(), pollJobs()]);
    }
  } catch (e) {
    // 后端未就绪:保持初始界面
  }
  renderWelcome();
  renderSidebar();
  syncButtons();
  setInterval(pollJobs, 1500);
}

init();
