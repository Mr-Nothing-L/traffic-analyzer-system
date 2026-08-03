/* ------------------------------------------------------------ SFT 卡(视图层) */
import { $, $$, esc, toast, flashSaveBtn } from './util.js';
import { state } from './state.js';
import { api, getFileSig } from './api.js';
import { selectVideo } from './preview.js';
import {
  groupMentionStrings, declaredSpans, tokenizeSpansHtml,
  replaceDeclaredSpans, swapSkeletonPrefix, mapMentionToOption,
} from './sft_spans.js';
import {
  evOptions, skeleton, missingRequired, sftConclusionLines,
  buildSftRevision, sftSignature, initSftDraft,
} from './sft_model.js';

// event_id 全局采用标注文档 v4.5 的 action 编号(9 = 正常占位),action / classN 直接等于 event_id

// ---------------------------------------------------------------------------
// 结构化选项(chips):封闭枚举,只读选项集,三层联动
//   chips(选中态) → 文本同步(见 applyChipChange:声明提及 span 锚定替换 +
//   骨架前缀就地换新) → 文本框
//   文本框为 contenteditable 富文本:仅声明提及的 span 渲染为 token;可自由编辑
//   (纯文本),blur/chip 变更时重新分词;chips 不从手动编辑回写。
//   chips 仅在样本对该事件声明了 attr_mentions 时渲染;无声明的样本/事件
//   退化为纯文本卡(无 chips、无 token,不做任何启发式分词/回填)
// ---------------------------------------------------------------------------

function declaredMentions(ev) {
  const d = state.sftDraft;
  if (!d || !d.mentions) return null;
  return d.mentions[ev.event_id] || null;
}

// 纯文本 → 带 token 标注的 HTML:仅声明提及(attr_mentions)所在的 span 标注为
// token(全文本精确子串定位;背景同形词保持纯文本);无声明提及的样本/事件
// 整体渲染为纯文本,不做任何启发式命中
function tokenHtml(ev, text) {
  const t = String(text || '');
  if (!declaredMentions(ev)) return esc(t);
  return tokenizeSpansHtml(declaredSpans(ev, t), t);
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

// chip 变更 → 文本同步(优先级从高到低):
//   1) 声明通道(attr_mentions)且该组有声明提及、单选换值 → 位置锚定替换:
//      仅声明提及所在的 span 替换为新值(无别名扩展;背景句同形词不动),
//      并同步更新草稿里的声明提及与 span;
//      骨架前缀含旧值但非声明提及(如模型按模板写的主语句)时就地换新骨架;
//   2) 已前置的骨架前缀就地更新(保证骨架只前置一次,不堆叠);
//   取消选中/移除多选不删词:旧值在文本中则保留原文。
//   多选组的声明提及同步:新格式为嵌套 per-option 绑定(选项名 → 提及串数组),
//   直接按选项键增删;旧扁平数组按「声明提及→选项」别名映射归选项
//   (见 mapMentionToOption)。取消选中的选项,其提及转为纯文本并移出该组
//   attr_mentions(词保留在正文中作事实描述,暂存 mentionsOff);重新选中时
//   仍在文本中的暂存提及恢复标注与声明;未映射提及保持现状。
function applyChipChange(body, ev, group, value) {
  const d = state.sftDraft;
  const id = ev.event_id;
  const attrs = d.attrs[id] || (d.attrs[id] = {});
  const oldVal = group.multi ? '' : (attrs[group.key] || '');
  let added = null; // 多选组本次新增的选项(移除时为 null)
  if (group.multi) {
    const cur = Array.isArray(attrs[group.key]) ? attrs[group.key] : [];
    const on = cur.indexOf(value) >= 0;
    const next = on ? cur.filter(o => o !== value) : cur.concat(value);
    if (!on) added = value;
    attrs[group.key] = group.options.filter(o => next.indexOf(o) >= 0); // 保持 options 定义顺序
  } else {
    attrs[group.key] = attrs[group.key] === value ? '' : value;
  }
  const newVal = group.multi ? '' : (attrs[group.key] || '');
  const oldSk = d.skeletons[id] || '';
  const newSk = skeleton(ev, attrs);
  d.skeletons[id] = newSk;
  let text = String(d.texts[id] || '');
  // 声明通道锚点:该组声明提及所在的 span(仅声明通道 + 单选换值时非空)
  const decl = declaredMentions(ev);
  // 多选组的声明提及同步:嵌套格式(选项名 → 提及串数组)按选项键直接增删;
  // 旧扁平数组按别名映射归选项。取消选中 → 该选项的声明提及移出 decl(重渲染
  // 即为纯文本,保存时不再上送),暂存 mentionsOff 供重新选中时恢复;
  // 新选中 → 暂存中仍在文本里的提及加回 decl(重新标注)。
  // 未映射提及两边都不动;span 缓存作废,骨架分支与重渲染按新声明重算
  if (group.multi && decl) {
    const offAll = d.mentionsOff || (d.mentionsOff = {});
    const off = offAll[id] || (offAll[id] = {});
    const isNest = v => !!v && !Array.isArray(v) && typeof v === 'object';
    const cur = decl[group.key];
    const stash = off[group.key];
    if (isNest(cur) || (!cur && isNest(stash))) {
      // 新格式:嵌套 per-option 绑定,选项名即键,无需别名映射
      if (!added) { // 取消选中 value
        const moved = (cur && cur[value]) || [];
        if (moved.length) {
          const rest = {};
          Object.keys(cur).forEach(o => { if (o !== value && cur[o].length) rest[o] = cur[o]; });
          if (Object.keys(rest).length) decl[group.key] = rest;
          else delete decl[group.key];
          off[group.key] = Object.assign(isNest(stash) ? stash : {}, { [value]: moved });
          d.mentionSpans[id] = null;
        }
      } else { // 新选中 added:恢复暂存提及
        const cand = (isNest(stash) && stash[added]) || [];
        const restored = cand.filter(s => text.indexOf(s) >= 0);
        if (restored.length) {
          const gone = cand.filter(s => restored.indexOf(s) < 0);
          if (gone.length) stash[added] = gone; else delete stash[added];
          const nest = isNest(decl[group.key]) ? decl[group.key] : (decl[group.key] = {});
          nest[added] = restored;
          d.mentionSpans[id] = null;
        }
      }
    } else if (!added) { // 旧扁平数组:取消选中 value
      const keep = [], moved = [];
      (cur || []).forEach(s => {
        (mapMentionToOption(group, s) === value ? moved : keep).push(s);
      });
      if (moved.length) {
        decl[group.key] = keep;
        off[group.key] = (off[group.key] || []).concat(moved);
        d.mentionSpans[id] = null;
      }
    } else { // 旧扁平数组:新选中 added,恢复暂存提及
      const remain = [], restored = [];
      (off[group.key] || []).forEach(s => {
        if (mapMentionToOption(group, s) === added && text.indexOf(s) >= 0) restored.push(s);
        else remain.push(s);
      });
      if (restored.length) {
        off[group.key] = remain;
        decl[group.key] = (decl[group.key] || []).concat(restored);
        d.mentionSpans[id] = null;
      }
    }
  }
  const declList = decl && oldVal && newVal ? groupMentionStrings(decl[group.key]) : [];
  const declSpans = declList.length ? declaredSpans(ev, text) : null;
  const mySpans = declSpans ? declSpans.filter(sp => sp.group === group.key) : [];
  if (mySpans.length) {
    // 声明提及即锚点:仅 span 位置替换为新值(无别名扩展,背景句同形词不动;
    // 链式编辑时上一次写入的新值靠 span 位置继续锚定,不会误伤背景);
    // 声明提及同步为新值(保存时随 attr_mentions 落盘,后端子串校验方能通过)
    const r = replaceDeclaredSpans(text, declSpans, group.key, newVal);
    text = r.text;
    decl[group.key] = [newVal];
    let spans = r.spans;
    // 骨架前缀(模型按模板写的主语句)含旧值但不是声明提及:就地换为新骨架,
    // 保持与 chips 一致;其后的 span 按差异区间平移(与差异区间重叠的丢弃)
    if (oldSk && newSk !== oldSk && text.indexOf(oldSk) === 0) {
      const sw = swapSkeletonPrefix(text, r.spans, oldSk, newSk);
      text = sw.text;
      spans = sw.spans;
    }
    d.mentionSpans[id] = spans;
  } else if (oldSk && text.indexOf(oldSk) === 0) {
    // 骨架前缀就地换新;声明通道下其余组的 span 同步平移,避免后续
    // 声明提及替换因文本移位而落在骨架的同形词上
    const spans = decl ? declaredSpans(ev, text) : null;
    const sw = swapSkeletonPrefix(text, spans || [], oldSk, newSk);
    text = sw.text;
    if (spans) d.mentionSpans[id] = sw.spans;
  }
  d.texts[id] = text;
  const el = $('[data-ev-text="' + id + '"]', body);
  if (el) {
    renderTokens(el, ev, group.key, text);
    el.classList.remove('sft-fade'); // 重新触发动画
    void el.offsetWidth;
    el.classList.add('sft-fade');
  }
  const cur = attrs[group.key];
  $$('[data-ev-chip="' + id + '"][data-attr="' + group.key + '"]', body).forEach(c => {
    c.classList.toggle('selected', Array.isArray(cur) ? cur.indexOf(c.dataset.value) >= 0 : cur === c.dataset.value);
  });
  refreshWarnDots(body);
  updateSftDirty();
}

// 文本框自适应高度:随内容增长,超过上限后出现滚动条(textarea 与富文本框通用)
const SFT_TEXTAREA_MAX_H = 300;
function autoGrow(ta) {
  ta.style.height = 'auto';
  const border = ta.offsetHeight - ta.clientHeight; // border-box 下高度需含边框
  const need = ta.scrollHeight + border;
  const capped = need > SFT_TEXTAREA_MAX_H;
  ta.style.height = (capped ? SFT_TEXTAREA_MAX_H : need) + 'px';
  ta.style.overflowY = capped ? 'auto' : 'hidden';
}

// 按纯文本重建 token 标注;仅在外部重渲染时调用(初次渲染/blur/chip 变更),
// 输入过程中不触碰 DOM,光标不重置。text 缺省取元素当前 innerText;
// pulseGroup 非空时给该组 token 加短暂高亮脉冲
function renderTokens(el, ev, pulseGroup, text) {
  el.innerHTML = tokenHtml(ev, text !== undefined ? text : el.innerText);
  autoGrow(el);
  if (pulseGroup) {
    $$('.sft-tok[data-attr="' + pulseGroup + '"]', el).forEach(s => s.classList.add('sft-tok-pulse'));
  }
}

// chip hover ↔ token 联动:on=true 时同事件卡内同组 token 加深高亮,
// 异组 token 不做任何变化(不淡化);仅切换 class,样式全在 CSS
function linkChipHover(chip, on) {
  const card = chip.closest('.sft-ev');
  if (!card) return;
  const group = chip.dataset.attr;
  $$('.sft-tok', card).forEach(tok => {
    tok.classList.toggle('sft-tok-link', on && tok.dataset.attr === group);
  });
}

function sftEditorHtml() {
  const d = state.sftDraft;
  let html = '<div class="sft-section-title">事件思考(按事件编辑;「检出」勾选在保存时联动 action 与结论)</div>';
  (state.eventConfig || []).forEach(ev => {
    const opts = ev.is_active ? evOptions(ev) : [];
    // chips 仅在该事件有声明提及(attr_mentions)时渲染;无声明退化为纯文本卡
    const hasDecl = !!(d.mentions && d.mentions[ev.event_id]);
    let chipsHtml = '';
    if (opts.length && hasDecl) {
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
      + (opts.length && hasDecl ? '<span class="sft-warn-dot" data-ev-warn="' + ev.event_id + '" hidden></span>' : '')
      + (ev.is_active ? '' : '<span class="sft-ev-tag">未激活</span>')
      + '<label class="sft-ev-check"><input type="checkbox" data-ev-check="' + ev.event_id + '"'
      + (d.checks[ev.event_id] ? ' checked' : '') + '>检出</label>'
      + '</div>'
      + chipsHtml
      + '<div class="sft-ev-text sft-richtext" data-ev-text="' + ev.event_id + '"'
      + ' contenteditable="true" spellcheck="false"'
      + (ev.is_active ? '' : ' data-placeholder="未激活事件类别,可人工修改"')
      + '>' + tokenHtml(ev, d.texts[ev.event_id] || '') + '</div>'
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
  // 所有文本框挂载时先按内容自适应一次(含富文本事件框与只读的未归类原文框)
  $$('textarea, .sft-richtext', body).forEach(autoGrow);
  // 事件文本为 contenteditable:输入只回写草稿纯文本(innerText),不重分词、不动光标;
  // 粘贴净化为纯文本;Enter 与 textarea 一致插入换行;blur 时按纯文本重新分词
  $$('.sft-richtext[data-ev-text]', body).forEach(el => {
    const ev = (state.eventConfig || []).find(e => e.event_id === +el.dataset.evText);
    el.addEventListener('input', () => {
      state.sftDraft.texts[+el.dataset.evText] = el.innerText;
      autoGrow(el);
      updateSftDirty();
    });
    el.addEventListener('paste', e => {
      e.preventDefault();
      const t = (e.clipboardData || window.clipboardData).getData('text/plain');
      document.execCommand('insertText', false, t);
    });
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        document.execCommand('insertText', false, '\n');
      }
    });
    el.addEventListener('blur', () => {
      if (!ev) return;
      renderTokens(el, ev);
      // 人工编辑把声明提及改没时提醒:保存时这些提及会被自动丢弃(见 buildSftRevision)
      const decl = declaredMentions(ev);
      const dd = state.sftDraft;
      if (decl && dd) {
        const t = String(dd.texts[ev.event_id] || '');
        const gone = [];
        Object.keys(decl).forEach(gk => groupMentionStrings(decl[gk]).forEach(s => {
          if (s && t.indexOf(s) < 0 && gone.indexOf(s) < 0) gone.push(s);
        }));
        const sig = gone.join('');
        if (gone.length && dd.mentionGoneSig !== sig) {
          toast('声明提及「' + gone.join('」、「') + '」已不在文本中,保存时将自动移除');
        }
        dd.mentionGoneSig = sig;
      }
    });
    // token hover 反向联动:同事件卡内同组 chips 加描边提示
    el.addEventListener('mouseover', e => {
      const tok = e.target.closest && e.target.closest('.sft-tok');
      const card = el.closest('.sft-ev');
      if (!card) return;
      $$('[data-ev-chip]', card).forEach(c => {
        c.classList.toggle('sft-chip-link', !!tok && c.dataset.attr === tok.dataset.attr);
      });
    });
    el.addEventListener('mouseleave', () => {
      const card = el.closest('.sft-ev');
      if (card) $$('.sft-chip-link', card).forEach(c => c.classList.remove('sft-chip-link'));
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
    chip.addEventListener('mouseenter', () => linkChipHover(chip, true));
    chip.addEventListener('mouseleave', () => linkChipHover(chip, false));
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
  // 只改 description / action / event_attributes / attr_mentions,其余字段原样提交(后端会校验)
  const payload = Object.assign({}, state.results.sft_label, buildSftRevision());
  const baseSig = getFileSig(stem);
  if (baseSig) payload.base_sig = baseSig; // 乐观锁:与 GET /api/results 的 file_sig 对齐
  const inFlightSig = sftSignature(); // 在途 payload 的签名,用于识别保存期间的继续编辑
  try {
    const saved = await api('/api/results/' + encodeURIComponent(stem) + '/sft', {
      method: 'PUT', body: payload,
    });
    if (state.currentStem !== stem) return; // 期间切换了视频
    if (saved && saved.file_sig) delete saved.file_sig; // 锁字段(api 层已缓存)不进标注对象
    state.results.sft_label = saved || payload;
    toast('已保存', 'ok');
    if (sftSignature() === inFlightSig) {
      renderSftBody(state.results.sft_label); // 保存期间无新编辑:以保存后的内容重建草稿
    } else {
      updateSftDirty(); // 保存期间用户继续编辑:保留草稿,仅重算 dirty
    }
    flashSaveBtn($('#btn-sft-save')); // 按钮短暂显示 ✓(可能已被上面的重建替换,取最新按钮)
  } catch (e) {
    if (e.status === 409) {
      // 乐观锁冲突:他人已修改;重载会丢弃当前未保存的修改,先 confirm
      if (confirm('该视频的标注已被他人修改。\n确定将丢弃当前未保存的修改并刷新为最新版本。')) {
        toast('他人已修改,已为你刷新', 'err');
        selectVideo(state.currentRel);
        return;
      }
    }
    if (btn) btn.disabled = false;
    toast('保存失败(' + e.status + '):' + e.message, 'err');
  }
}

export async function renderSftBody(sft) {
  const body = $('#sft-body');
  if (!body) return;
  if (!sft) { body.innerHTML = '<div class="empty-note">无 SFT 标注</div>'; return; }

  // chunk 时间戳统一一位小数(2.5s → 2.5s,整数秒也补 .0),元信息列宽稳定
  const sec = v => (typeof v === 'number' ? v.toFixed(1) : String(v));
  const meta = '<div class="sft-meta">'
    + '<span>' + esc(sft.chunk || '') + '</span>'
    + '<span>idx: ' + esc(sft.idx) + '</span>'
    + '<span>' + esc(sec(sft.start_timestamp)) + 's → ' + esc(sec(sft.end_timestamp)) + 's</span>'
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
