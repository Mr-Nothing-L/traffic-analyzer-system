/* ================================================================
   数据看板(整页视图):GT vs 模型检出的逐视频一致性 + 审核状态总览
   契约:
     GET /api/dashboard → { rows, summary, event_names, metrics }
     PUT /api/dashboard/review { stem, status }
   视图协作:openDashboard() 置 state.view='dashboard' 并整页替换 #main;
   selectVideo/renderWelcome 经 runCleanups() 触发此处登记的清理,把
   state.view 切回 'detail'。轮询由 main.js 每 1.5s 调 dashboardTick()。
   ================================================================ */
import { $, esc, toast } from './util.js';
import { state, runCleanups } from './state.js';
import { api } from './api.js';
import { selectVideo } from './preview.js';

const CONSISTENCY = [
  { key: 'consistent', label: '一致', cls: 'ok' },
  { key: 'diff', label: '有差异', cls: 'warn' },
  { key: 'no_gt', label: '无 GT', cls: 'mute' },
  { key: 'no_results', label: '未推理', cls: 'mute' },
];
const REVIEWS = [
  { key: 'unconfirmed', label: '未确认', cls: 'mute' },
  { key: 'confirmed', label: '已确认', cls: 'ok' },
  { key: 'needs_review', label: '需复核', cls: 'warn' },
];

let dashData = null;   // /api/dashboard 最近一次成功响应
let dashSnap = '';     // 数据快照:未变时跳过重渲染,防轮询闪烁
let fetching = false;  // 请求去重
const filters = { consistency: new Set(), review: new Set(), editedOnly: false };

/* ------------------------------------------------------------ 进入 / 轮询 */
export async function openDashboard() {
  runCleanups();
  state.view = 'dashboard';
  // 看板被 selectVideo/renderWelcome 整页替换时,经 cleanups 把视图态切回 detail
  state.cleanups.push(() => { if (state.view === 'dashboard') state.view = 'detail'; });
  const main = $('#main');
  delete main.dataset.renderedStem; // 离开分析视图,下次 renderResults 需整体重建
  main.innerHTML =
    '<div class="dash" id="dash-root">'
    + '<div class="cards">'
    + '<div class="card"><div class="card-head"><span class="card-title">精度指标</span>'
    + '<span class="card-sub">按事件类别统计</span></div>'
    + '<div class="card-body" id="dash-metrics"></div></div>'
    + '<div class="card"><div class="card-head"><span class="card-title">逐视频明细</span>'
    + '<span class="card-sub" id="dash-summary"></span>'
    + '<span class="spacer"></span>'
    + '<span class="card-sub">点击行跳回详情视图</span></div>'
    + '<div class="dash-filters" id="dash-filters"></div>'
    + '<div class="card-body dash-body" id="dash-body">'
    + '<div class="empty-note">看板数据加载中…</div></div></div>'
    + '</div></div>';
  dashSnap = ''; // 强制下一次 tick 重渲染
  await dashboardTick();
}

// 供 main.js 轮询调用;仅看板激活时拉取,快照比对后再重渲染
export async function dashboardTick() {
  if (state.view !== 'dashboard' || !$('#dash-root') || fetching) return;
  fetching = true;
  try {
    const data = await api('/api/dashboard');
    if (state.view !== 'dashboard' || !$('#dash-root')) return; // 期间已离开看板
    const snap = JSON.stringify(data);
    if (snap !== dashSnap) {
      dashData = data;
      dashSnap = snap;
      renderBody();
    }
  } catch (e) {
    if (!dashData && $('#dash-body')) {
      $('#dash-body').innerHTML = '<div class="empty-note">看板数据加载失败:'
        + esc(e.message) + '</div>';
    }
    // 已有数据时静默保留下轮重试,不打断阅读
  } finally {
    fetching = false;
  }
}

/* ------------------------------------------------------------ 渲染 */
function eventName(id) {
  const names = (dashData && dashData.event_names) || {};
  return names[id] != null ? String(names[id]) : String(id);
}

function fmtNum(v) {
  if (v == null || isNaN(v)) return '-';
  return typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(3) : String(v);
}

function namesText(ids) {
  return (ids || []).map(eventName).join('、');
}

function renderBody() {
  const d = dashData || {};
  renderSummary(d.summary || {});
  renderMetrics(d.metrics);
  renderFilters(d.summary || {});
  renderTable(d.rows || []);
}

function renderSummary(s) {
  const el = $('#dash-summary');
  if (!el) return;
  el.textContent = '共 ' + (s.total != null ? s.total : 0) + ' 个视频';
}

function renderMetrics(m) {
  const el = $('#dash-metrics');
  if (!el) return;
  const per = (m && m.per_event) || [];
  if (!per.length) {
    el.innerHTML = '<div class="empty-note">尚无精度指标,完成评估后此处展示。</div>';
    return;
  }
  let html = '<div class="dash-table-wrap"><table class="dash-table dash-metrics-table">'
    + '<thead><tr><th>事件</th><th>TP</th><th>FP</th><th>FN</th>'
    + '<th>精确率</th><th>召回率</th><th>F1</th></tr></thead><tbody>';
  per.forEach(e => {
    html += '<tr><td>' + esc(e.name || eventName(e.event_id)) + '</td>'
      + '<td class="num">' + esc(fmtNum(e.tp)) + '</td>'
      + '<td class="num">' + esc(fmtNum(e.fp)) + '</td>'
      + '<td class="num">' + esc(fmtNum(e.fn)) + '</td>'
      + '<td class="num">' + esc(fmtNum(e.precision)) + '</td>'
      + '<td class="num">' + esc(fmtNum(e.recall)) + '</td>'
      + '<td class="num">' + esc(fmtNum(e.f1)) + '</td></tr>';
  });
  [['宏平均', m.macro], ['微平均', m.micro]].forEach(([label, avg]) => {
    avg = avg || {};
    html += '<tr class="total"><td>' + label + '</td>'
      + '<td class="num">' + esc(fmtNum(avg.tp)) + '</td>'
      + '<td class="num">' + esc(fmtNum(avg.fp)) + '</td>'
      + '<td class="num">' + esc(fmtNum(avg.fn)) + '</td>'
      + '<td class="num">' + esc(fmtNum(avg.precision)) + '</td>'
      + '<td class="num">' + esc(fmtNum(avg.recall)) + '</td>'
      + '<td class="num">' + esc(fmtNum(avg.f1)) + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

function chipHtml(group, key, label, cls, count) {
  const on = group === 'edited' ? filters.editedOnly : filters[group].has(key);
  return '<button type="button" class="dash-chip dash-chip-' + cls + (on ? ' on' : '')
    + '" data-group="' + group + '" data-key="' + key + '">'
    + esc(label) + (count != null ? ' <b>' + esc(String(count)) + '</b>' : '') + '</button>';
}

function renderFilters(s) {
  const el = $('#dash-filters');
  if (!el) return;
  const hint = (filters.consistency.size || filters.review.size || filters.editedOnly)
    ? '<button type="button" class="dash-chip dash-chip-clear" data-group="__clear">清除过滤</button>'
    : '';
  el.innerHTML =
    '<span class="dash-filter-label">一致性</span>'
    + CONSISTENCY.map(c => chipHtml('consistency', c.key, c.label, c.cls, s[c.key])).join('')
    + '<span class="dash-filter-sep"></span>'
    + '<span class="dash-filter-label">审核</span>'
    + REVIEWS.map(r => chipHtml('review', r.key, r.label, r.cls, s[r.key])).join('')
    + '<span class="dash-filter-sep"></span>'
    + chipHtml('edited', 'edited', '人工已改', 'edit', s.edited)
    + hint;
  el.querySelectorAll('.dash-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const g = btn.dataset.group;
      if (g === '__clear') {
        filters.consistency.clear(); filters.review.clear(); filters.editedOnly = false;
      } else if (g === 'edited') {
        filters.editedOnly = !filters.editedOnly;
      } else {
        const set = filters[g];
        if (set.has(btn.dataset.key)) set.delete(btn.dataset.key); else set.add(btn.dataset.key);
      }
      renderBody(); // 纯本地过滤,无需重新拉取
    });
  });
}

function rowVisible(r) {
  if (filters.consistency.size && !filters.consistency.has(r.status)) return false;
  if (filters.review.size && !filters.review.has(r.review)) return false;
  if (filters.editedOnly && !r.edited) return false;
  return true;
}

function consistencyBadge(r) {
  if (r.status === 'consistent') return '<span class="dash-badge dash-badge-ok">一致</span>';
  if (r.status === 'diff') {
    return '<span class="dash-badge dash-badge-warn">有差异</span>'
      + '<span class="dash-diff-detail">漏:' + (r.missing || []).length
      + ';误:' + (r.extra || []).length + '</span>';
  }
  if (r.status === 'no_gt') return '<span class="dash-badge dash-badge-mute">无 GT</span>';
  return '<span class="dash-badge dash-badge-mute">未推理</span>';
}

function editedBadge(r) {
  if (!r.edited) return '';
  const raw = namesText(r.pred_raw_ids) || '(空)';
  const cur = namesText(r.pred_ids) || '(空)';
  let title = '原始检出:「' + raw + '」→ 现在:「' + cur + '」';
  if ((r.edit_extra || []).length) title += ';人工补充:「' + namesText(r.edit_extra) + '」';
  if ((r.edit_missing || []).length) title += ';人工删除:「' + namesText(r.edit_missing) + '」';
  return '<span class="dash-badge dash-badge-edit" title="' + esc(title) + '">人工已改</span>';
}

function reviewChips(r) {
  return REVIEWS.map(v =>
    '<button type="button" class="dash-review-chip dash-chip-' + v.cls
    + (r.review === v.key ? ' on' : '')
    + '" data-stem="' + esc(r.stem) + '" data-status="' + v.key + '" title="标记为「' + v.label + '」">'
    + v.label + '</button>').join('');
}

function renderTable(rows) {
  const el = $('#dash-body');
  if (!el) return;
  const visible = rows.filter(rowVisible);
  if (!visible.length) {
    el.innerHTML = '<div class="empty-note">'
      + (rows.length ? '当前过滤条件下没有匹配的视频。' : '工作区内暂无视频。')
      + '</div>';
    return;
  }
  let html = '<div class="dash-table-wrap"><table class="dash-table dash-rows-table">'
    + '<thead><tr><th>视频</th><th>GT 事件</th><th>模型检出</th><th>一致性</th>'
    + '<th class="dash-col-edit">人工</th><th class="dash-col-review">审核</th>'
    + '<th class="dash-col-open"></th></tr></thead><tbody>';
  visible.forEach(r => {
    const hasGt = r.status !== 'no_gt';
    const hasPred = r.status !== 'no_results';
    const gtCell = hasGt
      ? ((r.gt_ids || []).length
        ? r.gt_ids.map(id => '<span class="dash-ev-chip">' + esc(eventName(id)) + '</span>').join('')
        : '<span class="dash-none">无事件</span>')
      : '<span class="dash-none">—</span>';
    const predCell = hasPred
      ? ((r.pred_ids || []).length
        ? r.pred_ids.map(id => '<span class="dash-ev-chip">' + esc(eventName(id)) + '</span>').join('')
        : '<span class="dash-none">无检出</span>')
      : '<span class="dash-none">—</span>';
    html += '<tr data-rel="' + esc(r.rel) + '">'
      + '<td class="dash-v" title="' + esc(r.rel) + '"><span>' + esc(r.rel) + '</span></td>'
      + '<td>' + gtCell + '</td>'
      + '<td>' + predCell + '</td>'
      + '<td class="dash-nowrap">' + consistencyBadge(r) + '</td>'
      + '<td class="dash-nowrap">' + editedBadge(r) + '</td>'
      + '<td class="dash-nowrap"><span class="dash-review-group">' + reviewChips(r) + '</span></td>'
      + '<td class="dash-nowrap"><span class="dash-open">打开 →</span></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';

  el.querySelectorAll('.dash-review-chip').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation(); // 审核点击不触发行跳转
      setReview(btn.dataset.stem, btn.dataset.status);
    });
  });
  el.querySelectorAll('tr[data-rel]').forEach(tr => {
    tr.addEventListener('click', () => selectVideo(tr.dataset.rel));
  });
}

/* ------------------------------------------------------------ 审核:乐观更新 + 失败回滚 */
function adjustSummary(key, delta) {
  const s = dashData.summary || (dashData.summary = {});
  s[key] = Math.max(0, (s[key] || 0) + delta);
}

async function setReview(stem, status) {
  const row = (dashData.rows || []).find(r => r.stem === stem);
  if (!row || row.review === status) return;
  const prev = row.review;
  row.review = status;
  adjustSummary(prev, -1);
  adjustSummary(status, 1);
  dashSnap = JSON.stringify(dashData); // 快照与乐观值对齐,避免轮询回写同一数据时重渲染
  renderBody();
  try {
    await api('/api/dashboard/review', { method: 'PUT', body: { stem: stem, status: status } });
  } catch (e) {
    row.review = prev;
    adjustSummary(status, -1);
    adjustSummary(prev, 1);
    dashSnap = JSON.stringify(dashData);
    renderBody();
    toast('审核状态保存失败:' + e.message, 'err');
  }
}
