/* ================================================================
   认证:401 拦截 + 当前用户 + 工具栏用户区
   契约:
     GET  /api/auth/me      → {username, login_time, ip}(未登录 401)
     POST /api/auth/logout  → 任意 2xx
   mock 模式(?mock=1)无登录概念:不请求 /me、不显示用户区、401 不跳转。
   ================================================================ */
import { $ } from './util.js';
import { MOCK } from './state.js';
import { api } from './api.js';

// 当前登录用户(模块内状态;state.js 不在本包改动范围)
let currentMe = null;
export function getMe() {
  return currentMe;
}

// api.js 在收到 401 时调用:跳登录页(mock 模式跳过,避免开发演示被打断)
export function redirectToLogin() {
  if (MOCK) return;
  // 已在登录页(或直接打开 login.html)时不跳,防止循环
  if (location.pathname.indexOf('/login') === 0) return;
  location.href = '/login';
}

// 启动时取当前用户;401 由 api 层经 redirectToLogin 拦截,其余失败按未登录处理
async function fetchMe() {
  try {
    return await api('/api/auth/me');
  } catch (e) {
    return null;
  }
}

function fillPop(me) {
  $('#user-pop-name').textContent = me.username || me.name || '-';
  $('#user-pop-time').textContent = me.login_time || me.login_at || '-';
  $('#user-pop-ip').textContent = me.ip || me.client_ip || '-';
}

function closePop() {
  const pop = $('#user-pop');
  if (pop) pop.hidden = true;
}

// 工具栏右上角用户区:头像点击弹浮窗,点浮窗外关闭;mock 模式不显示
export async function initUserArea() {
  if (MOCK) return;
  const me = await fetchMe();
  if (!me) return; // 未登录(401 已被拦截跳走)或接口未就绪:保持隐藏
  currentMe = me;
  const area = $('#user-area');
  area.hidden = false;
  fillPop(me);

  $('#user-avatar').addEventListener('click', e => {
    e.stopPropagation();
    const pop = $('#user-pop');
    pop.hidden = !pop.hidden;
  });
  $('#user-pop').addEventListener('click', e => e.stopPropagation()); // 浮窗内点击不关闭
  document.addEventListener('click', closePop);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePop(); });

  $('#btn-logout').addEventListener('click', async () => {
    try {
      await api('/api/auth/logout', { method: 'POST' });
    } catch (e) { /* 登出失败也跳登录页,由服务端会话状态兜底 */ }
    location.href = '/login';
  });
}
