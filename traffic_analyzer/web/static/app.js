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
  evalData: null,           // /api/evaluate/latest 返回
  eventConfig: null,        // /api/config/events 缓存([{event_id, name_zh, is_active}])
  sftDraft: null,           // SFT 编辑草稿 {texts, checks, unmatched, env}
  sftSavedSig: '',          // 已保存草稿的签名(用于 dirty 判断)
  cleanups: [],             // 主区重渲染前的清理函数
  tree: { loaded: false, root: [], children: {}, expanded: new Set() }, // 侧栏文件树
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
    try {
      const j = await res.json();
      // FastAPI 422 的 detail 是 [{loc, msg, ...}] 数组,拼成「字段路径: 消息」
      detail = Array.isArray(j.detail)
        ? j.detail.map(d => (Array.isArray(d.loc) ? d.loc.join('.') : String(d.loc || '')) + ': ' + d.msg).join('; ')
        : (j.detail || JSON.stringify(j));
    } catch (e) { /* ignore */ }
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

// 当前视频的媒体来源:顶层视频用 {stem}(走 /api/videos/... 端点),
// 嵌套视频用 {stem, rel}(走 /api/workspace/... 端点;stem 仍用于结果图片)
function videoSource(v) {
  return v.rel && v.rel.indexOf('/') >= 0 ? { stem: v.stem, rel: v.rel } : { stem: v.stem };
}

function metaUrl(source) {
  if (source.rel != null) return '/api/workspace/meta?path=' + encodeURIComponent(source.rel);
  return '/api/videos/' + encodeURIComponent(source.stem) + '/meta';
}

function sourceFrameUrl(source, index) {
  if (source.rel != null) {
    return '/api/workspace/frame?path=' + encodeURIComponent(source.rel) + '&index=' + index;
  }
  return frameUrl(source.stem, index);
}

function imageUrl(stem, name) {
  if (MOCK) return mockImageUrl(stem, name);
  // name 为相对 analysis/<stem>/ 的路径(report.md 的 "tmp_img/.../x.jpg" 或
  // evidence.json 的 "images/x.jpg"),按原路径请求,不再降级为 basename。
  return '/api/results/' + encodeURIComponent(stem) + '/file?path=' + encodeURIComponent(name);
}

/* ================================================================
   Mock 数据层(?mock=1)
   ================================================================ */
const mockDb = {
  workspace: { path: '/mock/workspace' },
  videos: [
    { name: '01-02_Event_101_1756000000000_1.mp4', stem: '01-02_Event_101_1756000000000_1', rel: '01-02_Event_101_1756000000000_1.mp4', size: 8388608, mtime: 1756000100, has_results: true },
    { name: '03_Event_102_1756000001000_1.mp4', stem: '03_Event_102_1756000001000_1', rel: '03_Event_102_1756000001000_1.mp4', size: 12582912, mtime: 1756000200, has_results: false },
    { name: '05-07_Event_129_1756000002000_1.mp4', stem: '05-07_Event_129_1756000002000_1', rel: '05-07_Event_129_1756000002000_1.mp4', size: 6291456, mtime: 1756000300, has_results: false },
  ],
  jobs: [],
  nextJobId: 1,
  results: {},
  evalLatest: null,
  tickCount: 0,
};

// 目录弹窗用的模拟文件系统(?mock=1)
const mockFsTree = {
  '/': ['home', 'media', 'mock'],
  '/home': ['wanji'],
  '/home/wanji': ['projects', 'Videos'],
  '/home/wanji/projects': [],
  '/home/wanji/Videos': [],
  '/media': ['usb'],
  '/media/usb': [],
  '/mock': ['datasets', 'empty', 'workspace'],
  '/mock/datasets': ['labeled', 'raw'],
  '/mock/datasets/labeled': [],
  '/mock/datasets/raw': [],
  '/mock/empty': [],
  '/mock/workspace': ['analysis'],
  '/mock/workspace/analysis': [],
};

// 侧栏文件树的模拟数据(?mock=1);根目录的顶层视频由 mockDb.videos 动态生成,
// 以便模拟推理完成后 has_results 同步变化
const mockWsDirs = {
  'clips': [
    { name: 'nested_clip.mp4', rel: 'clips/nested_clip.mp4', type: 'file', is_video: true, size: 1048576, mtime: 1756000500, has_results: false },
    { name: 'readme.txt', rel: 'clips/readme.txt', type: 'file', is_video: false, size: 512, mtime: 1756000600 },
  ],
  'analysis': [],
};

function mockTreeEntries(rel) {
  if (rel === '') {
    return [
      { name: 'analysis', rel: 'analysis', type: 'dir' },
      { name: 'clips', rel: 'clips', type: 'dir' },
    ].concat(mockDb.videos.map(v => ({
      name: v.name, rel: v.name, type: 'file', is_video: true, stem: v.stem,
      size: v.size, mtime: v.mtime, has_results: v.has_results,
    }))).concat([
      { name: '说明.md', rel: '说明.md', type: 'file', is_video: false, size: 2048, mtime: 1756000400 },
    ]);
  }
  return mockWsDirs[rel];
}

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

// 与 traffic_analyzer/config/event_categories.yaml 一致(0-7 激活,8/9 未激活)
const MOCK_EVENT_CONFIG = EVENT_NAMES_10.map((name, i) => ({
  event_id: i, name_zh: name, is_active: i < 8,
}));

function mockSft(stem) {
  return {
    chunk: 'chunk #1', idx: 1, action: [2],
    description: '<think>\n违法停车：未发现违法停车。画面中无相关迹象。\n\n'
      + '应急车道占用：应急车道区域为画面最右侧白色实线以外;一辆白色小车静止于去向一侧应急车道内。\n\n'
      + '交通事故：未发现交通事故。画面中无相关迹象。\n</think>\n'
      + '<answer>\n天气：晴天\n时间：白天\n场景：高速公路双向主路场景,车流量中等。\n'
      + '最终结论：本视频块检出以下事件。\nclass2: 应急车道占用\n</answer>',
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
  const fsMatch = path.match(/^\/api\/fs\/list(?:\?(.*))?$/);
  if (fsMatch && method === 'GET') {
    const q = new URLSearchParams(fsMatch[1] || '');
    const p = q.get('path') || (mockDb.workspace && mockDb.workspace.path) || '/mock';
    const names = mockFsTree[p];
    if (!names) throw new ApiError(404, 'Not a directory: ' + p);
    return {
      path: p,
      parent: p === '/' ? null : (p.slice(0, p.lastIndexOf('/')) || '/'),
      dirs: names.map(n => ({ name: n, path: (p === '/' ? '' : p) + '/' + n })),
    };
  }
  if (path === '/api/workspace/videos') {
    // 递归列表:顶层视频 + clips/ 下的嵌套视频(rel 作为唯一键)
    return mockDb.workspace.path ? mockDb.videos.concat([
      { name: 'nested_clip.mp4', stem: 'nested_clip', rel: 'clips/nested_clip.mp4', size: 1048576, mtime: 1756000500, has_results: false },
    ]) : [];
  }

  const treeMatch = path.match(/^\/api\/workspace\/tree(?:\?(.*))?$/);
  if (treeMatch && method === 'GET') {
    if (!mockDb.workspace.path) throw new ApiError(400, 'No workspace selected');
    const rel = new URLSearchParams(treeMatch[1] || '').get('path') || '';
    const entries = mockTreeEntries(rel);
    if (!entries) throw new ApiError(404, 'Unknown tree path');
    return { path: rel, entries: entries };
  }

  if (path === '/api/infer' && method === 'POST') {
    // mock 树是扁平的:rel == name;同时兼容旧的 {stems}
    const rels = Array.isArray(body.rels) ? body.rels
      : (Array.isArray(body.stems) ? body.stems.map(s => s + '.mp4') : []);
    if (!rels.length) throw new ApiError(400, 'rels 为空');
    const ids = rels.map(rel => {
      const v = mockDb.videos.find(v => v.rel === rel);
      const job = {
        id: mockDb.nextJobId++, kind: 'infer', stem: v ? v.stem : rel.replace(/\.[^.]+$/, ''),
        rel: rel, status: 'queued',
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

  if (path === '/api/config/events' && method === 'GET') return MOCK_EVENT_CONFIG;

  m = path.match(/^\/api\/results\/([^/]+)\/sft$/);
  if (m && method === 'PUT') {
    const stem = decodeURIComponent(m[1]);
    const v = mockDb.videos.find(v => v.stem === stem);
    if (!v || !v.has_results) throw new ApiError(404, 'SFT 文件不存在');
    const r = mockResults(stem);
    const old = r.sft_label;
    // 与后端一致:仅 description / action 可变,action 必须是合法编号
    const ALLOWED = [1, 2, 3, 4, 5, 6, 7, 8, 10, 11];
    if (!body || !Array.isArray(body.action) || !body.action.every(a => ALLOWED.includes(a))) {
      throw new ApiError(422, 'action 含非法编号');
    }
    ['chunk', 'idx', 'start_timestamp', 'end_timestamp', 'chunk_name'].forEach(k => {
      if (JSON.stringify(body[k]) !== JSON.stringify(old[k])) throw new ApiError(422, k + ' 不可修改');
    });
    r.sft_label = body;
    return body;
  }

  throw new ApiError(404, 'mock: 未实现 ' + method + ' ' + path);
}

/* ================================================================
   任务步骤文案
   ================================================================ */
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
// 递归渲染一层树节点;depth 控制缩进(每级 14px)
function treeRowsHtml(entries, depth) {
  let html = '';
  entries.forEach(e => {
    const pad = 'style="padding-left:' + (8 + depth * 14) + 'px"';
    if (e.type === 'dir') {
      const open = state.tree.expanded.has(e.rel);
      html += '<div class="tree-row tree-dir" data-dir="' + esc(e.rel) + '" ' + pad + '>'
        + '<span class="tree-caret">' + (open ? '▾' : '▸') + '</span>'
        + '<span class="tree-ico">📁</span>'
        + '<span class="tree-name" title="' + esc(e.rel) + '">' + esc(e.name) + '</span></div>';
      if (open) {
        const kids = state.tree.children[e.rel];
        const childPad = 'style="padding-left:' + (8 + (depth + 1) * 14) + 'px"';
        if (kids && kids.length) html += treeRowsHtml(kids, depth + 1);
        else if (kids) html += '<div class="tree-empty" ' + childPad + '>空目录</div>';
        else html += '<div class="tree-empty" ' + childPad + '>加载中…</div>';
      }
    } else if (e.is_video) {
      // 视频(任意深度):勾选键为 rel,徽标/点击与顶层一致,均进入分析视图
      const rel = e.rel;
      const v = state.videos.find(v => v.rel === rel) || {
        stem: e.stem || e.name.replace(/\.[^.]+$/, ''), rel: rel, has_results: !!e.has_results,
      };
      const st = videoStatus(v);
      // 运行中:视频名右侧渲染旋转 spinner 替代文字徽标;排队/完成/失败保持徽标
      const statusHtml = st.cls === 'st-running'
        ? '<span class="spinner" title="推理中"></span>'
        : '<span class="badge ' + st.cls + '">' + st.text + '</span>';
      html += '<div class="video-item' + (state.currentRel === rel ? ' active' : '')
        + '" data-rel="' + esc(rel) + '" ' + pad + '>'
        + '<input type="checkbox" data-check="' + esc(rel) + '"' + (state.checked.has(rel) ? ' checked' : '') + '>'
        + '<span class="tree-ico">🎬</span>'
        + '<div class="video-meta"><div class="video-name" title="' + esc(rel) + '">' + esc(e.name) + '</div>'
        + '<div class="video-sub">' + fmtBytes(e.size) + '</div></div>'
        + statusHtml
        + '</div>';
    } else {
      // 非视频文件:仅展示,不可勾选/选中
      html += '<div class="tree-row tree-file" ' + pad + ' title="' + esc(e.rel) + '">'
        + '<span class="tree-caret"></span>'
        + '<span class="tree-ico">📄</span>'
        + '<span class="tree-name">' + esc(e.name) + '</span></div>';
    }
  });
  return html;
}

function renderSidebar() {
  const list = $('#video-list');
  if (!state.workspace || !state.workspace.path) {
    sidebarSnapshot = '';
    list.innerHTML = '<div class="side-empty">设置工作区后列出文件</div>';
    return;
  }
  if (!state.tree.loaded) {
    sidebarSnapshot = '';
    list.innerHTML = '<div class="side-empty">加载中…</div>';
    return;
  }
  // 快照对比,避免每次轮询重建 DOM(防止打断勾选/展开)
  const snap = JSON.stringify([
    state.tree.root, state.tree.children, Array.from(state.tree.expanded),
    state.videos.map(v => [v.rel, v.has_results, videoStatus(v).text,
      state.checked.has(v.rel), state.currentRel === v.rel]),
  ]);
  if (snap === sidebarSnapshot) return;
  sidebarSnapshot = snap;

  if (!state.tree.root.length) {
    list.innerHTML = '<div class="side-empty">工作区目录为空</div>';
    $('#check-all').checked = false;
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
  $$('#video-list .video-item').forEach(item => {
    item.addEventListener('click', () => selectVideo(item.dataset.rel));
  });
  $('#check-all').checked = state.videos.length > 0 && state.videos.every(v => state.checked.has(v.rel));
}

// 展开/收起目录;首次展开时懒加载子级并缓存,再次展开直接用缓存
async function toggleDir(rel) {
  if (state.tree.expanded.has(rel)) {
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

async function selectVideo(rel) {
  const v = state.videos.find(v => v.rel === rel);
  if (!v) return;
  state.currentStem = v.stem;
  state.currentRel = v.rel;
  state.evTabIdx = 0;
  state.results = null;
  state.evidenceDraft = null;
  state.evidenceDirty = false;
  sidebarSnapshot = ''; renderSidebar();
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
    '<div class="cards">'
    + '<div class="card" id="card-preview"><div class="card-head"><span class="card-title">视频预览</span>'
    + '<span class="card-sub">' + esc(stem) + '</span></div>'
    + '<div class="card-body" id="preview-body"></div></div>';

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

  html += '<div id="eval-card-slot"></div></div>';
  main.innerHTML = html;

  mountPreview(source, r.evidence && r.evidence.video);
  if (hasResults) {
    renderSftBody(r.sft_label);
    renderReportBody(r.report_md, stem);
    renderEvidenceCard(stem, source);
  }
  renderEvalCard();
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

/* ------------------------------------------------------------ SFT 卡 */
// event_id → 标注文档 v4.5 的 action 编号(action 9 = 正常占位,跳过)
const EVENT_ID_TO_ACTION = { 0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 10, 9: 11 };

// 解析 description:think 按空行分段,匹配「事件名：」前缀;answer 提取天气/时间/场景键值
function parseSftDescription(desc, events) {
  const sections = {};   // event_id -> 段落正文(去掉「事件名：」前缀)
  const unmatched = [];  // 匹配不到任何事件名的段落(原样保留,保存时回写)
  const env = { '天气': '', '时间': '', '场景': '' };  // 天气/时间/场景键值(答案区可编辑)
  const thinkM = String(desc || '').match(/<think>([\s\S]*?)<\/think>/);
  if (thinkM) {
    thinkM[1].trim().split(/\n\s*\n/).forEach(para => {
      const p = para.trim();
      if (!p) return;
      const m = p.match(/^([^：\n]{1,30})：/);
      const ev = m ? events.find(e => e.name_zh === m[1]) : null;
      if (ev && ev.is_active && sections[ev.event_id] === undefined) {
        sections[ev.event_id] = p.slice(m[0].length).trim();
      } else if (ev) {
        // 未激活事件的模型原文不展示、保存时丢弃;重复段落同样丢弃
      } else {
        unmatched.push(p);
      }
    });
  }
  const answerM = String(desc || '').match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerM) {
    answerM[1].split('\n').forEach(line => {
      const m = line.trim().match(/^(天气|时间|场景)\s*[:：]\s*(.*)$/);
      if (m) env[m[1]] = m[2];
    });
  }
  return { sections: sections, unmatched: unmatched, env: env };
}

// 天气/时间/场景按固定顺序重建为 answer 行;空值回退「未知」(与 core/sft_label_rewrite.py 口径一致)
function sftEnvLines() {
  const env = (state.sftDraft && state.sftDraft.env) || {};
  return ['天气', '时间', '场景'].map(k => {
    const v = String(env[k] || '').trim();
    return k + '：' + (v || '未知');
  });
}

// 由当前「检出」勾选生成结论行(保存与只读预览共用同一口径)
function sftConclusionLines() {
  const d = state.sftDraft;
  const events = state.eventConfig || [];
  const checked = events.filter(ev => d.checks[ev.event_id]);
  if (!checked.length) return ['最终结论：本视频块未检出任何事件,交通状况正常。'];
  const lines = ['最终结论：本视频块检出以下事件。'];
  checked.forEach(ev => {
    lines.push('class' + EVENT_ID_TO_ACTION[ev.event_id] + ': ' + ev.name_zh);
  });
  return lines;
}

// 由当前草稿重建 description 与 action(结论区按「检出」勾选重建)
function buildSftRevision() {
  const d = state.sftDraft;
  const events = state.eventConfig || [];
  const sections = [];
  events.forEach(ev => {
    const t = String(d.texts[ev.event_id] || '').trim();
    if (t) sections.push(ev.name_zh + '：' + t);
  });
  const think = sections.concat(d.unmatched).join('\n\n');
  const checked = events.filter(ev => d.checks[ev.event_id]);
  const answerLines = sftEnvLines().concat(sftConclusionLines());
  return {
    description: '<think>\n' + think + '\n</think>\n<answer>\n' + answerLines.join('\n') + '\n</answer>',
    action: checked.map(ev => EVENT_ID_TO_ACTION[ev.event_id]),
  };
}

function sftSignature() {
  return JSON.stringify(buildSftRevision());
}

// 从 sft 样本初始化编辑草稿(检出初值 = action 反映射;未激活事件留空不勾)
function initSftDraft(sft) {
  const events = state.eventConfig || [];
  const parsed = parseSftDescription(sft.description, events);
  const actions = Array.isArray(sft.action) ? sft.action : [];
  const texts = {}, checks = {};
  events.forEach(ev => {
    if (ev.is_active) {
      texts[ev.event_id] = parsed.sections[ev.event_id] || '';
      checks[ev.event_id] = actions.indexOf(EVENT_ID_TO_ACTION[ev.event_id]) >= 0;
    } else {
      texts[ev.event_id] = '';
      checks[ev.event_id] = false;
    }
  });
  state.sftDraft = {
    texts: texts, checks: checks,
    unmatched: parsed.unmatched, env: parsed.env,
  };
  state.sftSavedSig = sftSignature();
}

// textarea 自适应高度:随内容增长,超过上限后出现滚动条
const SFT_TEXTAREA_MAX_H = 300;
function autoGrow(ta) {
  ta.style.height = 'auto';
  const border = ta.offsetHeight - ta.clientHeight; // border-box 下高度需含边框
  const need = ta.scrollHeight + border;
  const capped = need > SFT_TEXTAREA_MAX_H;
  ta.style.height = (capped ? SFT_TEXTAREA_MAX_H : need) + 'px';
  ta.style.overflowY = capped ? 'auto' : 'hidden';
}

function sftEditorHtml() {
  const d = state.sftDraft;
  let html = '<div class="sft-section-title">事件思考(按事件编辑;「检出」勾选在保存时联动 action 与结论)</div>';
  (state.eventConfig || []).forEach(ev => {
    html += '<div class="sft-ev' + (ev.is_active ? '' : ' inactive') + '">'
      + '<div class="sft-ev-head">'
      + '<span class="sft-ev-name">' + esc(ev.name_zh) + '</span>'
      + (ev.is_active ? '' : '<span class="sft-ev-tag">未激活</span>')
      + '<label class="sft-ev-check"><input type="checkbox" data-ev-check="' + ev.event_id + '"'
      + (d.checks[ev.event_id] ? ' checked' : '') + '>检出</label>'
      + '</div>'
      + '<textarea class="sft-ev-text" data-ev-text="' + ev.event_id + '" rows="2"'
      + (ev.is_active ? '' : ' placeholder="未激活事件类别,可人工修改"')
      + '>' + esc(d.texts[ev.event_id] || '') + '</textarea>'
      + '</div>';
  });
  if (d.unmatched.length) {
    html += '<div class="sft-section-title">未归类原文(只读,保存时原样附加到思考末尾)</div>'
      + '<textarea class="sft-ev-text sft-unmatched" readonly rows="2">'
      + esc(d.unmatched.join('\n\n')) + '</textarea>';
  }
  // 天气/时间用单行输入框,场景用自适应文本框;原始答案缺行时也显示空编辑框供人工补全
  const envRows = ['天气', '时间', '场景'].map(k => {
    const label = '<span class="answer-key">' + esc(k) + '</span>';
    if (k === '场景') {
      return '<div class="answer-row">' + label
        + '<textarea class="sft-ev-text answer-env-text" data-env="' + esc(k) + '" rows="2">'
        + esc(d.env[k] || '') + '</textarea></div>';
    }
    return '<div class="answer-row">' + label
      + '<input class="answer-input" data-env="' + esc(k) + '" value="' + esc(d.env[k] || '') + '"></div>';
  }).join('');
  html += '<div class="sft-section-title">答案(ANSWER)</div><div class="answer-block">'
    + envRows
    + '</div>'
    + '<div id="sft-conclusion-preview" class="answer-block sft-conclusion"></div>'
    + '<div class="sft-actions">'
    + '<span class="dirty-flag" id="sft-dirty-flag" hidden>● 未保存</span>'
    + '<button class="btn btn-ghost btn-sm" id="btn-sft-reset" disabled>重置</button>'
    + '<button class="btn btn-primary btn-sm" id="btn-sft-save" disabled>保存</button>'
    + '</div>';
  return html;
}

// 刷新只读的最终结论预览(与 buildSftRevision 同一数据来源,随勾选实时联动)
function refreshSftConclusion() {
  const el = $('#sft-conclusion-preview');
  if (!el) return;
  el.innerHTML = sftConclusionLines().map(line => {
    const m = line.match(/^(class\d+):\s*(.*)$/);
    if (m) {
      return '<div class="answer-row"><span class="answer-key answer-class">' + esc(m[1]) + '</span>'
        + '<span class="answer-val">' + esc(m[2]) + '</span></div>';
    }
    const m2 = line.match(/^最终结论：([\s\S]*)$/);
    return '<div class="answer-row"><span class="answer-key">最终结论</span>'
      + '<span class="answer-val">' + esc(m2 ? m2[1] : line) + '</span></div>';
  }).join('');
}

function updateSftDirty() {
  const dirty = sftSignature() !== state.sftSavedSig;
  const f = $('#sft-dirty-flag'); if (f) f.hidden = !dirty;
  const s = $('#btn-sft-save'); if (s) s.disabled = !dirty;
  const r = $('#btn-sft-reset'); if (r) r.disabled = !dirty;
}

function bindSftEditor(body) {
  // 所有 textarea 挂载时先按内容自适应一次(含只读的未归类原文框)
  $$('textarea', body).forEach(autoGrow);
  $$('textarea[data-ev-text]', body).forEach(ta => {
    ta.addEventListener('input', () => {
      state.sftDraft.texts[+ta.dataset.evText] = ta.value;
      autoGrow(ta);
      updateSftDirty();
    });
  });
  $$('input[data-ev-check]', body).forEach(cb => {
    cb.addEventListener('change', () => {
      state.sftDraft.checks[+cb.dataset.evCheck] = cb.checked;
      refreshSftConclusion();
      updateSftDirty();
    });
  });
  $$('[data-env]', body).forEach(el => {
    el.addEventListener('input', () => {
      state.sftDraft.env[el.dataset.env] = el.value;
      if (el.tagName === 'TEXTAREA') autoGrow(el);
      updateSftDirty();
    });
  });
  refreshSftConclusion();
  $('#btn-sft-save', body).addEventListener('click', saveSft);
  $('#btn-sft-reset', body).addEventListener('click', () => {
    renderSftBody(state.results.sft_label);
    toast('已重置为磁盘版本');
  });
}

async function saveSft() {
  const stem = state.currentStem;
  if (!stem || !state.results || !state.results.sft_label) return;
  const btn = $('#btn-sft-save');
  if (btn) btn.disabled = true;
  // 只改 description / action,其余字段原样提交(后端会校验)
  const payload = Object.assign({}, state.results.sft_label, buildSftRevision());
  const inFlightSig = sftSignature(); // 在途 payload 的签名,用于识别保存期间的继续编辑
  try {
    const saved = await api('/api/results/' + encodeURIComponent(stem) + '/sft', {
      method: 'PUT', body: payload,
    });
    if (state.currentStem !== stem) return; // 期间切换了视频
    state.results.sft_label = saved || payload;
    toast('已保存', 'ok');
    if (sftSignature() === inFlightSig) {
      renderSftBody(state.results.sft_label); // 保存期间无新编辑:以保存后的内容重建草稿
    } else {
      updateSftDirty(); // 保存期间用户继续编辑:保留草稿,仅重算 dirty
    }
  } catch (e) {
    if (btn) btn.disabled = false;
    toast('保存失败(' + e.status + '):' + e.message, 'err');
  }
}

async function renderSftBody(sft) {
  const body = $('#sft-body');
  if (!body) return;
  if (!sft) { body.innerHTML = '<div class="empty-note">无 SFT 标注</div>'; return; }

  const meta = '<div class="sft-meta">'
    + '<span>' + esc(sft.chunk || '') + '</span>'
    + '<span>idx: ' + esc(sft.idx) + '</span>'
    + '<span>' + esc(sft.start_timestamp) + 's → ' + esc(sft.end_timestamp) + 's</span>'
    + '<span>' + esc(sft.chunk_name || '') + '</span>'
    + '</div>';

  if (!state.eventConfig) {
    body.innerHTML = meta + '<div class="empty-note">加载事件配置…</div>';
    try {
      state.eventConfig = await api('/api/config/events');
    } catch (e) {
      if (body.isConnected) {
        body.innerHTML = meta + '<div class="empty-note">事件配置加载失败:' + esc(e.message) + '</div>';
      }
      return;
    }
  }
  if (!body.isConnected) return; // 期间切换了视图
  initSftDraft(sft);
  body.innerHTML = meta + sftEditorHtml();
  bindSftEditor(body);
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

function renderEvidenceCard(stem, source) {
  runCleanups(); // 重挂面板前释放上一个证据面板的 window 监听(mouseup/resize)
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
    renderEvidenceCard(stem, source);
  }));

  const saveBtn = $('#btn-ev-save');
  const resetBtn = $('#btn-ev-reset');
  if (saveBtn) saveBtn.onclick = saveEvidence;
  if (resetBtn) resetBtn.onclick = resetEvidence;

  mountEvidencePane(body, stem, source, draft.events[state.evTabIdx], draft.video || {});
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
    if (state.currentStem !== stem) return; // 期间切换了视频
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
    if (state.currentStem !== stem) return; // 期间切换了视频
    if (r && r.evidence) {
      state.results.evidence = r.evidence;
      state.evidenceDraft = JSON.parse(JSON.stringify(r.evidence));
    }
    clearDirty();
    renderEvidenceCard(stem, videoSource({ stem: state.currentStem, rel: state.currentRel }));
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

function mountEvidencePane(mount, stem, source, ev, videoInfo) {
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
    img.src = sourceFrameUrl(source, frameIdx);
  }

  $('[data-a="prev"]', toolbar).addEventListener('click', () => setFrame(frameIdx - 1));
  $('[data-a="next"]', toolbar).addEventListener('click', () => setFrame(frameIdx + 1));
  $('.ev-frame-input', toolbar).addEventListener('change', e => setFrame(+e.target.value || 0));

  const onResize = () => fit();
  window.addEventListener('resize', onResize);
  img.addEventListener('load', fit);
  // 分隔条拖动等容器尺寸变化不触发 window resize,用 ResizeObserver 兜底
  const ro = new ResizeObserver(() => fit());
  ro.observe(stage);
  img.src = sourceFrameUrl(source, frameIdx);

  state.cleanups.push(() => {
    ro.disconnect();
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
    + (running ? '<span class="spinner spinner-inverse"></span>评估中…' : '运行评估') + '</button></div>'
    + '<div class="card-body" id="eval-body"></div></div>';
  slot.innerHTML = inner;

  const btn = $('#btn-eval-run');
  if (btn) btn.addEventListener('click', runEvaluate);

  const body = $('#eval-body');
  if (running) {
    body.innerHTML = '<div class="eval-running"><span class="spinner"></span><span>'
      + jobStepText(evalJob) + '</span></div>'
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
// 工作区已切换后的统一刷新(目录弹窗确认后调用)
async function applyWorkspace(ws) {
  state.workspace = ws;
  $('#ws-path').textContent = ws.path;
  state.currentStem = null;
  state.currentRel = null;
  state.checked.clear();
  state.evalData = null;
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
function onDirRecentChange(e) {
  const v = e.target.value;
  e.target.value = '';
  if (v === '__home__') navDir(null);
  else if (v === '__current__' && state.workspace) navDir(state.workspace.path);
  else if (v) navDir(v);
}

// 点击「选择工作区…」:打开页内目录导航弹窗
function browseWorkspace() {
  dirModal.open = true;
  $('#dir-modal').hidden = false;
  $('#dir-input').hidden = true;
  $('#dir-crumbs').hidden = false;
  renderDirRecent();
  $('.dir-dialog').focus();
  const start = state.workspace && state.workspace.path ? state.workspace.path : null;
  navDir(start); // 无 path 时后端回退到当前工作区或用户主目录
}

function closeDirModal() {
  dirModal.open = false;
  $('#dir-modal').hidden = true;
  $('#btn-workspace').focus();
}

async function navDir(path) {
  dirModal.loading = true;
  renderDirList();
  let data = null;
  try {
    data = await api(path ? '/api/fs/list?path=' + encodeURIComponent(path) : '/api/fs/list');
  } catch (e) {
    toast('读取目录失败(' + e.status + '):' + e.message, 'err');
  }
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

function showDirInput() {
  const input = $('#dir-input');
  $('#dir-crumbs').hidden = true;
  input.hidden = false;
  input.value = dirModal.cwd || '';
  input.focus();
  input.select();
}

async function confirmDir() {
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
function dirModalKeys(e) {
  if (!dirModal.open) return;
  if (e.key === 'Escape') { closeDirModal(); return; }
  if (e.key !== 'Tab') return;
  const els = $$('#dir-modal button, #dir-modal input, #dir-modal select').filter(el => !el.hidden && !el.disabled);
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
  else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
}

// 加载工作区文件树;preserve 时保留已展开目录(轮询刷新用)
// state.videos 来自递归的 /api/workspace/videos(任意深度,含 stem+rel)
async function loadTree(preserve) {
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

async function startInfer() {
  const rels = state.videos.filter(v => state.checked.has(v.rel)).map(v => v.rel);
  if (!rels.length) return;
  try {
    await api('/api/infer', { method: 'POST', body: { rels: rels } });
    toast('已提交 ' + rels.length + ' 个推理任务');
    pollJobs();
  } catch (e) {
    toast('推理提交失败(' + e.status + '):' + e.message, 'err');
  }
  syncButtons();
}

/* ================================================================
   轮询
   ================================================================ */
// 当前视图是否存在未保存的 SFT / 证据编辑
function hasUnsavedEdits() {
  if (state.evidenceDirty) return true;
  if (state.sftDraft && sftSignature() !== state.sftSavedSig) return true;
  return false;
}

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
    let reloadRel = null;
    jobs.forEach(j => {
      next[j.id] = j.status;
      const was = prev[j.id];
      if (was && was !== j.status && (j.status === 'done' || j.status === 'failed')) {
        needVideos = true;
        if (j.kind === 'evaluate' && j.status === 'done') loadEvalLatest();
        if (j.kind === 'evaluate' && j.status === 'failed') {
          toast('评估失败(rc=' + j.returncode + ')', 'err'); renderEvalCard();
        }
        if (j.kind === 'infer' && j.status === 'done') {
          toast('推理完成:' + (j.rel || j.stem || ''), 'ok');
        }
        if (j.kind === 'infer' && j.status === 'done' && (j.rel || j.stem) === state.currentRel) {
          reloadRel = j.rel || j.stem; // 刷新列表后再重载当前视频结果
        }
        if (j.kind === 'infer' && j.status === 'failed') {
          toast('推理失败:' + (j.rel || j.stem || '') + ' (rc=' + j.returncode + ')', 'err');
        }
      }
      if (j.kind === 'evaluate' && (j.status === 'running' || j.status === 'queued')) {
        renderEvalCard(); // 更新评估卡进度
      }
    });
    state.prevJobStatus = next;
    renderSidebar();
    syncButtons();
    if (needVideos) await loadTree(true);
    if (reloadRel && reloadRel === state.currentRel) {
      if (hasUnsavedEdits()) {
        // 有未保存编辑时不自动重载,避免丢弃草稿;该 toast 每个任务只在状态翻转时触发一次
        toast('「' + reloadRel + '」已重新分析完成,但当前有未保存的修改;请先保存或手动重新加载', 'err');
      } else {
        selectVideo(reloadRel);
      }
    }
  } catch (e) {
    // 后端未启动等场景:静默,下个周期重试
  } finally {
    polling = false;
  }
}

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
      await Promise.all([loadTree(), loadEvalLatest(), pollJobs()]);
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

