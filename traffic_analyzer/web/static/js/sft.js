/* ------------------------------------------------------------ SFT 卡 */
import { $, $$, esc, toast, flashSaveBtn } from './util.js';
import { state } from './state.js';
import { api } from './api.js';

// event_id → 标注文档 v4.5 的 action 编号(action 9 = 正常占位,跳过)
const EVENT_ID_TO_ACTION = { 0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 10, 9: 11 };

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
    lines.push('class' + EVENT_ID_TO_ACTION[ev.event_id] + ': ' + ev.name_zh);
  });
  return lines;
}

// 由当前草稿重建 description 与 action(结论区按「检出」勾选重建)
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
  return {
    description: '<think>\n' + think + '\n</think>\n<answer>\n' + answerLines.join('\n') + '\n</answer>',
    action: checked.map(ev => EVENT_ID_TO_ACTION[ev.event_id]),
  };
}

export function sftSignature() {
  return JSON.stringify(buildSftRevision());
}

// 从 sft 样本初始化编辑草稿(检出初值 = action 反映射;未激活事件留空不勾)
function initSftDraft(sft) {
  const events = state.eventConfig || [];
  const parsed = parseSftDescription(sft.description, events);
  const actions = Array.isArray(sft.action) ? sft.action : [];
  const texts = {}, checks = {};
  events.forEach(ev => {
    if (ev.is_active) {
      texts[ev.event_id] = parsed.sections[ev.event_id] || '';
      checks[ev.event_id] = actions.indexOf(EVENT_ID_TO_ACTION[ev.event_id]) >= 0;
    } else {
      texts[ev.event_id] = '';
      checks[ev.event_id] = false;
    }
  });
  state.sftDraft = {
    texts: texts, checks: checks,
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
    html += '<div class="sft-ev' + (ev.is_active ? '' : ' inactive') + '">'
      + '<div class="sft-ev-head">'
      + '<span class="sft-ev-name">' + esc(ev.name_zh) + '</span>'
      + (ev.is_active ? '' : '<span class="sft-ev-tag">未激活</span>')
      + '<label class="sft-ev-check"><input type="checkbox" data-ev-check="' + ev.event_id + '"'
      + (d.checks[ev.event_id] ? ' checked' : '') + '>检出</label>'
      + '</div>'
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
      updateSftDirty();
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
  // 只改 description / action,其余字段原样提交(后端会校验)
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
