/* ------------------------------------------------------------ API 层 */
import { MOCK } from './state.js';
import { ApiError } from './util.js';
import { redirectToLogin } from './auth.js';

// mock 体系按需加载:仅 ?mock=1 时动态 import(参照 mock_db.js 加载 mock_data.js 的写法),
// 非 mock 模式零 mock 模块下载。mockModule 供同步函数(frameUrl/imageUrl)读取:
// 首次渲染晚于本 await(main.js init 内同样 await mock 加载),同步读取时必定已就绪
let mockModule = null;
const mockReady = MOCK ? import('./mock.js').then(m => { mockModule = m; return m; }) : null;

// 乐观锁:GET /api/results/{stem} 响应里的 file_sig 按 stem 缓存,
// SFT/证据保存时作 base_sig 上送(见 sft.js / evidence.js);PUT 响应带新 file_sig 时更新
const fileSigCache = {};
const evidenceSigCache = {};
export function getFileSig(stem) {
  return fileSigCache[stem] || null;
}
export function getEvidenceSig(stem) {
  return evidenceSigCache[stem] || null;
}

// 从响应里摘取 file_sig / evidence_sig 并入缓存(仅 /api/results 相关端点)
function captureFileSig(path, json) {
  if (!json || typeof json !== 'object') return;
  const m = path.match(/^\/api\/results\/([^/]+)(?:\/(sft|evidence))?$/);
  if (!m) return;
  const stem = decodeURIComponent(m[1]);
  if (json.file_sig) fileSigCache[stem] = json.file_sig;
  if (json.evidence_sig) evidenceSigCache[stem] = json.evidence_sig;
}

export async function api(path, opts) {
  opts = opts || {};
  if (MOCK) {
    const { mockApi } = await mockReady;
    const mockRes = await mockApi(path, opts);
    captureFileSig(path, mockRes); // mock 同样维护 file_sig 缓存(乐观锁演示)
    return mockRes;
  }
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
    if (res.status === 401) redirectToLogin(); // 会话失效:统一跳登录页(mock 模式内部跳过)
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
  if (ct.includes('json')) {
    const json = await res.json();
    captureFileSig(path, json);
    return json;
  }
  return res.text();
}

export function frameUrl(stem, index) {
  if (MOCK) return mockModule.mockFrameUrl(stem, index);
  return '/api/videos/' + encodeURIComponent(stem) + '/frame?index=' + index;
}

// 当前视频的媒体来源:顶层视频用 {stem}(走 /api/videos/... 端点),
// 嵌套视频用 {stem, rel}(走 /api/workspace/... 端点;stem 仍用于结果图片)
export function videoSource(v) {
  return v.rel && v.rel.indexOf('/') >= 0 ? { stem: v.stem, rel: v.rel } : { stem: v.stem };
}

export function metaUrl(source) {
  if (source.rel != null) return '/api/workspace/meta?path=' + encodeURIComponent(source.rel);
  return '/api/videos/' + encodeURIComponent(source.stem) + '/meta';
}

export function sourceFrameUrl(source, index) {
  if (source.rel != null) {
    return '/api/workspace/frame?path=' + encodeURIComponent(source.rel) + '&index=' + index;
  }
  return frameUrl(source.stem, index);
}

export function imageUrl(stem, name) {
  if (MOCK) return mockModule.mockImageUrl(stem, name);
  // name 为相对 analysis/<stem>/ 的路径(report.md 的 "tmp_img/.../x.jpg" 或
  // evidence.json 的 "images/x.jpg"),按原路径请求,不再降级为 basename。
  return '/api/results/' + encodeURIComponent(stem) + '/file?path=' + encodeURIComponent(name);
}
