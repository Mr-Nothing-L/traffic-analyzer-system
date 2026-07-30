/* ================================================================
   动作:推理
   ================================================================ */
import { toast } from './util.js';
import { state } from './state.js';
import { api } from './api.js';
import { loadTree, renderSidebar, syncButtons } from './tree.js';
import { selectVideo } from './preview.js';
import { sftSignature } from './sft_model.js';

let inferPosting = false; // 提交防抖:双击时第二次直接忽略,与按钮 disabled 无关
export async function startInfer() {
  const rels = state.videos.filter(v => state.checked.has(v.rel)).map(v => v.rel);
  if (!rels.length || inferPosting) return;
  inferPosting = true;
  try {
    try {
      await api('/api/infer', { method: 'POST', body: { rels: rels } });
      toast('已提交 ' + rels.length + ' 个推理任务');
      selectVideo(rels[0]); // 自动切到第一个视频,预览区立即呈现推理动效
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

// 停止推理:调用后端取消端点;成功后本地立即把任务标记为失败态,
// 避免专家面板/侧栏在下次轮询对齐前闪跳;prevJobStatus 同步置位,跳过「推理失败」toast(用户主动停止)
export async function cancelJob(id) {
  try {
    await api('/api/jobs/' + encodeURIComponent(id) + '/cancel', { method: 'POST' });
  } catch (e) {
    toast('停止推理失败(' + e.status + '):' + e.message, 'err');
    return;
  }
  const jid = Number(id); // tree 的 data-stop 是字符串、preview 传数字,统一归一
  // 无条件记录:抑制紧随的轮询把「用户主动停止」报成「推理失败」toast。
  // 不能放在下面的条件块里——await 期间本地 job 对象可能已被并发轮询/mock 改写。
  state.prevJobStatus[jid] = 'failed';
  const job = state.jobs.find(j => j.id === jid);
  if (job && (job.status === 'running' || job.status === 'queued')) {
    job.status = 'failed';
    job.returncode = -15;
    job.progress = Object.assign({}, job.progress, { step_label: '已停止' });
  }
  toast('已请求停止推理');
  renderSidebar();
  syncButtons();
  pollJobs().then(schedulePoll); // 立即轮询一次,尽快与后端真实状态对齐
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
      // 当前选中视频的任务 queued→running:重渲染主区,让专家工作间面板补上
      if (j.kind === 'infer' && j.status === 'running' && was && was !== j.status
          && (j.rel || j.stem) === state.currentRel && !reloadRel) {
        reloadRel = j.rel || j.stem;
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
