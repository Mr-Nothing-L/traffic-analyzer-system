/* ================================================================
   任务步骤文案
   ================================================================ */
import { $, esc, toast } from './util.js';
import { state, STEP_LABELS } from './state.js';
import { api } from './api.js';
import { loadTree, renderSidebar, syncButtons } from './tree.js';
import { selectVideo } from './preview.js';
import { sftSignature } from './sft.js';

export function jobStepText(job) {
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

/* ------------------------------------------------------------ 精度评估卡 */
export function renderEvalCard() {
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
    // 后端 log_tail 是字符串数组(逐行);归一化为多行文本,空数组不渲染 pre 块
    const lt = Array.isArray(evalJob.log_tail) ? evalJob.log_tail.join('\n') : (evalJob.log_tail || '');
    body.innerHTML = '<div class="eval-running"><span class="spinner"></span><span>'
      + jobStepText(evalJob) + '</span></div>'
      + (lt ? '<pre class="md" style="margin-top:10px"><code>' + esc(lt) + '</code></pre>' : '');
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

let evalPosting = false; // 提交防抖:双击时第二次直接忽略,与按钮 disabled 无关
export async function runEvaluate() {
  if (evalPosting) return;
  evalPosting = true;
  try {
    try {
      await api('/api/evaluate', { method: 'POST', body: {} });
      toast('评估任务已提交');
      pollJobs();
    } catch (e) {
      toast('评估提交失败(' + e.status + '):' + e.message, 'err');
    }
    renderEvalCard();
    syncButtons();
  } finally {
    evalPosting = false;
  }
}

export async function loadEvalLatest() {
  try {
    state.evalData = await api('/api/evaluate/latest');
  } catch (e) {
    if (e.status !== 404) toast('读取评估结果失败:' + e.message, 'err');
    state.evalData = null;
  }
  renderEvalCard();
}

/* ================================================================
   动作:推理
   ================================================================ */
let inferPosting = false; // 提交防抖:双击时第二次直接忽略,与按钮 disabled 无关
export async function startInfer() {
  const rels = state.videos.filter(v => state.checked.has(v.rel)).map(v => v.rel);
  if (!rels.length || inferPosting) return;
  inferPosting = true;
  try {
    try {
      await api('/api/infer', { method: 'POST', body: { rels: rels } });
      toast('已提交 ' + rels.length + ' 个推理任务');
      pollJobs().then(schedulePoll); // 提交后立即进入活动任务的 1.5s 轮询
    } catch (e) {
      toast('推理提交失败(' + e.status + '):' + e.message, 'err');
    }
    syncButtons();
  } finally {
    inferPosting = false;
  }
}

// 失败任务重试:仅对该视频重新 POST /api/infer,轮询会自动更新状态徽标
export async function retryInfer(rel) {
  if (inferPosting) return;
  inferPosting = true;
  try {
    try {
      await api('/api/infer', { method: 'POST', body: { rels: [rel] } });
      toast('已重新提交推理:' + rel);
      pollJobs().then(schedulePoll);
    } catch (e) {
      toast('重试提交失败(' + e.status + '):' + e.message, 'err');
    }
  } finally {
    inferPosting = false;
  }
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
export async function pollJobs() {
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

// 轮询调度:有活动任务(运行/排队)1.5s,空闲 5s;页面隐藏时完全暂停
const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 5000;
let pollTimer = null;

function hasActiveJobs() {
  return state.jobs.some(j => j.status === 'running' || j.status === 'queued');
}

export function schedulePoll() {
  clearTimeout(pollTimer);
  if (document.hidden) return; // 页面隐藏:不再排程
  pollTimer = setTimeout(async () => {
    await pollJobs();
    schedulePoll(); // 用最新任务状态决定下一次间隔
  }, hasActiveJobs() ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(pollTimer); // 隐藏即暂停
  } else {
    pollJobs().then(schedulePoll); // 恢复可见:立即轮询一次再排程
  }
});
