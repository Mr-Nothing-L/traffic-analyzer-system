/* ================================================================
   在线状态(presence)
   契约:
     POST /api/presence {viewing: rel|null, editing: rel|null}  — 每 10s 上报
     GET  /api/presence → [{username, viewing, editing}](含自己,渲染时过滤)
   GET 名册并入 jobs 轮询(jobs.js pollJobs 调 pollPresence);
   徽章渲染:tree.js 视频行 / dashboard.js 行用 presenceBadgeHtml(rel)。
   名册为模块内状态(state.js 不在本包改动范围)。
   ================================================================ */
import { esc } from './util.js';
import { state } from './state.js';
import { api } from './api.js';
import { getMe } from './auth.js';
import { sftSignature } from './sft_model.js';

const PRESENCE_INTERVAL_MS = 10000;
let roster = [];
let timer = null;

export function presenceUsers() {
  return roster;
}

// 编辑中 = 当前视频有未保存的 SFT / 证据修改
function editingRel() {
  const sftDirty = !!(state.sftDraft && sftSignature() !== state.sftSavedSig);
  return (state.evidenceDirty || sftDirty) ? state.currentRel : null;
}

async function postPresence() {
  try {
    await api('/api/presence', {
      method: 'POST',
      body: { viewing: state.currentRel, editing: editingRel() },
    });
  } catch (e) { /* 后端未就绪:静默,下个周期重试 */ }
}

// 供 jobs.js 轮询调用;名册变化返回 true(调用方据此刷新徽章)
export async function pollPresence() {
  try {
    const data = await api('/api/presence');
    const users = (Array.isArray(data) ? data : (data && data.users) || [])
      .map(u => (u && typeof u === 'object' ? { ...u, username: u.username || u.user } : u))
      .filter(u => u && u.username);
    if (JSON.stringify(users) === JSON.stringify(roster)) return false;
    roster = users;
    return true;
  } catch (e) {
    return false; // 静默保留下轮重试
  }
}

// 某视频 rel 的 presence 徽章 HTML:编辑中=橙底用户名,查看中=灰底;不显示自己
export function presenceBadgeHtml(rel) {
  if (!rel || !roster.length) return '';
  const me = getMe();
  const myName = me && (me.username || me.name);
  let html = '';
  roster.forEach(u => {
    if (myName && u.username === myName) return;
    if (u.editing === rel) {
      html += '<span class="presence-badge presence-editing" title="'
        + esc(u.username) + ' 正在编辑">✎ ' + esc(u.username) + '</span>';
    } else if (u.viewing === rel) {
      html += '<span class="presence-badge presence-viewing" title="'
        + esc(u.username) + ' 正在查看">' + esc(u.username) + '</span>';
    }
  });
  return html;
}

// 每 10s 上报一次并同步拉取名册;页面隐藏时暂停(与 jobs 轮询同策略)。
// 名册变化后徽章随下一轮 renderSidebar/dashboardTick 快照比对自动刷新
export function startPresence() {
  if (timer) return;
  postPresence();
  pollPresence();
  timer = setInterval(() => {
    if (document.hidden) return;
    postPresence();
    pollPresence();
  }, PRESENCE_INTERVAL_MS);
}
