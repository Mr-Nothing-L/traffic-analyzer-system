/* ------------------------------------------------------------ SFT 卡 */
import { $, $$, esc, toast, flashSaveBtn } from './util.js';
import { state } from './state.js';
import { api } from './api.js';

// event_id 全局采用标注文档 v4.5 的 action 编号(9 = 正常占位),action / classN 直接等于 event_id

// ---------------------------------------------------------------------------
// 结构化选项(chips):封闭枚举,只读选项集,三层联动
//   chips(选中态) → 文本同步(见 applyChipChange:声明通道按声明提及 span 锚定
//   替换,旧样本走主语句内别名全词替换) → 文本框
//   文本框为 contenteditable 富文本:声明通道仅声明提及的 span 渲染为 token,
//   旧样本主语句内命中选项别名的片段渲染为 token;可自由编辑(纯文本),
//   blur/chip 变更时重新分词;chips 不从手动编辑回写
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
  '工程车': ['工程作业车', '工程车辆', '施工车', '清障车', '救援车'],
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

// --- 双通道分词:样本含 attr_mentions 且声明了该事件时走「声明提及通道」 --------
// 声明通道:模型声明的表面串是唯一权威——只标注/只替换声明串本身出现的位置
// (span 锚定,无正则、无别名扩展),背景句中的同形词一律不动;
// 未声明的事件与旧样本保持原有启发式路径(主语句锚定 + 选项别名)不变。
function declaredMentions(ev) {
  const d = state.sftDraft;
  if (!d || !d.mentions) return null;
  return d.mentions[ev.event_id] || null;
}

// 声明提及的位置 span 列表:[{start, end, group, str}],按 start 升序、互不重叠。
// 缓存于 state.sftDraft.mentionSpans[event_id];仅当缓存与当前文本不再吻合
// (人工编辑过)时才按声明串精确子串搜索重算——重算时若同一声明串的出现次数
// 多于旧 span 数(chip 同步后的裸值如 小型车 可能在背景句另有同形词),
// 按「与旧 span 最近」原则取前 N 个,保证链式编辑后背景同形词仍不被误标。
function computeDeclSpans(decl, text, prev) {
  const cand = [];
  Object.keys(decl).forEach(gk => {
    (decl[gk] || []).forEach(s => {
      if (!s) return;
      const occ = [];
      let i = text.indexOf(s);
      while (i >= 0) { occ.push(i); i = text.indexOf(s, i + 1); }
      if (!occ.length) return;
      const prevCnt = prev ? prev.filter(p => p.group === gk && p.str === s).length : 0;
      let starts = occ;
      if (!prevCnt) {
        // 首次计算(无旧 span 锚点,如初次渲染):出现次数按该串的声明次数封顶
        // (生成侧对同一组的声明串已去重,通常为 1),取文本中的前 N 处——
        // 背景句里的同形词不参与标注;出现次数不足声明次数时全部保留
        const declCount = (decl[gk] || []).filter(x => x === s).length;
        if (occ.length > declCount) starts = occ.slice(0, declCount);
      } else if (occ.length > prevCnt) {
        starts = occ.map(o => {
          let best = Infinity;
          prev.forEach(p => {
            if (p.group === gk && p.str === s) best = Math.min(best, Math.abs(o - p.start));
          });
          return [o, best];
        }).sort((a, b) => a[1] - b[1] || a[0] - b[0]).slice(0, prevCnt).map(x => x[0]);
      }
      starts.forEach(st => cand.push({ start: st, end: st + s.length, group: gk, str: s }));
    });
  });
  // 同起点长串优先(如 黄色工程作业车 吞掉内含的 工程作业车),贪心去重叠
  cand.sort((a, b) => (a.start - b.start) || (b.str.length - a.str.length));
  const out = [];
  let lastEnd = -1;
  cand.forEach(sp => {
    if (sp.start >= lastEnd) { out.push(sp); lastEnd = sp.end; }
  });
  return out;
}

function spansMatchText(spans, text) {
  return spans.every(sp => text.slice(sp.start, sp.end) === sp.str);
}

// 取该事件的声明提及 span(声明通道专用);缓存失效时按声明串精确搜索重算
function declaredSpans(ev, text) {
  const d = state.sftDraft;
  const decl = declaredMentions(ev);
  const id = ev.event_id;
  if (!d.mentionSpans) d.mentionSpans = {};
  const cached = d.mentionSpans[id];
  if (cached && spansMatchText(cached, text)) return cached;
  const spans = computeDeclSpans(decl, String(text || ''), cached);
  d.mentionSpans[id] = spans;
  return spans;
}

// 声明通道的 token 渲染:仅 span 位置标注为 token,其余一律纯文本
function tokenizeSpansHtml(spans, t) {
  let html = '', pos = 0;
  spans.forEach(sp => {
    if (sp.start < pos) return; // 防御:重叠 span 已在 computeDeclSpans 去除
    html += esc(t.slice(pos, sp.start))
      + '<span class="sft-tok" data-attr="' + esc(sp.group) + '">' + esc(t.slice(sp.start, sp.end)) + '</span>';
    pos = sp.end;
  });
  return html + esc(t.slice(pos));
}

// 位置锚定替换:仅把 groupKey 组的 span 内容替换为 newVal(背景同形词不动),
// 同步平移其余 span 并返回新文本与新 span 列表
function replaceDeclaredSpans(text, spans, groupKey, newVal) {
  let out = '', pos = 0, delta = 0;
  const next = [];
  spans.forEach(sp => {
    if (sp.group === groupKey) {
      out += text.slice(pos, sp.start) + newVal;
      pos = sp.end;
      next.push({ start: sp.start + delta, end: sp.start + delta + newVal.length, group: sp.group, str: newVal });
      delta += newVal.length - (sp.end - sp.start);
    } else {
      next.push({ start: sp.start + delta, end: sp.end + delta, group: sp.group, str: sp.str });
    }
  });
  return { text: out + text.slice(pos), spans: next };
}

// 按一组文本变更区间(edits:[{start,end,newLen}],按 start 升序、互不重叠)平移 span:
// 区间之前的 span 不动,之后的平移长度差,与变更区间重叠的 span 丢弃
// (丢弃后由 computeDeclSpans 按声明串重算兜底,与骨架差异区间同一语义)
function shiftSpansForEdits(spans, edits) {
  if (!edits.length) return spans;
  const next = [];
  spans.forEach(sp => {
    let ns = sp.start, ne = sp.end, drop = false;
    edits.forEach(e => {
      if (sp.end <= e.start) return;
      if (sp.start >= e.end) {
        const d = e.newLen - (e.end - e.start);
        ns += d; ne += d;
      } else {
        drop = true;
      }
    });
    if (!drop) next.push({ start: ns, end: ne, group: sp.group, str: sp.str });
  });
  return next;
}

// 骨架前缀就地换新(文本以 oldSk 开头):文本前缀 oldSk → newSk,并按
// 「公共前缀/公共后缀」定位差异区间,其后的 span 平移长度差、重叠的丢弃。
// 所有改动骨架前缀的分支共用,保证声明通道的 span 缓存随文本同步、不失锚
function swapSkeletonPrefix(text, spans, oldSk, newSk) {
  let cs = 0;
  while (cs < oldSk.length && cs < newSk.length && oldSk[cs] === newSk[cs]) cs++;
  let ce = 0;
  while (ce < oldSk.length - cs && ce < newSk.length - cs
         && oldSk[oldSk.length - 1 - ce] === newSk[newSk.length - 1 - ce]) ce++;
  const edit = { start: cs, end: oldSk.length - ce, newLen: newSk.length - ce };
  return {
    text: newSk + text.slice(oldSk.length),
    spans: shiftSpansForEdits(spans, [edit]),
  };
}

// --- chip→文本同步:旧值别名全词替换 -----------------------------------------
// 车道类型/范围的选项词(行车道/应急车道/导流区/路肩、单车道/多车道)与道路通用
// 词汇高度重叠,场景描述里常出现非属性含义的同一写法(如"护栏外应急车道边缘"),
// 全局替换会误伤正文 → 这些组不做替换,仅在骨架前置场景下同步(见 applyChipChange)。
const SFT_REPLACE_SKIP_GROUPS = { lane_type: 1, scope: 1 };

// 选项值的全部书写形态(自身 + 别名),按长度降序:最长优先匹配,
// 避免短别名吃掉长别名(如 客车 匹配进 大客车、小车 匹配进 小型车)
function aliasesOf(value) {
  return [value].concat(SFT_ATTR_ALIASES[value] || []).sort((a, b) => b.length - a.length);
}

function escRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function aliasRe(value) {
  return new RegExp(aliasesOf(value).map(escRe).join('|'), 'g');
}

// 文本中是否出现该选项值的任一书写形态
function mentionsValue(text, value) {
  return aliasesOf(value).some(a => text.indexOf(a) >= 0);
}

// 把旧值的全部书写形态一次性替换为新值(单趟扫描,替换结果不会被二次命中)
function replaceAliases(text, oldVal, newVal) {
  return text.replace(aliasRe(oldVal), newVal);
}

// --- 主语句锚定:标注/替换/回填只作用于「描述事件主体」的句子 -----------------
// 背景句(如"旁边行车道上的货车、小车持续驶过"描述正常通行)里的属性词并非
// 事件主体属性,不应渲染为 token、不应被 chip 替换、也不参与 chips 回填。
// 判定:按 。;与换行切句,命中该事件主语-谓语模式的句子为「主语绑定句」;
// 含「结论:」的句子恒为主语绑定句。
// 已知过包含(启发式宁多勿漏):行人/人员类模式会把"无人员行走"这类否定场景句
// 也判为绑定句;整段无一句命中时回退为全文,保证不丢标注。
const SFT_SUBJECT_PATTERNS = {
  1: /停着|停有|停放|停于|静止|占用|构成|判定/,       // 违法停车
  2: /停着|停有|停放|停于|静止|占用|构成|判定/,       // 应急车道占用
  3: /事故|碰撞|追尾|刮擦|剐蹭|撞击/,                 // 交通事故
  4: /行人|人员|人影|施工人员|工人/,                  // 高速公路行人出现
  5: /摩托|电动自行车|电动车|电瓶车|自行车|三轮车/,   // 摩托车出现
  6: /拥堵|缓行|排队|车龙/,                           // 拥堵
  7: /施工|作业|封闭/,                               // 道路施工
  8: /逆行|倒车/,                                     // 车辆逆行/倒车
  10: /抛洒|散落|掉落|遗撒/,                          // 抛洒物
};

// 按 。;;与换行切句,返回 [{start, end}](end 不含分隔符)
function splitSentences(t) {
  const out = [];
  let start = 0;
  for (let i = 0; i <= t.length; i++) {
    if (i === t.length || t[i] === '。' || t[i] === '；' || t[i] === ';' || t[i] === '\n') {
      if (i > start) out.push({ start: start, end: i });
      start = i + 1;
    }
  }
  return out;
}

// 主语绑定句的字符范围(升序、不重叠);事件无模式或整段无命中时回退为全文
function subjectRanges(ev, t) {
  const re = SFT_SUBJECT_PATTERNS[ev.event_id];
  const ranges = [];
  if (re) {
    splitSentences(t).forEach(s => {
      const seg = t.slice(s.start, s.end);
      if (re.test(seg) || seg.indexOf('结论:') >= 0 || seg.indexOf('结论：') >= 0) {
        ranges.push([s.start, s.end]);
      }
    });
  }
  return ranges.length ? ranges : [[0, t.length]];
}

// 按范围把文本拆成交替片段,仅对主语绑定片段应用 fn,其余原样拼接
function mapSubjectRanges(t, ranges, fn) {
  let out = '', pos = 0;
  ranges.forEach(r => {
    out += t.slice(pos, r[0]) + fn(t.slice(r[0], r[1]));
    pos = r[1];
  });
  return out + t.slice(pos);
}

// 主语绑定句拼接文本(供回填/提及判断;分隔符防跨句误拼出关键词)
function subjectText(ev, text) {
  const t = String(text || '');
  return subjectRanges(ev, t).map(r => t.slice(r[0], r[1])).join('。');
}

// --- token 标注:事件文本中命中任一选项别名的片段渲染为带组标记的 span --------
// 该事件所有组的「别名 → 组」索引,按别名长度降序;同位置先列出的组优先
function tokenIndex(ev) {
  const list = [];
  evOptions(ev).forEach(g => {
    g.options.forEach(opt => {
      aliasesOf(opt).forEach(a => list.push({ alias: a, group: g.key }));
    });
  });
  return list.sort((x, y) => y.alias.length - x.alias.length);
}

// 主语绑定句片段 → 带 token 标注的 HTML(逐字符贪心,最长别名优先)
function tokenizeSegHtml(idx, seg) {
  let html = '', i = 0;
  while (i < seg.length) {
    let hit = null;
    for (let k = 0; k < idx.length; k++) {
      if (seg.startsWith(idx[k].alias, i)) { hit = idx[k]; break; }
    }
    if (hit) {
      html += '<span class="sft-tok" data-attr="' + esc(hit.group) + '">' + esc(hit.alias) + '</span>';
      i += hit.alias.length;
    } else {
      html += esc(seg[i]);
      i += 1;
    }
  }
  return html;
}

// 纯文本 → 带 token 标注的 HTML;声明通道(attr_mentions)仅标注声明提及所在的
// span(全文本,无主语句过滤;背景同形词保持纯文本);否则仅主语绑定句内的
// 启发式命中渲染为 token,背景句保持纯文本
function tokenHtml(ev, text) {
  const t = String(text || '');
  const decl = declaredMentions(ev);
  if (decl) return tokenizeSpansHtml(declaredSpans(ev, t), t);
  const idx = tokenIndex(ev);
  const ranges = subjectRanges(ev, t);
  let html = '', pos = 0;
  ranges.forEach(r => {
    html += esc(t.slice(pos, r[0])) + tokenizeSegHtml(idx, t.slice(r[0], r[1]));
    pos = r[1];
  });
  return html + esc(t.slice(pos));
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
// 仅在主语绑定句内回猜:背景句里的属性词(如正常通行的货车/小车)不参与,
// 避免把背景提及误回填为主体属性(如违停主体是工程车却回填成小型车)
function guessAttrsFromText(ev, text) {
  const attrs = {};
  const t = subjectText(ev, text);
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

// chip 变更 → 文本同步(优先级从高到低):
//   1) 已前置的骨架前缀就地更新(保证骨架只前置一次,不堆叠);
//   2) 声明通道(attr_mentions)且该组有声明提及、单选换值 → 位置锚定替换:
//      仅声明提及所在的 span 替换为新值(无别名扩展;背景句同形词不动,
//      主语句规则与 skip 组均不适用),并同步更新草稿里的声明提及与 span;
//      骨架前缀含旧值但非声明提及(如模型按模板写的主语句)时就地换新骨架;
//      声明串已不在文本中时回退现行行为;
//   3) 单选换值且旧值出现在主语绑定句中 → 主语绑定句内旧值的全部别名形态
//      全词替换为新值(背景句同形词不动;车道类型/范围等 skip 组除外,
//      见 SFT_REPLACE_SKIP_GROUPS);
//   4) 旧值在主语绑定句中完全未出现(或新选/新增多选) → 前置插入骨架句,仅一次;
//   取消选中/移除多选不删词:旧值在文本中则保留原文,不在才补骨架。
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
  const declList = decl && oldVal && newVal ? (decl[group.key] || []) : [];
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
  } else if (oldVal && newVal && !SFT_REPLACE_SKIP_GROUPS[group.key] && mentionsValue(subjectText(ev, text), oldVal)) {
    // 仅替换主语绑定句内的旧值形态;背景句里的同形词保持原文。
    // 声明通道下同步平移其余组的 span(与替换区间重叠的丢弃,重算兜底)
    const spans = decl ? declaredSpans(ev, text) : null;
    const ranges = subjectRanges(ev, text);
    const re = aliasRe(oldVal);
    const edits = [];
    let out = '', pos = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (!ranges.some(r => m.index >= r[0] && m.index + m[0].length <= r[1])) continue;
      out += text.slice(pos, m.index) + newVal;
      pos = m.index + m[0].length;
      edits.push({ start: m.index, end: pos, newLen: newVal.length });
    }
    text = out + text.slice(pos);
    if (spans) d.mentionSpans[id] = shiftSpansForEdits(spans, edits);
  } else if (newSk && newSk !== oldSk && !(group.multi && !added)) {
    const probe = group.multi ? added : oldVal;
    if (!probe || !mentionsValue(subjectText(ev, text), probe)) {
      // 前置骨架句(仅一次):声明通道下全部 span 平移「骨架句+分隔符」长度,
      // 否则后续声明提及替换会落在骨架里的同形词上
      const spans = decl ? declaredSpans(ev, text) : null;
      const prefix = text ? newSk + ';' : newSk;
      text = prefix + text;
      if (spans) d.mentionSpans[id] = shiftSpansForEdits(spans, [{ start: 0, end: 0, newLen: prefix.length }]);
    }
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
  // 声明提及随草稿带出前,逐条按当前事件文本过滤(与后端 _check_attr_mentions
  // 同一子串口径):人工编辑删掉的提及不再上送,避免保存 422;过滤后的空数组、
  // 空组以及未激活/无选项组事件的条目一并省略(后端对无选项事件直接拒绝),
  // 全空时输出 null(后端不落 null 字段,即移除 attr_mentions)
  let mentionsOut = null;
  if (d.mentions) {
    const mo = {};
    events.forEach(ev => {
      if (!ev.is_active || !evOptions(ev).length) return;
      const raw = d.mentions[ev.event_id];
      if (!raw) return;
      const t = String(d.texts[ev.event_id] || '').trim();
      const clean = {};
      Object.keys(raw).forEach(k => {
        const keep = (raw[k] || []).filter(s => s && t.indexOf(s) >= 0);
        if (keep.length) clean[k] = keep;
      });
      if (Object.keys(clean).length) mo[ev.event_id] = clean;
    });
    if (Object.keys(mo).length) mentionsOut = mo;
  }
  return {
    description: '<think>\n' + think + '\n</think>\n<answer>\n' + answerLines.join('\n') + '\n</answer>',
    action: checked.map(ev => ev.event_id),
    event_attributes: Object.keys(attrsOut).length ? attrsOut : null,
    attr_mentions: mentionsOut,
  };
}

export function sftSignature() {
  return JSON.stringify(buildSftRevision());
}

// 从 sft 样本初始化编辑草稿(检出初值 = action 反映射;未激活事件留空不勾)
// attrs:新格式取文件 event_attributes(按当前选项定义清洗);旧格式按关键词回猜(原文不动)
// mentions:样本含 attr_mentions 时按事件声明的表面串(按当前选项定义清洗),
//   该事件走声明通道分词/锚定;样本无 attr_mentions 时整体为 null,全量走启发式
function initSftDraft(sft) {
  const events = state.eventConfig || [];
  const parsed = parseSftDescription(sft.description, events);
  const actions = Array.isArray(sft.action) ? sft.action : [];
  const hasFileAttrs = sft.event_attributes != null && typeof sft.event_attributes === 'object';
  const fileAttrs = hasFileAttrs ? sft.event_attributes : {};
  const rawMentions = (sft.attr_mentions != null && typeof sft.attr_mentions === 'object') ? sft.attr_mentions : null;
  const texts = {}, checks = {}, attrs = {}, skeletons = {};
  const mentions = rawMentions ? {} : null;
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
    // 声明提及与 attrs 同一门槛:仅保留已激活且有选项组的事件;空数组与空组一律丢弃
    // (未激活事件的段落在 parseSftDescription 已丢弃,保留其提及保存必遭后端 422;
    //  无选项组的事件如实线变道,后端校验「no options defined」同样拒绝)
    if (mentions && ev.is_active && evOptions(ev).length) {
      const raw = rawMentions[ev.event_id] || rawMentions[String(ev.event_id)];
      if (raw && typeof raw === 'object') {
        const clean = {};
        evOptions(ev).forEach(g => {
          const v = raw[g.key];
          if (Array.isArray(v)) {
            const arr = v.filter(s => typeof s === 'string' && s);
            if (arr.length) clean[g.key] = arr;
          }
        });
        if (Object.keys(clean).length) mentions[ev.event_id] = clean;
      }
    }
  });
  state.sftDraft = {
    texts: texts, checks: checks, attrs: attrs, skeletons: skeletons,
    mentions: mentions,
    mentionSpans: {}, // 声明通道的提及位置缓存(event_id → spans),按需惰性计算
    unmatched: parsed.unmatched, env: parsed.env,
  };
  state.sftSavedSig = sftSignature();
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
        Object.keys(decl).forEach(gk => (decl[gk] || []).forEach(s => {
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
