/* --------------------------------------- SFT 声明提及 span 引擎(纯函数) */
import { esc } from './util.js';
import { state } from './state.js';

// 选项关键词别名(选项文本本身总是首个关键词):仅供 mapMentionToOption 把旧样本
// 扁平多选数组里的声明提及串归到选项;不再用于分词/替换/回填
export const SFT_ATTR_ALIASES = {
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

// --- 声明提及分词:样本含 attr_mentions 且声明了该事件时走「声明提及通道」 ------
// 声明通道:模型声明的表面串是唯一权威——只标注/只替换声明串本身出现的位置
// (span 锚定,无正则、无别名扩展),背景句中的同形词一律不动;
// 未声明的事件与无 attr_mentions 的旧样本为纯文本卡(无 chips、无 token)。
// 组的声明提及串列表:单选组与旧扁平多选为字符串数组;新格式多选组为
// 「选项名 → 字符串数组」的嵌套对象,展开为全部提及串(只保留组归属)
export function groupMentionStrings(v) {
  if (Array.isArray(v)) return v;
  if (v && typeof v === 'object') {
    return Object.keys(v).reduce((acc, k) => acc.concat(v[k] || []), []);
  }
  return [];
}

// 声明提及的位置 span 列表:[{start, end, group, str}],按 start 升序、互不重叠。
// 缓存于 state.sftDraft.mentionSpans[event_id];仅当缓存与当前文本不再吻合
// (人工编辑过)时才按声明串精确子串搜索重算——重算时若同一声明串的出现次数
// 多于旧 span 数(chip 同步后的裸值如 小型车 可能在背景句另有同形词),
// 按「与旧 span 最近」原则取前 N 个,保证链式编辑后背景同形词仍不被误标。
export function computeDeclSpans(decl, text, prev) {
  const cand = [];
  Object.keys(decl).forEach(gk => {
    const strs = groupMentionStrings(decl[gk]);
    strs.forEach(s => {
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
        const declCount = strs.filter(x => x === s).length;
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

export function spansMatchText(spans, text) {
  return spans.every(sp => text.slice(sp.start, sp.end) === sp.str);
}

// 取该事件的声明提及 span(声明通道专用);缓存失效时按声明串精确搜索重算
export function declaredSpans(ev, text) {
  const d = state.sftDraft;
  const decl = (d && d.mentions) ? (d.mentions[ev.event_id] || null) : null;
  const id = ev.event_id;
  if (!d.mentionSpans) d.mentionSpans = {};
  const cached = d.mentionSpans[id];
  if (cached && spansMatchText(cached, text)) return cached;
  const spans = computeDeclSpans(decl, String(text || ''), cached);
  d.mentionSpans[id] = spans;
  return spans;
}

// 声明通道的 token 渲染:仅 span 位置标注为 token,其余一律纯文本
export function tokenizeSpansHtml(spans, t) {
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
export function replaceDeclaredSpans(text, spans, groupKey, newVal) {
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
export function shiftSpansForEdits(spans, edits) {
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
export function swapSkeletonPrefix(text, spans, oldSk, newSk) {
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

// 选项值的全部书写形态(自身 + 别名),按长度降序:最长优先匹配,
// 避免短别名吃掉长别名(如 客车 匹配进 大客车、小车 匹配进 小型车)
export function aliasesOf(value) {
  return [value].concat(SFT_ATTR_ALIASES[value] || []).sort((a, b) => b.length - a.length);
}

// 旧扁平多选数组「声明提及 → 选项」映射专用的扩展关键词:仅用于把声明提及串
// 归到选项,不进 SFT_ATTR_ALIASES(如 人员 过于泛化,进入别名表会让旧样本里
// 「无人员行走」之类的提及误归到 施工人员)
export const SFT_MULTI_MAP_EXTRA = { '施工人员': ['人员'] };

// 声明提及串映射到组内选项(仅供旧扁平多选数组;新格式为嵌套 per-option 绑定,
// 无需映射):提及中命中某选项的书写形态(选项文本 + 别名 + 映射专用扩展词),
// 最长命中优先,并列按 options 定义顺序;均不命中返回 null
// (未映射提及保持现状:仍标注为 token、仍留在 attr_mentions)
export function mapMentionToOption(group, mention) {
  let best = null, bestLen = 0;
  group.options.forEach(opt => {
    aliasesOf(opt).concat(SFT_MULTI_MAP_EXTRA[opt] || []).forEach(a => {
      if (a.length > bestLen && mention.indexOf(a) >= 0) { best = opt; bestLen = a.length; }
    });
  });
  return best;
}
