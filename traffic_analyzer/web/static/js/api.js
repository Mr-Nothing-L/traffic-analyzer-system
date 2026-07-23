/* ------------------------------------------------------------ API 层 */
import { MOCK } from './state.js';
import { ApiError } from './util.js';
import { mockApi, mockFrameUrl, mockImageUrl } from './mock.js';

export async function api(path, opts) {
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

export function frameUrl(stem, index) {
  if (MOCK) return mockFrameUrl(stem, index);
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
  if (MOCK) return mockImageUrl(stem, name);
  // name 为相对 analysis/<stem>/ 的路径(report.md 的 "tmp_img/.../x.jpg" 或
  // evidence.json 的 "images/x.jpg"),按原路径请求,不再降级为 basename。
  return '/api/results/' + encodeURIComponent(stem) + '/file?path=' + encodeURIComponent(name);
}
