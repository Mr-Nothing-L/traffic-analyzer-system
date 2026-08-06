// SFT 草稿模型(纯函数,零 DOM):草稿与序列化
// 逐语义移植自 legacy 前端 sft_model.js(legacy 已随 web/static 删除);
// 全局 state 依赖改为显式参数(draft / events 进,数据出)。
import type {
  EventAttrs, EventDef, MentionValue, SftDraft, SftLabel, SftPutPayload, SftRevision,
} from './types';

// 骨架模板:字符串为固定文字;{slot, pre, post} 为该属性有值时输出的从句(空值整句省略)
export type SkeletonPart = string | { slot: string; pre?: string; post?: string };
export const SFT_SKELETON_TEMPLATES: Record<number, SkeletonPart[]> = {
  1: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, '停有一辆', { slot: 'vehicle_type' }],
  2: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, { slot: 'vehicle_type', pre: '有', post: '占用' }],
  3: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, '发生交通事故', { slot: 'vehicle_type', pre: ',涉及' }],
  4: [{ slot: 'direction', post: '一侧' }, '出现', { slot: 'person_type' }],
  5: [{ slot: 'direction', post: '一侧' }, '出现', { slot: 'non_motor_type' }],
  6: [{ slot: 'direction', post: '一侧' }, '出现', { slot: 'scope' }, '拥堵'],
  7: [{ slot: 'direction', post: '一侧' }, '道路施工', { slot: 'work_elements', pre: ',现场有' }],
  8: [{ slot: 'direction', post: '一侧' }, { slot: 'lane_type', post: '内' }, { slot: 'vehicle_type', pre: '有', post: '逆行' }],
};

// 事件的结构化属性组(event_options.yaml,经 /api/config/events 下发);旧版后端无该字段时回退空
export function evOptions(ev: EventDef) {
  return Array.isArray(ev.options) ? ev.options : [];
}

// 骨架句:按模板把已选属性拼成一句,空值从句整体省略
export function skeleton(ev: EventDef, attrs?: EventAttrs): string {
  const parts = SFT_SKELETON_TEMPLATES[ev.event_id];
  if (!parts) return '';
  attrs = attrs || {};
  let out = '';
  parts.forEach(p => {
    if (typeof p === 'string') { out += p; return; }
    const v = attrs![p.slot];
    if (Array.isArray(v)) {
      if (v.length) out += (p.pre || '') + v.join('、') + (p.post || '');
    } else if (v) {
      out += (p.pre || '') + v + (p.post || '');
    }
  });
  return out;
}

// 新格式:event_attributes 只保留当前选项定义内合法的键值(防御旧配置漂移)
export function sanitizeFileAttrs(ev: EventDef, raw: unknown): EventAttrs {
  const attrs: EventAttrs = {};
  if (!raw || typeof raw !== 'object') return attrs;
  const rec = raw as Record<string, unknown>;
  evOptions(ev).forEach(g => {
    const v = rec[g.key];
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
export function missingRequired(ev: EventDef, attrs?: EventAttrs): string[] {
  const miss: string[] = [];
  evOptions(ev).forEach(g => {
    if (!g.required) return;
    const v = (attrs || {})[g.key];
    if (Array.isArray(v) ? !v.length : !v) miss.push(g.label);
  });
  return miss;
}

export interface ParsedDescription {
  sections: Record<number, string>; // event_id → 段落正文(去掉「事件名:」前缀)
  unmatched: string[];              // 匹配不到任何事件名的段落(原样保留,保存时回写)
  env: Record<string, string>;      // 天气/时间/场景键值(答案区可编辑)
}

// 解析 description:think 按空行分段,匹配「事件名:」前缀;answer 提取天气/时间/场景键值
export function parseSftDescription(desc: unknown, events: EventDef[]): ParsedDescription {
  const sections: Record<number, string> = {};
  const unmatched: string[] = [];
  const env: Record<string, string> = { '天气': '', '时间': '', '场景': '' };
  const thinkM = String(desc || '').match(/<think>([\s\S]*?)<\/think>/);
  if (thinkM) {
    thinkM[1].trim().split(/\n\s*\n/).forEach(para => {
      const p = para.trim();
      if (!p) return;
      const m = p.match(/^([^：\n]{1,30})：/);
      const ev = m ? events.find(e => e.name_zh === m[1]) : undefined;
      if (ev && ev.is_active && sections[ev.event_id] === undefined) {
        sections[ev.event_id] = p.slice(m![0].length).trim();
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
  return { sections, unmatched, env };
}

// 天气/时间/场景按固定顺序重建为 answer 行;空值回退「未知」(与 core/sft_label_rewrite.py 口径一致)
export function envLines(env: Record<string, string>): string[] {
  return ['天气', '时间', '场景'].map(k => {
    const v = String((env || {})[k] || '').trim();
    return k + '：' + (v || '未知');
  });
}

// 由当前「检出」勾选生成结论行(保存与只读预览共用同一口径)
export function conclusionLines(events: EventDef[], checks: Record<number, boolean>): string[] {
  const checked = events.filter(ev => checks[ev.event_id]);
  if (!checked.length) return ['最终结论：本视频块未检出任何事件,交通状况正常。'];
  const lines = ['最终结论：本视频块检出以下事件。'];
  checked.forEach(ev => {
    lines.push('class' + ev.event_id + ': ' + ev.name_zh);
  });
  return lines;
}

// 由当前草稿重建 description 与 action(结论区按「检出」勾选重建)
// event_attributes 由 chips 生成:仅保留非空键;全部为空时输出 null(后端落盘时省略该字段)
export function buildRevision(draft: SftDraft, events: EventDef[]): SftRevision {
  const d = draft;
  const sections: string[] = [];
  events.forEach(ev => {
    const t = String(d.texts[ev.event_id] || '').trim();
    if (t) sections.push(ev.name_zh + '：' + t);
  });
  const think = sections.concat(d.unmatched).join('\n\n');
  const checked = events.filter(ev => d.checks[ev.event_id]);
  const answerLines = envLines(d.env).concat(conclusionLines(events, d.checks));
  const attrsOut: Record<number, EventAttrs> = {};
  events.forEach(ev => {
    const a = d.attrs && d.attrs[ev.event_id];
    if (!a) return;
    const clean: EventAttrs = {};
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
  let mentionsOut: Record<number, Record<string, MentionValue>> | null = null;
  if (d.mentions) {
    const mo: Record<number, Record<string, MentionValue>> = {};
    events.forEach(ev => {
      if (!ev.is_active || !evOptions(ev).length) return;
      const raw = d.mentions![ev.event_id];
      if (!raw) return;
      const t = String(d.texts[ev.event_id] || '').trim();
      const clean: Record<string, MentionValue> = {};
      Object.keys(raw).forEach(k => {
        const v = raw[k];
        if (Array.isArray(v)) {
          const keep = v.filter(s => s && t.indexOf(s) >= 0);
          if (keep.length) clean[k] = keep;
        } else if (v && typeof v === 'object') {
          // 嵌套 per-option 绑定:逐选项过滤,空选项与空组一并省略
          const nest: Record<string, string[]> = {};
          Object.keys(v).forEach(o => {
            const keepOpt = (v[o] || []).filter(s => s && t.indexOf(s) >= 0);
            if (keepOpt.length) nest[o] = keepOpt;
          });
          if (Object.keys(nest).length) clean[k] = nest;
        }
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

export function signature(draft: SftDraft, events: EventDef[]): string {
  return JSON.stringify(buildRevision(draft, events));
}

export function isDirty(draft: SftDraft, events: EventDef[], savedSig: string): boolean {
  return signature(draft, events) !== savedSig;
}

// 从 sft 样本初始化编辑草稿(检出初值 = action 反映射;未激活事件留空不勾)
// mentions:样本含 attr_mentions 时按事件声明的表面串(按当前选项定义清洗;
//   多选组兼容嵌套「选项名 → 字符串数组」与旧扁平数组),该事件走声明通道
//   分词/锚定并渲染 chips;样本无 attr_mentions 时整体为 null,事件卡退化为
//   纯文本(无 chips、无 token,不做任何启发式)
// attrs:取文件 event_attributes(按当前选项定义清洗),随保存原样带回;
//   无 attr_mentions 的样本 attrs 仅用于保数据,不渲染 options UI、不回猜
export function initDraft(events: EventDef[], sft: SftLabel): { draft: SftDraft; savedSig: string } {
  const parsed = parseSftDescription(sft.description, events);
  const actions = Array.isArray(sft.action) ? sft.action : [];
  const hasFileAttrs = sft.event_attributes != null && typeof sft.event_attributes === 'object';
  const fileAttrs = hasFileAttrs ? sft.event_attributes! : {};
  const rawMentions = (sft.attr_mentions != null && typeof sft.attr_mentions === 'object') ? sft.attr_mentions : null;
  const texts: Record<number, string> = {}, checks: Record<number, boolean> = {};
  const attrs: Record<number, EventAttrs> = {}, skeletons: Record<number, string> = {};
  const mentions: Record<number, Record<string, MentionValue>> | null = rawMentions ? {} : null;
  events.forEach(ev => {
    if (ev.is_active) {
      texts[ev.event_id] = parsed.sections[ev.event_id] || '';
      checks[ev.event_id] = actions.indexOf(ev.event_id) >= 0;
    } else {
      texts[ev.event_id] = '';
      checks[ev.event_id] = false;
    }
    // 声明提及仅保留已激活且有选项组的事件;空数组与空组一律丢弃
    // (未激活事件的段落在 parseSftDescription 已丢弃,保留其提及保存必遭后端 422;
    //  无选项组的事件如实线变道,后端校验「no options defined」同样拒绝)
    if (mentions && ev.is_active && evOptions(ev).length) {
      const raw = rawMentions![ev.event_id] || rawMentions![String(ev.event_id)];
      if (raw && typeof raw === 'object') {
        const clean: Record<string, MentionValue> = {};
        evOptions(ev).forEach(g => {
          const v = raw[g.key];
          if (Array.isArray(v)) { // 扁平数组(单选组及旧样本多选组)
            const arr = v.filter(s => typeof s === 'string' && s);
            if (arr.length) clean[g.key] = arr;
          } else if (g.multi && v && typeof v === 'object') {
            // 新格式多选组:嵌套「选项名 → 字符串数组」,选项名按当前定义清洗
            const nest: Record<string, string[]> = {};
            Object.keys(v).forEach(o => {
              if (g.options.indexOf(o) < 0 || !Array.isArray(v[o])) return;
              const arr = v[o].filter(s => typeof s === 'string' && s);
              if (arr.length) nest[o] = arr;
            });
            if (Object.keys(nest).length) clean[g.key] = nest;
          }
        });
        if (Object.keys(clean).length) mentions[ev.event_id] = clean;
      }
    }
    // attrs 取文件 event_attributes(按当前选项定义清洗),随保存原样带回——
    // 无声明提及的样本虽为纯文本卡(无 chips),已有结构化标注不得因保存丢失;
    // skeletons 仅供 chips 文本联动,仅在有声明提及(渲染 chips)时初始化
    if (ev.is_active && evOptions(ev).length && hasFileAttrs) {
      attrs[ev.event_id] = sanitizeFileAttrs(
        ev, fileAttrs[ev.event_id] || fileAttrs[String(ev.event_id)]);
      if (mentions && mentions[ev.event_id]) {
        skeletons[ev.event_id] = skeleton(ev, attrs[ev.event_id]);
      }
    }
  });
  const draft: SftDraft = {
    texts, checks, attrs, skeletons,
    mentions,
    mentionSpans: {}, // 声明通道的提及位置缓存(event_id → spans),按需惰性计算
    unmatched: parsed.unmatched, env: parsed.env,
  };
  return { draft, savedSig: signature(draft, events) };
}

// 保存载荷:等价 legacy sft.js saveSft 的
//   payload = Object.assign({}, sft_label, buildSftRevision());
//   if (baseSig) payload.base_sig = baseSig
// baseSig 即 GET /api/results/{stem} 响应里的 file_sig(乐观锁;与证据的 evidence_sig 是两个字段)
export function buildPutPayload(
  draft: SftDraft, events: EventDef[], sft: SftLabel, baseSig?: string | null,
): SftPutPayload {
  const payload = Object.assign({}, sft, buildRevision(draft, events)) as SftPutPayload;
  if (baseSig) payload.base_sig = baseSig;
  return payload;
}
