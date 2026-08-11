// chips 三层联动的计算侧(纯函数,零 DOM):chip 变更 → attrs/文本/声明提及同步
// 逐语义移植自 legacy sft.js applyChipChange 的非 DOM 部分;视图层(chip 选中态、
// token 重渲染、dirty 徽标)由 Vue 壳负责,这里只改草稿数据。
import type { AttrGroup, EventDef, MentionValue, SftDraft } from './types';
import { skeleton } from './model';
import {
  declaredSpans, groupMentionStrings, mapMentionToOption,
  replaceDeclaredSpans, swapSkeletonPrefix,
} from './spans';

function isNest(v: MentionValue | undefined | null): v is Record<string, string[]> {
  return !!v && !Array.isArray(v) && typeof v === 'object';
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
export function applyChipChange(
  draft: SftDraft, events: EventDef[], ev: EventDef, group: AttrGroup, value: string,
): void {
  const d = draft;
  const id = ev.event_id;
  const attrs = d.attrs[id] || (d.attrs[id] = {});
  const oldVal = group.multi ? '' : (attrs[group.key] as string || '');
  let added: string | null = null; // 多选组本次新增的选项(移除时为 null)
  if (group.multi) {
    const cur = Array.isArray(attrs[group.key]) ? attrs[group.key] as string[] : [];
    const on = cur.indexOf(value) >= 0;
    const next = on ? cur.filter(o => o !== value) : cur.concat(value);
    if (!on) added = value;
    attrs[group.key] = group.options.filter(o => next.indexOf(o) >= 0); // 保持 options 定义顺序
  } else {
    attrs[group.key] = attrs[group.key] === value ? '' : value;
  }
  const newVal = group.multi ? '' : (attrs[group.key] as string || '');
  const oldSk = d.skeletons[id] || '';
  const newSk = skeleton(ev, attrs);
  d.skeletons[id] = newSk;
  let text = String(d.texts[id] || '');
  // 声明通道锚点:该组声明提及所在的 span(仅声明通道 + 单选换值时非空)
  const decl = d.mentions ? (d.mentions[id] || null) : null;
  // 多选组的声明提及同步:嵌套格式(选项名 → 提及串数组)按选项键直接增删;
  // 旧扁平数组按别名映射归选项。取消选中 → 该选项的声明提及移出 decl(重渲染
  // 即为纯文本,保存时不再上送),暂存 mentionsOff 供重新选中时恢复;
  // 新选中 → 暂存中仍在文本里的提及加回 decl(重新标注)。
  // 未映射提及两边都不动;span 缓存作废,骨架分支与重渲染按新声明重算
  if (group.multi && decl) {
    const offAll = d.mentionsOff || (d.mentionsOff = {});
    const off = offAll[id] || (offAll[id] = {});
    const cur = decl[group.key];
    const stash = off[group.key];
    if (isNest(cur) || (!cur && isNest(stash))) {
      // 新格式:嵌套 per-option 绑定,选项名即键,无需别名映射
      if (!added) { // 取消选中 value
        const moved = (isNest(cur) && cur[value]) || [];
        if (moved.length && isNest(cur)) {
          const rest: Record<string, string[]> = {};
          Object.keys(cur).forEach(o => { if (o !== value && cur[o].length) rest[o] = cur[o]; });
          if (Object.keys(rest).length) decl[group.key] = rest;
          else delete decl[group.key];
          off[group.key] = Object.assign(isNest(stash) ? stash : {}, { [value]: moved });
          d.mentionSpans[id] = null;
        }
      } else { // 新选中 added:恢复暂存提及
        const cand = (isNest(stash) && stash[added]) || [];
        const restored = cand.filter(s => text.indexOf(s) >= 0);
        if (restored.length && isNest(stash)) {
          const gone = cand.filter(s => restored.indexOf(s) < 0);
          if (gone.length) stash[added] = gone; else delete stash[added];
          const nest = isNest(decl[group.key]) ? decl[group.key] as Record<string, string[]>
            : (decl[group.key] = {} as Record<string, string[]>) as Record<string, string[]>;
          nest[added] = restored;
          d.mentionSpans[id] = null;
        }
      }
    } else if (!added) { // 旧扁平数组:取消选中 value
      const keep: string[] = [], moved: string[] = [];
      ((cur as string[]) || []).forEach(s => {
        (mapMentionToOption(group, s) === value ? moved : keep).push(s);
      });
      if (moved.length) {
        decl[group.key] = keep;
        off[group.key] = ((off[group.key] as string[]) || []).concat(moved);
        d.mentionSpans[id] = null;
      }
    } else { // 旧扁平数组:新选中 added,恢复暂存提及
      const remain: string[] = [], restored: string[] = [];
      ((off[group.key] as string[]) || []).forEach(s => {
        if (mapMentionToOption(group, s) === added && text.indexOf(s) >= 0) restored.push(s);
        else remain.push(s);
      });
      if (restored.length) {
        off[group.key] = remain;
        decl[group.key] = ((decl[group.key] as string[]) || []).concat(restored);
        d.mentionSpans[id] = null;
      }
    }
  }
  const declList = decl && oldVal && newVal ? groupMentionStrings(decl[group.key]) : [];
  const declSpans = declList.length ? declaredSpans(d, ev, text) : null;
  const mySpans = declSpans ? declSpans.filter(sp => sp.group === group.key) : [];
  if (mySpans.length) {
    // 声明提及即锚点:仅 span 位置替换为新值(无别名扩展,背景句同形词不动;
    // 链式编辑时上一次写入的新值靠 span 位置继续锚定,不会误伤背景);
    // 声明提及同步为新值(保存时随 attr_mentions 落盘,后端子串校验方能通过)
    const r = replaceDeclaredSpans(text, declSpans!, group.key, newVal);
    text = r.text;
    decl![group.key] = [newVal];
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
    const spans = decl ? declaredSpans(d, ev, text) : null;
    const sw = swapSkeletonPrefix(text, spans || [], oldSk, newSk);
    text = sw.text;
    if (spans) d.mentionSpans[id] = sw.spans;
  }
  d.texts[id] = text;
}
