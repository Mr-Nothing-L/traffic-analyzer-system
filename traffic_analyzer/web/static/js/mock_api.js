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
