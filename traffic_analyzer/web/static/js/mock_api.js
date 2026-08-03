/* ================================================================
   Mock 数据层(?mock=1)—— mockApi 路由与帧/图片占位 URL
   ================================================================ */
import { ApiError } from './util.js';
import { mockDb, mockFsTree, mockTreeEntries, mockResults, MOCK_EVENT_CONFIG } from './mock_db.js';
import { MOCK_EXPERT_PHASES } from './mock_tick.js';

const mockFrameCache = {};
const MOCK_FRAME_CACHE_MAX = 64; // 帧/图片 dataURL 缓存上限(LRU:命中提新,超出淘汰最久未用)

function mockCacheGet(key) {
  const url = mockFrameCache[key];
  if (url) { delete mockFrameCache[key]; mockFrameCache[key] = url; } // 命中提升到最新
  return url;
}

function mockCachePut(key, url) {
  delete mockFrameCache[key];
  mockFrameCache[key] = url;
  const keys = Object.keys(mockFrameCache);
  if (keys.length > MOCK_FRAME_CACHE_MAX) delete mockFrameCache[keys[0]];
}

/* ------------------------------------------------------------ 乐观锁(mock) */
// 冲突演示开关:?mock=1&conflict=1 时,每个 stem+kind 首次携带 base_sig 的保存
// 必现一次 409(模拟「他人已修改」);默认关闭,避免常规演示/e2e 的首次保存被打断。
// 无论开关与否,base_sig 与当前 file_sig 不匹配一律 409(契约行为)
const CONFLICT_DEMO = new URLSearchParams(location.search).get('conflict') === '1';

// file_sig:每个 stem+kind 各自递增,GET /api/results 携带;PUT 带 base_sig 校验,
// 每次成功后对应 kind 签名 +1(SFT 与证据互不影响,与后端按文件哈希语义一致)。
// 存储惰性挂在 mockDb 上(mock_db.js 不在本包改动范围)
function currentSig(stem, kind) {
  const sigs = mockDb.fileSigs || (mockDb.fileSigs = {});
  const key = stem + '|' + (kind || 'sft');
  if (!sigs[key]) sigs[key] = 'sig-1';
  return sigs[key];
}

function bumpSig(stem, kind) {
  const key = stem + '|' + (kind || 'sft');
  const n = parseInt(currentSig(stem, kind).split('-')[1], 10) + 1;
  mockDb.fileSigs[key] = 'sig-' + n;
  return mockDb.fileSigs[key];
}

function checkBaseSig(stem, kind, body) {
  if (!body || body.base_sig === undefined) return;
  const fired = mockDb.conflictFired || (mockDb.conflictFired = {});
  const key = stem + '|' + kind;
  if (CONFLICT_DEMO && !fired[key]) {
    fired[key] = true; // 演示:首次保存必现一次 409
    throw new ApiError(409, '文件已被他人修改(file_sig 不匹配)');
  }
  if (body.base_sig !== currentSig(stem, kind)) {
    throw new ApiError(409, '文件已被他人修改(file_sig 不匹配)');
  }
}

export function mockFrameUrl(stem, index) {
  const key = stem + '#' + index;
  const hit = mockCacheGet(key);
  if (hit) return hit;
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
  mockCachePut(key, url);
  return url;
}

export function mockImageUrl(stem, name) {
  const key = stem + '/' + name;
  const hit = mockCacheGet(key);
  if (hit) return hit;
  const c = document.createElement('canvas');
  c.width = 320; c.height = 180;
  const g = c.getContext('2d');
  g.fillStyle = '#6B6257'; g.fillRect(0, 0, 320, 180);
  g.fillStyle = '#F7F4EE'; g.font = '13px monospace';
  g.fillText(String(name).split('/').pop(), 12, 94);
  const url = c.toDataURL('image/jpeg', 0.85);
  mockCachePut(key, url);
  return url;
}

export async function mockApi(path, opts) {
  await new Promise(r => setTimeout(r, 60)); // 模拟网络延迟
  const method = (opts && opts.method) || 'GET';
  // 与真实 api() 的 JSON.stringify 序列化语义对齐:body 深拷贝隔离,
  // mock 库不持有调用方活对象(否则 evidence PUT 与前端草稿共享引用,「重置」失效)
  const body = JSON.parse(JSON.stringify((opts && opts.body) || {}));

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
    // 递归列表(rel 作为唯一键):顶层与 clips/ 嵌套视频同出 mockDb.videos
    return mockDb.workspace.path ? mockDb.videos : [];
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
    // rel 为唯一键(顶层 rel == name,嵌套为 clips/x.mp4);同时兼容旧的 {stems}
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

  if (path === '/api/jobs') return mockDb.jobs;

  // presence:GET 名册模拟另外两个用户(张三在编辑、李四在查看);POST 上报直接收下
  if (path === '/api/presence') {
    if (method === 'POST') return { ok: true };
    const v0 = mockDb.videos[0];
    const v1 = mockDb.videos[1] || v0;
    return [
      { username: '张三', viewing: null, editing: v0 ? v0.rel : null },
      { username: '李四', viewing: v1 ? v1.rel : null, editing: null },
    ];
  }

  // 数据看板汇总:GT 按文件名解析,pred 取 SFT 标注 action,metrics 实时计算;
  // 与后端一致,只回 summary/event_names/metrics(基于全量未过滤行),行走 /rows
  if (path === '/api/dashboard' && method === 'GET') {
    if (!mockDb.workspace.path) throw new ApiError(400, 'No workspace selected');
    const d = mockDashboard();
    return { summary: d.summary, event_names: d.event_names, metrics: d.metrics };
  }

  // 看板行:先过滤(consistency/review 逗号多值、edited=1、q 子串)后分页
  const dashRowsMatch = path.match(/^\/api\/dashboard\/rows(?:\?(.*))?$/);
  if (dashRowsMatch && method === 'GET') {
    if (!mockDb.workspace.path) throw new ApiError(400, 'No workspace selected');
    const q = new URLSearchParams(dashRowsMatch[1] || '');
    const page = parseInt(q.get('page') || '1', 10);
    const size = parseInt(q.get('size') || '50', 10);
    if (!(page >= 1) || !(size >= 1) || size > 200) {
      throw new ApiError(422, 'page/size 参数非法(1 ≤ size ≤ 200)');
    }
    const statuses = (q.get('consistency') || '').split(',').map(s => s.trim()).filter(Boolean);
    const reviews = (q.get('review') || '').split(',').map(s => s.trim()).filter(Boolean);
    const needle = (q.get('q') || '').trim().toLowerCase();
    let rows = mockDashboard().rows;
    if (statuses.length) rows = rows.filter(r => statuses.indexOf(r.status) >= 0);
    if (reviews.length) rows = rows.filter(r => reviews.indexOf(r.review) >= 0);
    if (q.get('edited') === '1') rows = rows.filter(r => r.edited);
    if (needle) {
      rows = rows.filter(r =>
        r.rel.toLowerCase().indexOf(needle) >= 0 || r.stem.toLowerCase().indexOf(needle) >= 0);
    }
    const total = rows.length;
    const start = (page - 1) * size;
    return {
      rows: rows.slice(start, start + size), // page 越界 → 空 rows
      page: page, size: size, total: total,
      total_pages: Math.ceil(total / size),
    };
  }

  // 人工审核结论:{stem, status};存内存(reviewStates),刷新页面即失
  if (path === '/api/dashboard/review' && method === 'PUT') {
    if (!body.stem || typeof body.stem !== 'string') throw new ApiError(422, 'stem 必填');
    if (!mockDb.videos.some(v => v.stem === body.stem)) throw new ApiError(404, '视频不存在: ' + body.stem);
    if (MOCK_REVIEW_STATUSES.indexOf(body.status) < 0) throw new ApiError(422, '非法审核状态: ' + body.status);
    mockDb.reviewStates[body.stem] = body.status;
    return { ok: true, stem: body.stem, status: body.status };
  }

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
    return Object.assign({}, mockResults(stem), {
      file_sig: currentSig(stem, 'sft'), evidence_sig: currentSig(stem, 'evidence') });
  }

  m = path.match(/^\/api\/results\/([^/]+)\/evidence$/);
  if (m && method === 'PUT') {
    const stem = decodeURIComponent(m[1]);
    if (!body || !Array.isArray(body.events)) throw new ApiError(422, 'evidence 结构不合法');
    checkBaseSig(stem, 'evidence', body); // 乐观锁:base_sig 不匹配 → 409
    delete body.base_sig; // 锁字段不落库
    const r = mockResults(stem);
    const old = r.evidence;
    // 简易校验:事件数一致、不可改字段未变
    if (old && body.events.length !== old.events.length) throw new ApiError(422, 'events 数量不可变');
    r.evidence = body;
    return Object.assign({}, body, { evidence_sig: bumpSig(stem, 'evidence') });
  }

  if (path === '/api/config/events' && method === 'GET') return MOCK_EVENT_CONFIG;

  m = path.match(/^\/api\/results\/([^/]+)\/sft$/);
  if (m && method === 'PUT') {
    const stem = decodeURIComponent(m[1]);
    const v = mockDb.videos.find(v => v.stem === stem);
    if (!v || !v.has_results) throw new ApiError(404, 'SFT 文件不存在');
    checkBaseSig(stem, 'sft', body); // 乐观锁:base_sig 不匹配 → 409
    delete body.base_sig; // 锁字段不落库也不回传
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
    // 返回保存后的对象并附带新 file_sig(前端更新缓存,下一次保存以此为 base_sig)
    return Object.assign({}, body, { file_sig: bumpSig(stem, 'sft') });
  }

  throw new ApiError(404, 'mock: 未实现 ' + method + ' ' + path);
}

/* ------------------------------------------------------------ 数据看板 */
// GT 从文件名解析:「01-02-04_Event_2048_...」前缀的横线分隔数字即真值事件编号;
// 无该前缀(如 nested_clip.mp4)视为无真值
function mockGtIds(rel) {
  const m = String(rel).split('/').pop().match(/^([\d-]+)_Event_/);
  if (!m) return [];
  return m[1].split('-').map(Number).filter(n => n > 0).sort((a, b) => a - b);
}

// 演示「人工已改」徽章:该样本的原始模型输出(pred_raw)少了 class2,
// 人工审核后补上 → edited=true(pred_raw_ids ≠ pred_ids);其余样本 raw == pred
const MOCK_PRED_RAW_OVERRIDES = {
  '01-02_Event_129_1755579215119_1': [1],
};

// 审核状态枚举,与 dashboard.js REVIEWS 一致;行默认 'unconfirmed'
const MOCK_REVIEW_STATUSES = ['unconfirmed', 'confirmed', 'needs_review'];

function idDiff(a, b) { return a.filter(x => b.indexOf(x) < 0); }

function mockDashboard() {
  const rows = mockDb.videos.map(v => {
    const gt = mockGtIds(v.rel);
    let pred = [];
    if (v.has_results) {
      const r = mockResults(v.stem);
      pred = (r.sft_label && Array.isArray(r.sft_label.action) ? r.sft_label.action.slice() : [])
        .sort((a, b) => a - b);
    }
    const predRaw = v.has_results
      ? (MOCK_PRED_RAW_OVERRIDES[v.stem] || pred).slice().sort((a, b) => a - b)
      : [];
    const edited = v.has_results && JSON.stringify(predRaw) !== JSON.stringify(pred);
    const missing = idDiff(gt, pred); // 漏检:GT 有而模型未检出
    const extra = idDiff(pred, gt);   // 误检:模型检出而 GT 无
    return {
      rel: v.rel, stem: v.stem, has_results: v.has_results,
      gt_ids: gt, pred_ids: pred,
      status: !v.has_results ? 'no_results'
        : (v.rel.indexOf('_Event_') < 0 ? 'no_gt'
          : (missing.length || extra.length ? 'diff' : 'consistent')),
      missing: missing, extra: extra,
      pred_raw_ids: predRaw, edited: edited,
      edit_extra: edited ? idDiff(pred, predRaw) : [],   // 相对 raw 人工补充的
      edit_missing: edited ? idDiff(predRaw, pred) : [], // 相对 raw 人工删除的
      review: mockDb.reviewStates[v.stem] || 'unconfirmed',
    };
  });
  const eventNames = {};
  MOCK_EVENT_CONFIG.forEach(e => { eventNames[String(e.event_id)] = e.name_zh; });
  return {
    rows: rows,
    summary: mockDashboardSummary(rows),
    event_names: eventNames,
    metrics: mockDashboardMetrics(rows),
  };
}

function mockDashboardSummary(rows) {
  const s = { total: rows.length, edited: rows.filter(r => r.edited).length };
  ['consistent', 'diff', 'no_gt', 'no_results'].forEach(k => {
    s[k] = rows.filter(r => r.status === k).length;
  });
  MOCK_REVIEW_STATUSES.forEach(k => {
    s[k] = rows.filter(r => r.review === k).length;
  });
  return s;
}

// 仅统计 has_results 且有 GT 的行;per_event 为数组(dashboard.js 按 .length/.forEach 消费),
// macro 为各类简单平均,micro 由总 TP/FP/FN 计算
function mockDashboardMetrics(rows) {
  const done = rows.filter(r => r.status === 'consistent' || r.status === 'diff');
  const ids = new Set();
  done.forEach(r => { r.gt_ids.forEach(i => ids.add(i)); r.pred_ids.forEach(i => ids.add(i)); });
  const per = [];
  let ttp = 0, tfp = 0, tfn = 0;
  Array.from(ids).sort((a, b) => a - b).forEach(id => {
    let tp = 0, fp = 0, fn = 0;
    done.forEach(r => {
      const inGt = r.gt_ids.indexOf(id) >= 0, inPred = r.pred_ids.indexOf(id) >= 0;
      if (inGt && inPred) tp++;
      else if (inPred) fp++;
      else if (inGt) fn++;
    });
    const p = tp + fp > 0 ? tp / (tp + fp) : 0;
    const rc = tp + fn > 0 ? tp / (tp + fn) : 0;
    per.push({
      event_id: id, tp: tp, fp: fp, fn: fn,
      precision: +p.toFixed(4), recall: +rc.toFixed(4),
      f1: +((p + rc) > 0 ? 2 * p * rc / (p + rc) : 0).toFixed(4),
    });
    ttp += tp; tfp += fp; tfn += fn;
  });
  const avg = k => per.length
    ? +(per.reduce((s, e) => s + e[k], 0) / per.length).toFixed(4) : 0;
  const mp = ttp + tfp > 0 ? ttp / (ttp + tfp) : 0;
  const mr = ttp + tfn > 0 ? ttp / (ttp + tfn) : 0;
  return {
    per_event: per,
    macro: { precision: avg('precision'), recall: avg('recall'), f1: avg('f1') },
    micro: {
      tp: ttp, fp: tfp, fn: tfn,
      precision: +mp.toFixed(4), recall: +mr.toFixed(4),
      f1: +((mp + mr) > 0 ? 2 * mp * mr / (mp + mr) : 0).toFixed(4),
    },
  };
}
