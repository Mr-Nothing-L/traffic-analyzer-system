// SFT 编辑器纯逻辑模块:类型定义
// 对齐后端 traffic_analyzer/web/evidence_schema.py 的 SftSample 与
// traffic_analyzer/config/event_options.yaml 的封闭枚举配置。

// 属性组定义(event_options.yaml 经 GET /api/config/events 下发)
export interface AttrGroup {
  key: string;        // 属性键(如 lane_type / work_elements)
  label: string;      // 中文名(必填缺失软提醒用)
  required?: boolean; // 必填(未选仅软提醒,不拦截保存)
  multi?: boolean;    // 多选组:值为选项数组;否则为单选字符串
  options: string[];  // 封闭选项集,顺序即 UI 与骨架槽位顺序
}

// 事件定义(GET /api/config/events 数组元素)
export interface EventDef {
  event_id: number;   // 标注文档 v4.5 的 action 编号(9 = 正常占位,不出现)
  name_zh: string;    // 事件中文名(think 段落前缀、结论行 classN 后缀)
  is_active: boolean; // 未激活事件:不渲染 chips、不勾检出、提及/段落保存时丢弃
  options?: AttrGroup[];
}

// 声明提及值:单选组与旧扁平多选为字符串数组;
// 新格式多选组为嵌套「选项名 → 提及串数组」
export type MentionValue = string[] | Record<string, string[]>;

// 事件的结构化属性值(单选 string / 多选 string[])
export type EventAttrs = Record<string, string | string[]>;

// SFT 样本(GET /api/results/{stem} 响应的 sft_label;字段与 SftSample 对齐)
export interface SftLabel {
  chunk: unknown;
  idx: unknown;
  action: number[];
  description: string;
  start_timestamp: unknown;
  end_timestamp: unknown;
  chunk_name: unknown;
  event_attributes?: Record<string, Record<string, unknown>> | null;
  attr_mentions?: Record<string, Record<string, MentionValue>> | null;
}

// 声明提及位置 span:按 start 升序、互不重叠
export interface DeclSpan {
  start: number;
  end: number;
  group: string; // 属性组 key
  str: string;   // 当前文本中该 span 的表面串
}

// 文本变更区间(供 span 平移)
export interface SpanEdit {
  start: number;
  end: number;
  newLen: number;
}

// 编辑草稿(纯数据;mentionSpans 为 span 缓存,按需惰性计算)
export interface SftDraft {
  texts: Record<number, string>;
  checks: Record<number, boolean>;
  attrs: Record<number, EventAttrs>;
  skeletons: Record<number, string>;
  mentions: Record<number, Record<string, MentionValue>> | null; // 样本无 attr_mentions 时整体 null
  mentionsOff?: Record<number, Record<string, MentionValue>>;    // 多选取消选中的暂存提及
  mentionSpans: Record<number, DeclSpan[] | null>;
  unmatched: string[];
  env: Record<string, string>; // 天气/时间/场景
  // 手动勾选检出插入骨架前的原文快照(纯内存,不进保存签名):取消勾选时仅当
  // 文本与「骨架+快照原文」逐字符一致才撤销,防止误删模型/人工原文;
  // 磁盘载入与模型检出事件无此标记,取消勾选不碰其文本
  skeletonOrig?: Record<number, string>;
}

// 保存时重建的四字段(PUT body 的可编辑部分)
export interface SftRevision {
  description: string;
  action: number[];
  event_attributes: Record<number, EventAttrs> | null;
  attr_mentions: Record<number, Record<string, MentionValue>> | null;
}

// PUT /api/results/{stem}/sft 的请求体
export type SftPutPayload = SftLabel & SftRevision & { base_sig?: string };

// tokenizeSpans 输出:token 段(group 非空)与纯文本段(group 为 null)
export interface TokenSegment {
  text: string;
  group: string | null;
}
