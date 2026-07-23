/* ------------------------------------------------------------ 证据编辑卡 */
import { $, $$, esc, toast, flashSaveBtn } from './util.js';
import { state } from './state.js';
import { api, videoSource, sourceFrameUrl, imageUrl } from './api.js';
import { runCleanups } from './preview.js';

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

export function renderEvidenceCard(stem, source) {
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
    flashSaveBtn($('#btn-ev-save')); // 按钮短暂显示 ✓
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
