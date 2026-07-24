/* ------------------------------------------------------------ SFT 卡 */
import { $, $$, esc, toast, flashSaveBtn } from './util.js';
import { state } from './state.js';
import { api } from './api.js';

// event_id 全局采用标注文档 v4.5 的 action 编号(9 = 正常占位),action / classN 直接等于 event_id

// ---------------------------------------------------------------------------
// 结构化选项(chips):封闭枚举,只读选项集,三层联动
//   chips(选中态) → 骨架句(skeleton 按模板重算,空值从句省略) → 文本框
//   (骨架前缀替换,找不到则前置插入;细节文本始终保留;文本框可自由编辑,
//   chips 不从手动编辑回写)
// ---------------------------------------------------------------------------

// 骨架模板:字符串为固定文字;{slot, pre, post} 为该属性有值时输出的从句(空值整句省略)
const SFT_SKELETON_TEMPLATES = {
  1: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, '停有一辆', { slot: 'vehicle_type' }],
  2: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, { slot: 'vehicle_type', pre: '有', post: '占用' }],
  3: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, '发生交通事故', { slot: 'vehicle_type', pre: ',涉及' }],
  4: [{ slot: 'direction', post: '一侧' }, '出现', { slot: 'person_type' }],
  5: [{ slot: 'direction', post: '一侧' }, '出现', { slot: 'non_motor_type' }],
  6: [{ slot: 'direction', post: '一侧' }, '出现', { slot: 'scope' }, '拥堵'],
  7: [{ slot: 'direction', post: '一侧' }, '道路施工', { slot: 'work_elements', pre: ',现场有' }],
  8: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, { slot: 'vehicle_type', pre: '有', post: '逆行' }],
};

// 旧样本回填:选项关键词别名(选项文本本身总是首个关键词);按 options 顺序首个命中
const SFT_ATTR_ALIASES = {
  '行车道': ['主车道'],
  '来向': ['对向'],
  '小型车': ['小车', '轿车', '私家车'],
  '大客车': ['客车', '大巴'],
  '货车': ['卡车'],
  '工程车': ['施工车', '清障车', '救援车'],
  '施工人员': ['工人'],
  '滞留驾乘人员': ['驾乘人员', '滞留人员'],
  '摩托车': ['摩托'],
  '电动自行车': ['电动车', '电瓶车'],
  '单车道': ['一条车道'],
  '多车道': ['多条车道', '全部车道'],
  '施工车辆': ['工程车', '施工车'],
  '交通锥/隔离栏': ['交通锥', '锥桶', '路锥', '隔离栏', '锥形桶'],
  '施工标志牌': ['施工标志', '标志牌', '施工牌'],
  '车道封闭': ['封闭车道', '封路'],
  '塑料袋/纸张': ['塑料袋', '纸张', '塑料'],
  '水瓶/容器': ['水瓶', '瓶子'],
  '木板/构件': ['木板', '木条'],
  '泥土/散落物': ['泥土', '散落物', '碎石'],
  '三角警示牌': ['三角牌'],
};

// 事件的结构化属性组(event_options.yaml,经 /api/config/events 下发);旧版后端无该字段时回退空
function evOptions(ev) {
  return Array.isArray(ev.options) ? ev.options : [];
}

// 骨架句:按模板把已选属性拼成一句,空值从句整体省略
function skeleton(ev, attrs) {
  const parts = SFT_SKELETON_TEMPLATES[ev.event_id];
  if (!parts) return '';
  attrs = attrs || {};
  let out = '';
  parts.forEach(p => {
    if (typeof p === 'string') { out += p; return; }
    const v = attrs[p.slot];
    if (Array.isArray(v)) {
      if (v.length) out += (p.pre || '') + v.join('、') + (p.post || '');
    } else if (v) {
      out += (p.pre || '') + v + (p.post || '');
    }
  });
  return out;
}

// 旧样本(无 event_attributes)按关键词回猜 chips;原文不动
function guessAttrsFromText(ev, text) {
  const attrs = {};
  const t = String(text || '');
  evOptions(ev).forEach(g => {
    const hit = [];
    g.options.forEach(opt => {
      const kws = [opt].concat(SFT_ATTR_ALIASES[opt] || []);
      if (kws.some(k => k && t.indexOf(k) >= 0)) hit.push(opt);
    });
    if (g.multi) { if (hit.length) attrs[g.key] = hit; }
    else if (hit.length) attrs[g.key] = hit[0];
  });
  return attrs;
}

// 新格式:event_attributes 只保留当前选项定义内合法的键值(防御旧配置漂移)
function sanitizeFileAttrs(ev, raw) {
  const attrs = {};
  if (!raw || typeof raw !== 'object') return attrs;
  evOptions(ev).forEach(g => {
    const v = raw[g.key];
    if (g.multi) {
      if (Array.isArray(v)) {
        const ok = g.options.filter(o => v.indexOf(o) >= 0);
        if (ok.length) attrs[g.key] = ok;
      }
    } else if (typeof v === 'string' && g.options.indexOf(v) >= 0) {
      attrs[g.key] = v;
    }
  });
  return attrs;
}

// 必填缺失(软提醒,不拦截保存):返回缺失属性组的中文名
function missingRequired(ev, attrs) {
  const miss = [];
  evOptions(ev).forEach(g => {
    if (!g.required) return;
    const v = (attrs || {})[g.key];
    if (Array.isArray(v) ? !v.length : !v) miss.push(g.label);
  });
  return miss;
}

// 检出 + 必填缺失 → 事件名旁黄色圆点(hover 显示缺哪项)
function refreshWarnDots(body) {
  const d = state.sftDraft;
  if (!d) return;
  (state.eventConfig || []).forEach(ev => {
    const dot = $('[data-ev-warn="' + ev.event_id + '"]', body);
    if (!dot) return;
    const miss = d.checks[ev.event_id] ? missingRequired(ev, d.attrs[ev.event_id]) : [];
    dot.hidden = !miss.length;
    dot.title = miss.length ? '缺少:' + miss.join('、') : '';
  });
}

// chip 变更 → 重算骨架 → 骨架前缀在文本中则替换,否则前置插入(细节保留);同步文本框/dirty
function applyChipChange(body, ev, group, value) {
  const d = state.sftDraft;
  const id = ev.event_id;
  const attrs = d.attrs[id] || (d.attrs[id] = {});
  if (group.multi) {
    const cur = Array.isArray(attrs[group.key]) ? attrs[group.key] : [];
    const next = cur.indexOf(value) >= 0 ? cur.filter(o => o !== value) : cur.concat(value);
    attrs[group.key] = group.options.filter(o => next.indexOf(o) >= 0); // 保持 options 定义顺序
  } else {
    attrs[group.key] = attrs[group.key] === value ? '' : value;
  }
  const oldSk = d.skeletons[id] || '';
  const newSk = skeleton(ev, attrs);
  d.skeletons[id] = newSk;
  let text = String(d.texts[id] || '');
  if (oldSk && text.indexOf(oldSk) === 0) {
    text = newSk + text.slice(oldSk.length);
  } else if (newSk && newSk !== oldSk) {
    text = text ? newSk + ';' + text : newSk;
  }
  d.texts[id] = text;
  const ta = $('textarea[data-ev-text="' + id + '"]', body);
  if (ta) {
    ta.value = text;
    autoGrow(ta);
    ta.classList.remove('sft-fade'); // 重新触发动画
    void ta.offsetWidth;
    ta.classList.add('sft-fade');
  }
  const cur = attrs[group.key];
  $$('[data-ev-chip="' + id + '"][data-attr="' + group.key + '"]', body).forEach(c => {
    c.classList.toggle('selected', Array.isArray(cur) ? cur.indexOf(c.dataset.value) >= 0 : cur === c.dataset.value);
  });
  refreshWarnDots(body);
  updateSftDirty();
}

// 解析 description:think 按空行分段,匹配「事件名：」前缀;answer 提取天气/时间/场景键值
function parseSftDescription(desc, events) {
  const sections = {};   // event_id -> 段落正文(去掉「事件名：」前缀)
  const unmatched = [];  // 匹配不到任何事件名的段落(原样保留,保存时回写)
  const env = { '天气': '', '时间': '', '场景': '' };  // 天气/时间/场景键值(答案区可编辑)
  const thinkM = String(desc || '').match(/<think>([\s\S]*?)<\/think>/);
  if (thinkM) {
    thinkM[1].trim().split(/\n\s*\n/).forEach(para => {
      const p = para.trim();
      if (!p) return;
      const m = p.match(/^([^：\n]{1,30})：/);
      const ev = m ? events.find(e => e.name_zh === m[1]) : null;
      if (ev && ev.is_active && sections[ev.event_id] === undefined) {
        sections[ev.event_id] = p.slice(m[0].length).trim();
      } else if (ev) {
        // 未激活事件的模型原文不展示、保存时丢弃;重复段落同样丢弃
      } else {
        unmatched.push(p);
      }
    });
  }
  const answerM = String(desc || '').match(/<answer>([\s\S]*?)<\/answer>/);
  if (answerM) {
    answerM[1].split('\n').forEach(line => {
      const m = line.trim().match(/^(天气|时间|场景)\s*[:：]\s*(.*)$/);
      if (m) env[m[1]] = m[2];
    });
  }
  return { sections: sections, unmatched: unmatched, env: env };
}

// 天气/时间/场景按固定顺序重建为 answer 行;空值回退「未知」(与 core/sft_label_rewrite.py 口径一致)
function sftEnvLines() {
  const env = (state.sftDraft && state.sftDraft.env) || {};
  return ['天气', '时间', '场景'].map(k => {
    const v = String(env[k] || '').trim();
    return k + '：' + (v || '未知');
  });
}

// 由当前「检出」勾选生成结论行(保存与只读预览共用同一口径)
function sftConclusionLines() {
  const d = state.sftDraft;
  const events = state.eventConfig || [];
  const checked = events.filter(ev => d.checks[ev.event_id]);
  if (!checked.length) return ['最终结论：本视频块未检出任何事件,交通状况正常。'];
  const lines = ['最终结论：本视频块检出以下事件。'];
  checked.forEach(ev => {
    lines.push('class' + ev.event_id + ': ' + ev.name_zh);
  });
  return lines;
}

// 由当前草稿重建 description 与 action(结论区按「检出」勾选重建)
// event_attributes 由 chips 生成:仅保留非空键;全部为空时输出 null(后端落盘时省略该字段)
function buildSftRevision() {
  const d = state.sftDraft;
  const events = state.eventConfig || [];
  const sections = [];
  events.forEach(ev => {
    const t = String(d.texts[ev.event_id] || '').trim();
    if (t) sections.push(ev.name_zh + '：' + t);
  });
  const think = sections.concat(d.unmatched).join('\n\n');
  const checked = events.filter(ev => d.checks[ev.event_id]);
  const answerLines = sftEnvLines().concat(sftConclusionLines());
  const attrsOut = {};
  events.forEach(ev => {
    const a = d.attrs && d.attrs[ev.event_id];
    if (!a) return;
    const clean = {};
    Object.keys(a).forEach(k => {
      const v = a[k];
      if (Array.isArray(v) ? v.length : v) clean[k] = v;
    });
    if (Object.keys(clean).length) attrsOut[ev.event_id] = clean;
  });
  return {
    description: '<think>\n' + think + '\n</think>\n<answer>\n' + answerLines.join('\n') + '\n</answer>',
    action: checked.map(ev => ev.event_id),
    event_attributes: Object.keys(attrsOut).length ? attrsOut : null,
  };
}

export function sftSignature() {
  return JSON.stringify(buildSftRevision());
}

// 从 sft 样本初始化编辑草稿(检出初值 = action 反映射;未激活事件留空不勾)
// attrs:新格式取文件 event_attributes(按当前选项定义清洗);旧格式按关键词回猜(原文不动)
function initSftDraft(sft) {
  const events = state.eventConfig || [];
  const parsed = parseSftDescription(sft.description, events);
  const actions = Array.isArray(sft.action) ? sft.action : [];
  const hasFileAttrs = sft.event_attributes != null && typeof sft.event_attributes === 'object';
  const fileAttrs = hasFileAttrs ? sft.event_attributes : {};
  const texts = {}, checks = {}, attrs = {}, skeletons = {};
  events.forEach(ev => {
    if (ev.is_active) {
      texts[ev.event_id] = parsed.sections[ev.event_id] || '';
      checks[ev.event_id] = actions.indexOf(ev.event_id) >= 0;
      if (evOptions(ev).length) {
        attrs[ev.event_id] = hasFileAttrs
          ? sanitizeFileAttrs(ev, fileAttrs[ev.event_id] || fileAttrs[String(ev.event_id)])
          : guessAttrsFromText(ev, texts[ev.event_id]);
        skeletons[ev.event_id] = skeleton(ev, attrs[ev.event_id]);
      }
    } else {
      texts[ev.event_id] = '';
      checks[ev.event_id] = false;
    }
  });
  state.sftDraft = {
    texts: texts, checks: checks, attrs: attrs, skeletons: skeletons,
    unmatched: parsed.unmatched, env: parsed.env,
  };
  state.sftSavedSig = sftSignature();
}

// textarea 自适应高度:随内容增长,超过上限后出现滚动条
const SFT_TEXTAREA_MAX_H = 300;
function autoGrow(ta) {
  ta.style.height = 'auto';
  const border = ta.offsetHeight - ta.clientHeight; // border-box 下高度需含边框
  const need = ta.scrollHeight + border;
  const capped = need > SFT_TEXTAREA_MAX_H;
  ta.style.height = (capped ? SFT_TEXTAREA_MAX_H : need) + 'px';
  ta.style.overflowY = capped ? 'auto' : 'hidden';
}

function sftEditorHtml() {
  const d = state.sftDraft;
  let html = '<div class="sft-section-title">事件思考(按事件编辑;「检出」勾选在保存时联动 action 与结论)</div>';
  (state.eventConfig || []).forEach(ev => {
    const opts = ev.is_active ? evOptions(ev) : [];
    let chipsHtml = '';
    if (opts.length) {
      chipsHtml = '<div class="sft-attrs">' + opts.map(g => {
        const cur = (d.attrs[ev.event_id] || {})[g.key];
        const pills = g.options.map(opt => {
          const sel = Array.isArray(cur) ? cur.indexOf(opt) >= 0 : cur === opt;
          return '<button type="button" class="sft-chip' + (sel ? ' selected' : '') + '"'
            + ' data-ev-chip="' + ev.event_id + '" data-attr="' + esc(g.key) + '"'
            + ' data-value="' + esc(opt) + '">' + esc(opt) + '</button>';
        }).join('');
        return '<div class="sft-attr-row"><span class="answer-key">' + esc(g.label) + '</span>'
          + '<span class="sft-chips">' + pills + '</span></div>';
      }).join('') + '</div>';
    }
    html += '<div class="sft-ev' + (ev.is_active ? '' : ' inactive') + '">'
      + '<div class="sft-ev-head">'
      + '<span class="sft-ev-name">' + esc(ev.name_zh) + '</span>'
      + (opts.length ? '<span class="sft-warn-dot" data-ev-warn="' + ev.event_id + '" hidden></span>' : '')
      + (ev.is_active ? '' : '<span class="sft-ev-tag">未激活</span>')
      + '<label class="sft-ev-check"><input type="checkbox" data-ev-check="' + ev.event_id + '"'
      + (d.checks[ev.event_id] ? ' checked' : '') + '>检出</label>'
      + '</div>'
      + chipsHtml
      + '<textarea class="sft-ev-text" data-ev-text="' + ev.event_id + '" rows="2"'
      + (ev.is_active ? '' : ' placeholder="未激活事件类别,可人工修改"')
      + '>' + esc(d.texts[ev.event_id] || '') + '</textarea>'
      + '</div>';
  });
  if (d.unmatched.length) {
    html += '<div class="sft-section-title">未归类原文(只读,保存时原样附加到思考末尾)</div>'
      + '<textarea class="sft-ev-text sft-unmatched" readonly rows="2">'
      + esc(d.unmatched.join('\n\n')) + '</textarea>';
  }
  // 天气/时间用单行输入框,场景用自适应文本框;原始答案缺行时也显示空编辑框供人工补全
  const envRows = ['天气', '时间', '场景'].map(k => {
    const label = '<span class="answer-key">' + esc(k) + '</span>';
    if (k === '场景') {
      return '<div class="answer-row">' + label
        + '<textarea class="sft-ev-text answer-env-text" data-env="' + esc(k) + '" rows="2">'
        + esc(d.env[k] || '') + '</textarea></div>';
    }
    return '<div class="answer-row">' + label
      + '<input class="answer-input" data-env="' + esc(k) + '" value="' + esc(d.env[k] || '') + '"></div>';
  }).join('');
  html += '<div class="sft-section-title">答案(ANSWER)</div><div class="answer-block">'
    + envRows
    + '</div>'
    + '<div id="sft-conclusion-preview" class="answer-block sft-conclusion"></div>'
    + '<div class="sft-actions">'
    + '<span class="dirty-flag" id="sft-dirty-flag" hidden>● 未保存</span>'
    + '<button class="btn btn-ghost btn-sm" id="btn-sft-reset" disabled>重置</button>'
    + '<button class="btn btn-primary btn-sm" id="btn-sft-save" disabled>保存</button>'
    + '</div>';
  return html;
}

// 刷新只读的最终结论预览(与 buildSftRevision 同一数据来源,随勾选实时联动)
function refreshSftConclusion() {
  const el = $('#sft-conclusion-preview');
  if (!el) return;
  el.innerHTML = sftConclusionLines().map(line => {
    const m = line.match(/^(class\d+):\s*(.*)$/);
    if (m) {
      return '<div class="answer-row"><span class="answer-key answer-class">' + esc(m[1]) + '</span>'
        + '<span class="answer-val">' + esc(m[2]) + '</span></div>';
    }
    const m2 = line.match(/^最终结论：([\s\S]*)$/);
    return '<div class="answer-row"><span class="answer-key">最终结论</span>'
      + '<span class="answer-val">' + esc(m2 ? m2[1] : line) + '</span></div>';
  }).join('');
}

function updateSftDirty() {
  const dirty = sftSignature() !== state.sftSavedSig;
  const f = $('#sft-dirty-flag'); if (f) f.hidden = !dirty;
  const s = $('#btn-sft-save'); if (s) s.disabled = !dirty;
  const r = $('#btn-sft-reset'); if (r) r.disabled = !dirty;
}

function bindSftEditor(body) {
  // 所有 textarea 挂载时先按内容自适应一次(含只读的未归类原文框)
  $$('textarea', body).forEach(autoGrow);
  $$('textarea[data-ev-text]', body).forEach(ta => {
    ta.addEventListener('input', () => {
      state.sftDraft.texts[+ta.dataset.evText] = ta.value;
      autoGrow(ta);
      updateSftDirty();
    });
  });
  $$('input[data-ev-check]', body).forEach(cb => {
    cb.addEventListener('change', () => {
      state.sftDraft.checks[+cb.dataset.evCheck] = cb.checked;
      refreshSftConclusion();
      refreshWarnDots(body);
      updateSftDirty();
    });
  });
  $$('[data-ev-chip]', body).forEach(chip => {
    chip.addEventListener('click', () => {
      const ev = (state.eventConfig || []).find(e => e.event_id === +chip.dataset.evChip);
      const group = ev && evOptions(ev).find(g => g.key === chip.dataset.attr);
      if (!ev || !group) return;
      applyChipChange(body, ev, group, chip.dataset.value);
    });
  });
  $$('[data-env]', body).forEach(el => {
    el.addEventListener('input', () => {
      state.sftDraft.env[el.dataset.env] = el.value;
      if (el.tagName === 'TEXTAREA') autoGrow(el);
      updateSftDirty();
    });
  });
  refreshSftConclusion();
  refreshWarnDots(body);
  $('#btn-sft-save', body).addEventListener('click', saveSft);
  $('#btn-sft-reset', body).addEventListener('click', () => {
    renderSftBody(state.results.sft_label);
    toast('已重置为磁盘版本');
  });
}

async function saveSft() {
  const stem = state.currentStem;
  if (!stem || !state.results || !state.results.sft_label) return;
  const btn = $('#btn-sft-save');
  if (btn) btn.disabled = true;
  // 只改 description / action / event_attributes,其余字段原样提交(后端会校验)
  const payload = Object.assign({}, state.results.sft_label, buildSftRevision());
  const inFlightSig = sftSignature(); // 在途 payload 的签名,用于识别保存期间的继续编辑
  try {
    const saved = await api('/api/results/' + encodeURIComponent(stem) + '/sft', {
      method: 'PUT', body: payload,
    });
    if (state.currentStem !== stem) return; // 期间切换了视频
    state.results.sft_label = saved || payload;
    toast('已保存', 'ok');
    if (sftSignature() === inFlightSig) {
      renderSftBody(state.results.sft_label); // 保存期间无新编辑:以保存后的内容重建草稿
    } else {
      updateSftDirty(); // 保存期间用户继续编辑:保留草稿,仅重算 dirty
    }
    flashSaveBtn($('#btn-sft-save')); // 按钮短暂显示 ✓(可能已被上面的重建替换,取最新按钮)
  } catch (e) {
    if (btn) btn.disabled = false;
    toast('保存失败(' + e.status + '):' + e.message, 'err');
  }
}

export async function renderSftBody(sft) {
  const body = $('#sft-body');
  if (!body) return;
  if (!sft) { body.innerHTML = '<div class="empty-note">无 SFT 标注</div>'; return; }

  const meta = '<div class="sft-meta">'
    + '<span>' + esc(sft.chunk || '') + '</span>'
    + '<span>idx: ' + esc(sft.idx) + '</span>'
    + '<span>' + esc(sft.start_timestamp) + 's → ' + esc(sft.end_timestamp) + 's</span>'
    + '<span>' + esc(sft.chunk_name || '') + '</span>'
    + '</div>';

  if (!state.eventConfig) {
    body.innerHTML = meta + '<div class="empty-note">加载事件配置…</div>';
    try {
      state.eventConfig = await api('/api/config/events');
    } catch (e) {
      if (body.isConnected) {
        body.innerHTML = meta + '<div class="empty-note">事件配置加载失败:' + esc(e.message) + '</div>';
      }
      return;
    }
  }
  if (!body.isConnected) return; // 期间切换了视图
  initSftDraft(sft);
  body.innerHTML = meta + sftEditorHtml();
  bindSftEditor(body);
}
