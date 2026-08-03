/* ================================================================
   数据看板(整页视图):GT vs 模型检出的逐视频一致性 + 审核状态总览
   契约:
     GET /api/dashboard → { summary, event_names, metrics }
     GET /api/dashboard/rows?page=1&size=50&consistency=diff,no_gt
         &review=confirmed,unconfirmed&edited=1&q=词
         → { rows, page, size, total, total_pages }
     PUT /api/dashboard/review { stem, status }
   视图协作:openDashboard() 置 state.view='dashboard' 并整页替换 #main;
   selectVideo/renderWelcome 经 runCleanups() 触发此处登记的清理,把
   state.view 切回 'detail'。轮询由 main.js 每 1.5s 调 dashboardTick()。
   筛选(一致性/审核/人工已改/名称搜索)与翻页均走服务端:
   变更即 fetchRows(page=1),当前筛选状态保存在本模块内。
   ================================================================ */
import { $, esc, toast } from './util.js';
import { state, runCleanups } from './state.js';
import { api } from './api.js';
import { selectVideo } from './preview.js';
import { presenceBadgeHtml, presenceUsers } from './presence.js';

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
const PAGE_SIZE = 50;          // 每页行数(契约 size 参数)
const SEARCH_DEBOUNCE_MS = 300; // 名称搜索防抖

let dashData = null;   // /api/dashboard 最近一次成功响应 {summary, event_names, metrics}
let dashSnap = '';     // 汇总快照:未变时跳过重渲染,防轮询闪烁
let fetching = false;  // 汇总请求去重
let rowsData = null;   // /api/dashboard/rows 最近一次成功响应 {rows, page, size, total, total_pages}
let rowsSnap = '';     // 明细快照(含 presence 名册,徽章变化同样触发重渲染)
let rowsFetching = false; // 明细请求去重
let curPage = 1;       // 当前页码(以服务端回包 page 为准)
let searchTimer = null;   // 搜索防抖定时器
const pendingReviews = new Set(); // 正在提交审核的 stem:轮询回包不回滚这些行
const filters = { consistency: new Set(), review: new Set(), editedOnly: false, name: '' };

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
  // 重置筛选/分页与快照,强制首轮全量渲染
  filters.consistency.clear(); filters.review.clear();
  filters.editedOnly = false; filters.name = '';
  curPage = 1;
  dashData = null; dashSnap = '';
  rowsData = null; rowsSnap = '';
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
      renderSummary();
      renderMetrics(data.metrics);
      renderFilters(data.summary || {});
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
  await fetchRows(curPage); // 明细行独立拉取(自身快照比对防闪烁)
}

/* ------------------------------------------------------------ 明细行:服务端分页 + 筛选 */
function rowsQuery(page) {
  const q = new URLSearchParams({ page: String(page), size: String(PAGE_SIZE) });
  if (filters.consistency.size) q.set('consistency', Array.from(filters.consistency).join(','));
  if (filters.review.size) q.set('review', Array.from(filters.review).join(','));
  if (filters.editedOnly) q.set('edited', '1');
  if (filters.name) q.set('q', filters.name);
  return '/api/dashboard/rows?' + q.toString();
}

async function fetchRows(page) {
  if (state.view !== 'dashboard' || !$('#dash-root') || rowsFetching) return;
  rowsFetching = true;
  setPagerBusy(true); // 加载期(大工作区可达 ~11s)禁用翻页并提示,防用户连点误判
  try {
    let data = await api(rowsQuery(page));
    if (state.view !== 'dashboard' || !$('#dash-root')) return;
    // 页码夹紧:请求页超出总页数(数据变少/筛选收窄)时回退最后一页重拉
    if (data.total_pages >= 1 && data.page > data.total_pages) {
      data = await api(rowsQuery(data.total_pages));
      if (state.view !== 'dashboard' || !$('#dash-root')) return;
    }
    // 用户正在点审核 chip 的行:保留本地乐观值,轮询回包不回滚
    if (pendingReviews.size && rowsData) {
      (data.rows || []).forEach(r => {
        if (pendingReviews.has(r.stem)) {
          const cur = rowsData.rows.find(x => x.stem === r.stem);
          if (cur) r.review = cur.review;
        }
      });
    }
    // presence 名册一并纳入快照:他人编辑/查看徽章变化时同样触发重渲染
    const snap = JSON.stringify([data, presenceUsers()]);
    if (snap !== rowsSnap) {
      rowsData = data;
      rowsSnap = snap;
      curPage = data.page;
      renderTable();
    }
  } catch (e) {
    if (!rowsData && $('#dash-body')) {
      $('#dash-body').innerHTML = '<div class="empty-note">明细加载失败:'
        + esc(e.message) + '</div>';
    }
    // 已有数据时静默保留下轮重试
  } finally {
    rowsFetching = false;
    setPagerBusy(false);
  }
}

// rowsFetching 期间翻页条两按钮 disabled + 「加载中…」提示;
// 恢复时按最新 rowsData 重算 disabled(不恢复旧值,避免页码已变的陈旧状态)。
function setPagerBusy(busy) {
  const hint = $('#dash-pager-busy');
  if (hint) hint.hidden = !busy;
  const prev = $('#dash-prev');
  const next = $('#dash-next');
  const d = rowsData;
  if (prev) prev.disabled = busy || !d || d.page <= 1;
  if (next) next.disabled = busy || !d || (d.total_pages || 0) < 1 || d.page >= d.total_pages;
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

// 页眉:「共 X 个视频 · 第 a-b 条 / 共 N 条」(区间按当前筛选后的 total)
function renderSummary() {
  const el = $('#dash-summary');
  if (!el) return;
  const s = (dashData && dashData.summary) || {};
  let text = '共 ' + (s.total != null ? s.total : 0) + ' 个视频';
  if (rowsData) {
    const total = rowsData.total || 0;
    const a = total ? (rowsData.page - 1) * rowsData.size + 1 : 0;
    const b = Math.min(total, rowsData.page * rowsData.size);
    text += ' · 第 ' + a + '-' + b + ' 条 / 共 ' + total + ' 条';
  }
  el.textContent = text;
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
  // 过滤条重建会替换搜索框:轮询重渲染时保住焦点与光标,不打断输入
  const searchHadFocus = document.activeElement && document.activeElement.id === 'dash-search';
  const hint = (filters.consistency.size || filters.review.size || filters.editedOnly || filters.name)
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
    + '<span class="dash-filter-sep"></span>'
    + '<input id="dash-search" class="dash-search" type="text" spellcheck="false"'
    + ' placeholder="搜索名称…" value="' + esc(filters.name) + '">'
    + hint;
  // chip 点击 → 更新模块内筛选状态 → 回到第 1 页重新向服务端请求
  el.querySelectorAll('.dash-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const g = btn.dataset.group;
      if (g === '__clear') {
        filters.consistency.clear(); filters.review.clear(); filters.editedOnly = false;
        filters.name = '';
      } else if (g === 'edited') {
        filters.editedOnly = !filters.editedOnly;
      } else {
        const set = filters[g];
        if (set.has(btn.dataset.key)) set.delete(btn.dataset.key); else set.add(btn.dataset.key);
      }
      curPage = 1;
      renderFilters((dashData && dashData.summary) || {}); // 就地刷新选中态,计数等下轮汇总
      fetchRows(1);
    });
  });
  // 名称搜索:300ms 防抖后触发服务端过滤;只重置表格,过滤条不重建,焦点不丢
  const search = $('#dash-search', el);
  search.addEventListener('input', () => {
    filters.name = search.value;
    curPage = 1;
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => fetchRows(1), SEARCH_DEBOUNCE_MS);
  });
  if (searchHadFocus) {
    search.focus();
    search.setSelectionRange(search.value.length, search.value.length);
  }
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

function pagerHtml(data) {
  const tp = data.total_pages || 0;
  return '<div class="dash-pager" id="dash-pager">'
    + '<button type="button" class="dash-chip" id="dash-prev"'
    + (data.page <= 1 ? ' disabled' : '') + '>上一页</button>'
    + '<span class="card-sub">第 ' + data.page + ' / ' + Math.max(tp, 1) + ' 页</span>'
    + '<button type="button" class="dash-chip" id="dash-next"'
    + (tp < 1 || data.page >= tp ? ' disabled' : '') + '>下一页</button>'
    + '<span class="card-sub" id="dash-pager-busy" hidden>加载中…</span>'
    + '</div>';
}

function bindPager(el, data) {
  const prev = $('#dash-prev', el);
  const next = $('#dash-next', el);
  // 点击时读最新 rowsData(轮询/筛选可能已更新页码),不用闭包里的旧 data,
  // 避免陈旧页码;rowsFetching 期间忽略点击(按钮已 disabled,双保险)。
  if (prev) prev.addEventListener('click', () => {
    const d = rowsData || data;
    if (!rowsFetching && d.page > 1) fetchRows(d.page - 1);
  });
  if (next) next.addEventListener('click', () => {
    const d = rowsData || data;
    if (!rowsFetching && d.page < (d.total_pages || 0)) fetchRows(d.page + 1);
  });
}

function bindReviewChips(root) {
  root.querySelectorAll('.dash-review-chip').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation(); // 审核点击不触发行跳转
      setReview(btn.dataset.stem, btn.dataset.status);
    });
  });
}

function renderTable() {
  const el = $('#dash-body');
  if (!el) return;
  const data = rowsData || { rows: [], page: 1, size: PAGE_SIZE, total: 0, total_pages: 0 };
  const rows = data.rows || [];
  renderSummary(); // 页眉「第 a-b 条 / 共 N 条」随行数据刷新
  if (!rows.length) {
    const wsEmpty = !((dashData && dashData.summary) || {}).total;
    el.innerHTML = '<div class="empty-note">'
      + (wsEmpty ? '工作区内暂无视频。' : '当前过滤条件下没有匹配的视频。')
      + '</div>' + pagerHtml(data);
    bindPager(el, data);
    return;
  }
  let html = '<div class="dash-table-wrap"><table class="dash-table dash-rows-table">'
    + '<thead><tr><th>视频</th><th>GT 事件</th><th>模型检出</th><th>一致性</th>'
    + '<th class="dash-col-edit">人工</th><th class="dash-col-review">审核</th>'
    + '<th class="dash-col-open"></th></tr></thead><tbody>';
  rows.forEach(r => {
    const hasGt = r.status !== 'no_gt';
    const hasPred = r.status !== 'no_results';
    // 一致性差异行:GT 缺失(漏检)/模型多余(误检)的事件 chip 用暖色,其余保持中性底
    const missIds = new Set(r.missing || []);
    const extraIds = new Set(r.extra || []);
    const evChip = (id, warm) => '<span class="dash-ev-chip' + (warm ? ' dash-ev-chip-warm' : '')
      + '">' + esc(eventName(id)) + '</span>';
    const gtCell = hasGt
      ? ((r.gt_ids || []).length
        ? r.gt_ids.map(id => evChip(id, missIds.has(id))).join('')
        : '<span class="dash-none">无事件</span>')
      : '<span class="dash-none">—</span>';
    const predCell = hasPred
      ? ((r.pred_ids || []).length
        ? r.pred_ids.map(id => evChip(id, extraIds.has(id))).join('')
        : '<span class="dash-none">无检出</span>')
      : '<span class="dash-none">—</span>';
    html += '<tr data-rel="' + esc(r.rel) + '">'
      + '<td class="dash-v" title="' + esc(r.rel) + '"><span>' + esc(r.rel) + '</span>'
      + presenceBadgeHtml(r.rel) + '</td>'
      + '<td>' + gtCell + '</td>'
      + '<td>' + predCell + '</td>'
      + '<td class="dash-nowrap">' + consistencyBadge(r) + '</td>'
      + '<td class="dash-nowrap">' + editedBadge(r) + '</td>'
      + '<td class="dash-nowrap"><span class="dash-review-group">' + reviewChips(r) + '</span></td>'
      + '<td class="dash-nowrap"><span class="dash-open">打开 →</span></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>' + pagerHtml(data);

  bindReviewChips(el);
  el.querySelectorAll('tr[data-rel]').forEach(tr => {
    tr.addEventListener('click', () => selectVideo(tr.dataset.rel));
  });
  bindPager(el, data);
}

/* ------------------------------------------------------------ 审核:乐观更新 + 失败回滚 */
function adjustSummary(key, delta) {
  if (!dashData) return;
  const s = dashData.summary || (dashData.summary = {});
  s[key] = Math.max(0, (s[key] || 0) + delta);
}

// 审核成功后只就地更新该行的 chip 组,不整页重拉
function updateReviewChipsDom(row) {
  const el = $('#dash-body');
  if (!el) return;
  const tr = Array.prototype.find.call(
    el.querySelectorAll('tr[data-rel]'), t => t.dataset.rel === row.rel);
  const group = tr && tr.querySelector('.dash-review-group');
  if (!group) { renderTable(); return; } // 行不在当前 DOM(异常),退化为整表重渲染
  group.innerHTML = reviewChips(row);
  bindReviewChips(group);
}

async function setReview(stem, status) {
  const row = ((rowsData && rowsData.rows) || []).find(r => r.stem === stem);
  if (!row || row.review === status) return;
  const prev = row.review;
  row.review = status;
  pendingReviews.add(stem);
  adjustSummary(prev, -1);
  adjustSummary(status, 1);
  // 快照与乐观值对齐,避免轮询回写同一数据时重渲染
  rowsSnap = JSON.stringify([rowsData, presenceUsers()]);
  dashSnap = JSON.stringify(dashData);
  renderFilters((dashData && dashData.summary) || {}); // 审核计数 chip 即时更新
  updateReviewChipsDom(row);
  try {
    await api('/api/dashboard/review', { method: 'PUT', body: { stem: stem, status: status } });
  } catch (e) {
    row.review = prev;
    adjustSummary(status, -1);
    adjustSummary(prev, 1);
    rowsSnap = JSON.stringify([rowsData, presenceUsers()]);
    dashSnap = JSON.stringify(dashData);
    renderFilters((dashData && dashData.summary) || {});
    updateReviewChipsDom(row);
    toast('审核状态保存失败:' + e.message, 'err');
  } finally {
    pendingReviews.delete(stem);
  }
}
