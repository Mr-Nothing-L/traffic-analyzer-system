/* ================================================================
   Mock 数据层(?mock=1)
   ================================================================ */
import { ApiError } from './util.js';

// 真实推理结果(scripts/build_mock_data.py 生成);不存在时回退下方合成数据
let REAL = null;
try {
  REAL = (await import('./mock_data.js')).REAL_MOCK;
} catch (e) { /* 无 mock_data.js:完全走合成兜底 */ }

const mockDb = {
  workspace: { path: '/mock/workspace' },
  videos: REAL ? REAL.videos : [
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

// 与 traffic_analyzer/config/event_categories.yaml 一致:编号 1-8 激活,10/11 未激活
// (编号 9 为「正常」占位,不在配置中;SFT 编辑器按 event_id 匹配草稿/勾选态)
// 有 REAL 时由 build_mock_data.py 从 yaml 生成(含 options 结构化选项组)
const MOCK_EVENT_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 10, 11];
let MOCK_EVENT_CONFIG = EVENT_NAMES_10.map((name, i) => {
  const id = MOCK_EVENT_IDS[i];
  return { event_id: id, name_zh: name, is_active: id <= 8 };
});
if (REAL) MOCK_EVENT_CONFIG = REAL.eventConfig;

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
    // 有真实结果时优先用(REAL.results 由 build_mock_data.py 生成);
    // 证据图片引用沿用 mockImageUrl 占位,不搬真实图片
    if (REAL && REAL.results && REAL.results[stem]) {
      mockDb.results[stem] = REAL.results[stem];
    } else {
      mockDb.results[stem] = {
        report_md: mockReport(),
        sft_label: mockSft(stem),
        evidence: mockEvidence(stem),
      };
    }
  }
  return mockDb.results[stem];
}

const mockFrameCache = {};
export function mockFrameUrl(stem, index) {
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

export function mockImageUrl(stem, name) {
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

/* ------------------------------------------------------------ 专家泳道慢速模拟 */
// 与 config 中 8 个激活类别一致 + 最后的「裁决」泳道;label 为各阶段中文短文案
const MOCK_EXPERT_DEFS = [
  ['违法停车', ['扫描路肩与车道边缘', '比对目标静止时长', '排除缓行车流误报']],
  ['应急车道占用', ['标定应急车道区域', '检测车道内停留目标', '核对特种车辆豁免特征']],
  ['交通事故', ['检测车辆异常姿态', '识别碎片与散落痕迹', '分析车流速度突变']],
  ['高速公路行人出现', ['扫描人体轮廓特征', '追踪目标移动轨迹', '排除护栏阴影干扰']],
  ['摩托车出现', ['检测两轮目标', '核对车型长宽比例', '评估行驶车道合法性']],
  ['拥堵', ['统计车道车流密度', '估算区间平均车速', '定位缓行队列尾部']],
  ['道路施工', ['识别锥桶与围挡', '检测施工机械特征', '核对车道封闭标志']],
  ['车辆逆行/倒车', ['估计车辆行驶方向', '比对断面车流主流向', '确认逆向持续时长']],
  ['裁决', ['汇总各专家结论', '仲裁冲突证据', '生成最终判定']],
];
// /api/expert-phases 的 mock 应答:里程碑取 (i+1)/(n+1),不含 1.0,给前端缓行封顶留出余量
const MOCK_EXPERT_PHASES = {};
MOCK_EXPERT_DEFS.forEach(([name, labels]) => {
  MOCK_EXPERT_PHASES[name] = labels.map((label, i) => ({
    fraction: +(((i + 1) / (labels.length + 1)).toFixed(2)), label: label,
  }));
});

function initMockExperts() {
  return MOCK_EXPERT_DEFS.map(([name]) => ({
    name: name, status: 'queued', detected: null, fraction: 0, label: '等待调度',
  }));
}

// 推进一条泳道 0.05-0.2,并按里程碑刷新阶段文案;到 1.0 置 done
// detectedIds 非空时(真实结果)按 泳道名→event_id 映射判定检出,否则走合成兜底
function advanceLane(lane, detectedIds) {
  lane.fraction = Math.min(1, +(lane.fraction + 0.05 + Math.random() * 0.15).toFixed(3));
  const phases = MOCK_EXPERT_PHASES[lane.name];
  const idx = phases.findIndex(s => s.fraction > lane.fraction);
  lane.label = phases[idx >= 0 ? idx : phases.length - 1].label;
  if (lane.fraction >= 1) {
    lane.status = 'done';
    if (lane.name === '裁决') {
      lane.detected = true; // 裁决泳道视为有结论
    } else if (detectedIds) {
      const ev = MOCK_EVENT_CONFIG.find(e => e.name_zh === lane.name);
      lane.detected = !!(ev && detectedIds.indexOf(ev.event_id) >= 0);
    } else {
      // 与合成 mock 结果集一致:仅「应急车道占用」检出
      lane.detected = lane.name === '应急车道占用';
    }
    lane.label = lane.detected ? '检出疑似目标' : '未发现相关迹象';
  }
}

export function mockTick() {
  mockDb.tickCount++;
  // 推理 job:串行推进
  let running = mockDb.jobs.find(j => j.status === 'running');
  if (!running) {
    const next = mockDb.jobs.find(j => j.status === 'queued');
    if (next) { next.status = 'running'; running = next; }
  }
  if (running) {
    if (running.kind === 'infer') {
      // 8 个类别专家泳道 + 「裁决」泳道的慢速 staggered 步进
      if (!running._experts) running._experts = initMockExperts();
      // 真实结果的检出集合(首次 tick 时确定);无则回退合成逻辑
      if (running._detected === undefined) {
        running._detected = (REAL && REAL.detectedMap && REAL.detectedMap[running.stem]) || null;
      }
      const experts = running._experts;
      // 类别泳道(不含裁决与 SFT/报告阶段泳道,阶段泳道不参与随机推进)
      const lanes = experts.filter(e => ['裁决', 'SFT 标注', '报告'].indexOf(e.name) < 0);
      const verdict = experts.find(e => e.name === '裁决');
      // 4 并发上限的假象:running 不足 4 条时启动下一条排队泳道
      const runningLanes = lanes.filter(e => e.status === 'running');
      if (runningLanes.length < 4) {
        const nextLane = lanes.find(e => e.status === 'queued');
        if (nextLane) {
          nextLane.status = 'running';
          nextLane.label = MOCK_EXPERT_PHASES[nextLane.name][0].label;
          runningLanes.push(nextLane);
        }
      }
      // 每 tick 随机挑 1-2 条 running 泳道推进
      const pool = runningLanes.slice();
      const picks = Math.min(pool.length, 1 + Math.floor(Math.random() * 2));
      for (let i = 0; i < picks; i++) {
        advanceLane(pool.splice(Math.floor(Math.random() * pool.length), 1)[0], running._detected);
      }
      // 全部类别 done 后才推进裁决泳道
      if (lanes.every(e => e.status === 'done')) {
        if (verdict.status === 'queued') {
          verdict.status = 'running';
          verdict.label = MOCK_EXPERT_PHASES['裁决'][0].label;
        } else if (verdict.status === 'running') {
          advanceLane(verdict, running._detected);
        }
      }
      const frac = experts.reduce((s, e) => s + (e.fraction || 0), 0) / experts.length;
      running.progress = {
        step_label: verdict.status === 'queued' ? '专家分析' : '裁决',
        step_index: verdict.status === 'queued' ? 2 : 3,
        total_steps: 5,
        fraction: +frac.toFixed(3),
        experts: experts,
      };
      running.log_tail = '[mock] 专家泳道完成 '
        + experts.filter(e => e.status === 'done').length + '/' + experts.length;
      if (verdict.status === 'done') {
        // 裁决后补两条阶段泳道:SFT 标注 → 报告,各停一拍,与真实任务周期对齐
        if (!running._stage) running._stage = 1;
        if (running._stage === 1) {
          experts.push({ name: 'SFT 标注', status: 'running', detected: null, fraction: 0.5, label: 'SFT 标签改写' });
          running.progress = { step_label: 'SFT', step_index: 4, total_steps: 5, fraction: 0.8, experts: experts };
          running._stage = 2;
        } else if (running._stage === 2) {
          const sft = experts.find(e => e.name === 'SFT 标注');
          if (sft) Object.assign(sft, { status: 'done', fraction: 1, label: 'SFT 完成' });
          experts.push({ name: '报告', status: 'running', detected: null, fraction: 0.5, label: '生成报告' });
          running.progress = { step_label: '报告', step_index: 5, total_steps: 5, fraction: 0.9, experts: experts };
          running._stage = 3;
        } else {
          const rep = experts.find(e => e.name === '报告');
          if (rep) Object.assign(rep, { status: 'done', fraction: 1, label: '报告完成' });
          running.status = 'done';
          running.progress = { step_label: '完成', step_index: 5, total_steps: 5, fraction: 1, experts: experts };
          running.returncode = 0;
          const v = mockDb.videos.find(v => v.stem === running.stem);
          if (v) v.has_results = true;
        }
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

export async function mockApi(path, opts) {
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
      // 重新推理会覆盖旧结果:清掉 has_results 与缓存结果,前端才会收起结果卡、
      // 显示「专家工作间」面板(renderResultCards 仅在无结果时渲染 #card-experts)
      if (v) { v.has_results = false; delete mockDb.results[v.stem]; }
      const job = {
        id: mockDb.nextJobId++, kind: 'infer', stem: v ? v.stem : rel.replace(/\.[^.]+$/, ''),
        rel: rel, status: 'queued',
        progress: { step_label: '排队中', step_index: 0, total_steps: 5, fraction: 0, experts: [] },
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

  // 取消任务:running/queued → failed(rc=-15 表示被终止)
  const cm = path.match(/^\/api\/jobs\/(\d+)\/cancel$/);
  if (cm && method === 'POST') {
    const job = mockDb.jobs.find(j => j.id === +cm[1]);
    if (!job) throw new ApiError(404, '任务不存在');
    if (job.status === 'running' || job.status === 'queued') {
      job.status = 'failed';
      job.returncode = -15;
      job.progress = Object.assign({}, job.progress, { step_label: '已停止' });
    }
    return { ok: true, status: job.status };
  }

  // 专家阶段定义:供前端计算泳道缓行的下一个里程碑封顶
  if (path === '/api/expert-phases' && method === 'GET') return MOCK_EXPERT_PHASES;

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
