/* ------------------------------------------------------------ 专家工作间(推理进行面板) */
import { $, esc } from './util.js';
import { state } from './state.js';
import { api } from './api.js';
import { latestJobForStem } from './tree.js';
import { cancelJob } from './jobs.js';
import { pixelBarHtml, paintPixelBar } from './pixel_bar.js';
import { icon } from './icons.js';

// GET /api/expert-phases 的阶段定义缓存(每类别 [{fraction, label}]);404 时记 null,走内置 fallback 封顶
let expertPhasesPromise = null;
export function loadExpertPhases() {
  if (!expertPhasesPromise) {
    expertPhasesPromise = api('/api/expert-phases')
      .then(d => (d && d.categories) || d || null)
      .catch(() => null);
  }
  return expertPhasesPromise;
}

// 泳道 displayed 到达 target 后的缓行封顶:阶段序列中 displayed 之后的下一个里程碑(绝不越过);
// 无阶段定义(/api/expert-phases 404)时封顶在当前 fraction+0.1 或 1.0
export function nextMilestone(phases, name, displayed, target) {
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

export const EXPERT_CATCH_RATE = 0.3;  // displayed 线性逼近 target 的恒定速率(fraction/秒);缓存命中秒回的推理也能看清推进
export const EXPERT_CREEP_RATE = 0.015; // 到达 target 且仍 running 时,向下个里程碑缓行的速率
export const LANE_CELLS = 18;  // 每条泳道的像素列数(3×N 网格,大方块窄间隔铺满卡宽)

export function mountExpertPanel(job) {
  const body = $('#experts-body');
  if (!body) return;
  const stem = job.stem;
  body.innerHTML =
    '<div class="expert-panel">'
    + '<div class="expert-head">'
    + '<span class="expert-step" id="exp-step"></span>'
    + '<span class="mini-prog pixel-bar" id="exp-mini" title="总进度">' + pixelBarHtml(8) + '</span>'
    + '<button class="btn btn-ghost btn-sm stop-btn" id="exp-stop" title="停止推理">' + icon('stop') + ' 停止推理</button>'
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

  const miniCells = Array.from($('#exp-mini').children);

  const lanes = new Map(); // name -> {displayed, row, cells, phaseEl, cls}
  let laneSig = '';
  let lastT = performance.now();
  let rafId = 0;

  // 泳道 DOM 按专家名单签名重建(后端首次透出 experts 前可能为空);
  // 重建时保留已有泳道的 displayed(阶段泳道追加不该让全局面值归零重爬,
  // 否则与侧栏迷你条在 SFT/报告阶段视觉脱节)
  function syncLanes(list) {
    const sig = list.map(e => e.name).join('|');
    if (sig === laneSig) return;
    laneSig = sig;
    const prev = new Map(lanes);
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
        const old = prev.get(ex.name);
        lanes.set(ex.name, {
          displayed: old ? old.displayed : 0, cls: 'lane-queued', row: row,
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
    // 自止时从 cleanups 摘除自身,避免 cleanups 数组随面板生命周期膨胀
    const i = state.cleanups.indexOf(stopLoop);
    if (i >= 0) state.cleanups.splice(i, 1);
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
          + '可在左侧点击「重试」,或重新勾选后点击「开始推理」。</div>';
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
      // queued 泳道降为单行摘要(名称 + 「等待调度」,像素矩阵由 CSS 隐藏);
      // running/done 保持全尺寸矩阵,阶段文案照旧
      const phaseText = ex.status === 'queued' ? '等待调度' : (ex.label || '');
      lane.phaseEl.textContent = phaseText;
      lane.phaseEl.title = phaseText;
      const cls = expertLaneCls(ex);
      if (cls !== lane.cls) {
        lane.cls = cls;
        lane.row.className = 'expert-lane ' + cls + (lane.judge ? ' lane-judge' : '');
      }
    });
    const stepEl = $('#exp-step');
    if (stepEl) stepEl.textContent = cp.step_label || '';
    paintPixelBar(miniCells, typeof cp.fraction === 'number' ? cp.fraction : 0, { running: true });
    rafId = requestAnimationFrame(frame);
  }

  state.cleanups.push(stopLoop); // 主区重渲染前自动停帧
  rafId = requestAnimationFrame(frame);
}
