/* ================================================================
   动作:推理
   ================================================================ */
import { toast } from './util.js';
import { MOCK, state } from './state.js';
import { api } from './api.js';
import { subscribe } from './events.js';
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
      pollJobs().then(schedulePoll); // 提交后立即拉一次对齐;后续进度由 SSE 推送(mock 模式由轮询接力)
    } catch (e) {
      toast('推理提交失败(' + e.status + '):' + e.message, 'err');
    }
    syncButtons();
  } finally {
    inferPosting = false;
  }
}

// 失败任务重试:仅对该视频重新 POST /api/infer,SSE/轮询会自动更新状态徽标
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
   任务同步:进入页面一次性拉全量 /api/jobs,之后由 SSE 事件增量更新
   (events.js 全局共享连接);mock 模式无 SSE,仍由 mockTick + 轮询驱动
   ================================================================ */
// 当前视图是否存在未保存的 SFT / 证据编辑
function hasUnsavedEdits() {
  if (state.evidenceDirty) return true;
  if (state.sftDraft && sftSignature() !== state.sftSavedSig) return true;
  return false;
}

// 状态转移检测 + 渲染触发:全量首拉与 SSE 增量共用同一路径
async function syncJobs() {
  const prev = state.prevJobStatus;
  const next = {};
  let needVideos = false;
  let reloadRel = null;
  state.jobs.forEach(j => {
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
}

// 一次性全量首拉(进入页面/提交推理后调用);之后靠 SSE 事件增量
let polling = false;
export async function pollJobs() {
  if (polling) return;
  polling = true;
  try {
    state.jobs = await api('/api/jobs');
    await syncJobs();
  } catch (e) {
    // 后端未启动等场景:静默,交由 SSE 重连或下一轮 mock 轮询对齐
  } finally {
    polling = false;
  }
}

// SSE 快照落库:按 id 更新/插入;快照不含 log_tail,原地合并以保留本地已有日志。
// cancel/timeout 竞态下 job.progress 可能晚于 job.done 到达一次:
// 本地已是终态(done/failed)时忽略迟到的非终态快照,按 status 过滤
function onJobEvent(job) {
  if (!job || job.id == null) return;
  const cur = state.jobs.find(j => j.id === job.id);
  if (cur && (cur.status === 'done' || cur.status === 'failed')
      && job.status !== 'done' && job.status !== 'failed') return;
  if (cur) Object.assign(cur, job); else state.jobs.push(job);
  syncJobs();
}

// 订阅 job.progress / job.done;断线重连由 EventSource 自带,连接挂全局(见 events.js)
export function startJobEvents() {
  if (MOCK) return; // mock 模式由 mockTick + 轮询驱动,EventSource 无法被 mock 拦截
  subscribe('job.progress', onJobEvent);
  subscribe('job.done', onJobEvent);
}

// mock 模式轮询调度:有活动任务(运行/排队)1.5s,空闲 5s;页面隐藏时完全暂停
// 非 mock 模式不使用(SSE 事件驱动),调用方无需区分
const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 5000;
let pollTimer = null;

function hasActiveJobs() {
  return state.jobs.some(j => j.status === 'running' || j.status === 'queued');
}

export function schedulePoll() {
  if (!MOCK) return; // 非 mock:SSE 取代轮询
  clearTimeout(pollTimer);
  if (document.hidden) return; // 页面隐藏:不再排程
  pollTimer = setTimeout(async () => {
    await pollJobs();
    schedulePoll(); // 用最新任务状态决定下一次间隔
  }, hasActiveJobs() ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

if (MOCK) {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(pollTimer); // 隐藏即暂停
    } else {
      pollJobs().then(schedulePoll); // 恢复可见:立即轮询一次再排程
    }
  });
}
