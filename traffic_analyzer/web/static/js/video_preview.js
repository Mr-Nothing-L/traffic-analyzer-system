/* ------------------------------------------------------------ 视频预览卡 */
import { $, esc } from './util.js';
import { MOCK } from './state.js';
import { api, metaUrl, sourceFrameUrl } from './api.js';

// mock 数据库按需加载(与 api.js 同一惰性方案):仅 ?mock=1 时动态 import,
// 非 mock 模式零 mock 模块下载。REAL 由 mock_db.js 异步初始化(其内部 await
// mock_data.js);首次预览调用晚于 main.js init 的 mock 加载 await,读取时必定已就绪
let REAL = null;
if (MOCK) import('./mock_db.js').then(m => { REAL = m.REAL; });

export function streamUrl(source, ss) {
  if (MOCK) {
    // mock + 真实数据:演示视频直连真实后端流(工作区已由 mock_db.js 切到演示区);
    // 无真实数据或合成视频(如 clips/nested_clip.mp4)保持 null,走逐帧预览
    const v = REAL && REAL.videos && REAL.videos.find(v => v.stem === source.stem);
    if (!v) return null;
    return '/api/workspace/stream?path=' + encodeURIComponent(v.rel);
  }
  let url = source.rel != null
    ? '/api/workspace/stream?path=' + encodeURIComponent(source.rel)
    : '/api/videos/' + encodeURIComponent(source.stem) + '/stream';
  if (ss != null && ss > 0) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'ss=' + ss.toFixed(2);
  return url;
}

export function mountPreview(source, videoInfo) {
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
export function mountFrameStepper(mount, source, hint, onRetry, videoInfo) {
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

export function buildStepper(mount, source, total, hint, onRetry) {
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
  // 滑块高频 input:rAF 节流换帧(序号即时更新,帧图每帧最多换一次)
  let sliderRaf = 0;
  slider.addEventListener('input', () => {
    idxLabel.textContent = (+slider.value) + ' / ' + slider.max;
    if (sliderRaf) return;
    sliderRaf = requestAnimationFrame(() => {
      sliderRaf = 0;
      img.src = sourceFrameUrl(source, +slider.value);
    });
  });
  // 单帧读取失败:仅显示占位提示,不改动滑块范围
  img.addEventListener('load', () => { img.hidden = false; frameErr.hidden = true; });
  img.addEventListener('error', () => { img.hidden = true; frameErr.hidden = false; });
  $('#pv-retry', mount).addEventListener('click', onRetry);
}
