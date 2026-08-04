/* ================================================================
   在线状态(presence)
   契约:
     POST /api/presence {viewing: rel|null, editing: rel|null}  — 每 10s 上报
     GET  /api/presence → [{username, viewing, editing}](含自己,渲染时过滤)
     SSE  'presence' 事件 → {"roster": [...]}(名册变化时推送)
   名册刷新:非 mock 模式由 SSE 'presence' 事件推送(以事件为准);
   mock 模式无 SSE,仍走 10s GET 轮询。10s POST 心跳两种模式都保留
   (留在名册里的必要动作);心跳响应不用于渲染。
   徽章渲染:tree.js 视频行 / dashboard.js 行用 presenceBadgeHtml(rel)。
   名册为模块内状态(state.js 不在本包改动范围)。
   ================================================================ */
import { esc } from './util.js';
import { MOCK, state } from './state.js';
import { api } from './api.js';
import { subscribe } from './events.js';
import { getMe } from './auth.js';
import { sftSignature } from './sft_model.js';
import { icon } from './icons.js';

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

// 名册归一化:兼容数组 / {users:[...]} / SSE 的 {roster:[...]};username 缺省时取 user 字段
function normalizeRoster(data) {
  return (Array.isArray(data) ? data : (data && (data.users || data.roster)) || [])
    .map(u => (u && typeof u === 'object' ? { ...u, username: u.username || u.user } : u))
    .filter(u => u && u.username);
}

// mock 模式专用:10s GET 轮询名册;名册变化返回 true(调用方据此刷新徽章)
export async function pollPresence() {
  try {
    const users = normalizeRoster(await api('/api/presence'));
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
        + esc(u.username) + ' 正在编辑">' + icon('edit', 11) + ' ' + esc(u.username) + '</span>';
    } else if (u.viewing === rel) {
      html += '<span class="presence-badge presence-viewing" title="'
        + esc(u.username) + ' 正在查看">' + esc(u.username) + '</span>';
    }
  });
  return html;
}

// 每 10s POST 上报 viewing/editing(留在名册里的必要动作);页面隐藏时暂停。
// 名册刷新:非 mock 由 SSE 'presence' 事件推送(事件带全量名册,以事件为准,
// 心跳响应不参与渲染);mock 模式仍走 10s GET 轮询。
// 名册变化后的徽章重渲染:侧栏由 main.js 订阅 'presence' 触发 renderSidebar,
// 看板由 dashboard.js 自行订阅重渲染表格
export function startPresence() {
  if (timer) return;
  postPresence();
  if (!MOCK) {
    subscribe('presence', data => { roster = normalizeRoster(data); });
    timer = setInterval(() => {
      if (document.hidden) return;
      postPresence();
    }, PRESENCE_INTERVAL_MS);
    return;
  }
  pollPresence();
  timer = setInterval(() => {
    if (document.hidden) return;
    postPresence();
    pollPresence();
  }, PRESENCE_INTERVAL_MS);
}
