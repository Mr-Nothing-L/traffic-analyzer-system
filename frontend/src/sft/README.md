# sft/ — SFT 编辑器纯逻辑模块(零 DOM)

从 legacy `traffic_analyzer/web/static/js/`(sft_model.js / sft_spans.js / sft.js 夹带的纯逻辑)
逐语义移植为纯 TS:数据进数据出,无 DOM / window / fetch。Vue 壳只负责渲染与事件绑定。

## legacy 纯逻辑函数清单 → 本模块对照

| legacy | 新位置 | 输入 → 输出 | 说明 |
|---|---|---|---|
| sft_model.js `SFT_SKELETON_TEMPLATES` | model.ts 同名常量 | — | 骨架模板:event_id → 固定文字/槽位从句 |
| `evOptions(ev)` | model.ts 同名 | EventDef → AttrGroup[] | 属性组(无 options 字段回退空) |
| `skeleton(ev, attrs)` | model.ts 同名 | → string | 骨架句,空值从句整体省略,多选「、」连接 |
| `sanitizeFileAttrs(ev, raw)` | model.ts 同名 | → EventAttrs | 只保留封闭枚举内合法键值 |
| `missingRequired(ev, attrs)` | model.ts 同名 | → string[] | 必填缺失组中文名(软提醒) |
| `parseSftDescription(desc, events)` | model.ts 同名 | → {sections, unmatched, env} | think 按空行分段匹配「事件名:」;answer 提取天气/时间/场景 |
| `sftEnvLines()` | model.ts `envLines(env)` | → string[] | 空值回退「未知」;不再读全局 state |
| `sftConclusionLines()` | model.ts `conclusionLines(events, checks)` | → string[] | 由「检出」勾选生成结论行 |
| `buildSftRevision()` | model.ts `buildRevision(draft, events)` | → SftRevision | 重建 description/action/event_attributes/attr_mentions;提及按当前文本子串过滤 |
| `sftSignature()` | model.ts `signature(draft, events)` | → string | revision 的 JSON 串 |
| —(sft.js `updateSftDirty` 的判定) | model.ts `isDirty(draft, events, savedSig)` | → boolean | 签名比对 |
| `initSftDraft(sft)` | model.ts `initDraft(events, sft)` | → {draft, savedSig} | 从 sft_label 初始化草稿;写 state 改为返回值 |
| sft_spans.js `SFT_ATTR_ALIASES` / `SFT_MULTI_MAP_EXTRA` | spans.ts 同名常量 | — | 别名表(仅归选项用)/ 映射专用扩展词 |
| `groupMentionStrings(v)` | spans.ts 同名 | MentionValue → string[] | 扁平/嵌套统一展开 |
| `computeDeclSpans(decl, text, prev)` | spans.ts 同名 | → DeclSpan[] | 声明串精确子串定位;背景同形词不动 |
| `spansMatchText(spans, text)` | spans.ts 同名 | → boolean | 缓存吻合校验 |
| `declaredSpans(ev, text)` | spans.ts `declaredSpans(draft, ev, text)` | → DeclSpan[] | 带缓存;失效按声明串重算 |
| `tokenizeSpansHtml(spans, t)` | spans.ts `tokenizeSpans(spans, t)` | → TokenSegment[] | HTML 拼接改为分段数据,Vue 壳自行渲染 |
| `replaceDeclaredSpans` / `shiftSpansForEdits` / `swapSkeletonPrefix` | spans.ts 同名 | — | 位置锚定替换 / span 平移 / 骨架前缀就地换新 |
| `aliasesOf(value)` / `mapMentionToOption(group, mention)` | spans.ts 同名 | — | 书写形态(自身+别名,长度降序)/ 旧扁平多选归选项 |
| sft.js `applyChipChange` 计算侧 | chips.ts `applyChipChange(draft, events, ev, group, value)` | 原地改 draft | chips 三层联动:attrs → 声明提及/骨架 → 文本 |
| sft.js `saveSft` 载荷构造 | model.ts `buildPutPayload(draft, events, sft, baseSig?)` | → SftPutPayload | `Object.assign({}, sft_label, revision)` + `base_sig` |

## Vue 壳用法要点

- `const { draft, savedSig } = initDraft(events, sftLabel)`;之后所有编辑都改 `draft`。
- 文本输入:直接写 `draft.texts[id]`;blur 时用 `declaredSpans(draft, ev, text)` + `tokenizeSpans` 重取 token 段。
- chip 点击:`applyChipChange(draft, events, ev, group, value)`,然后用 `draft.texts[id]` 与 `tokenizeSpans(declaredSpans(...), text)` 重渲染。
- dirty:`isDirty(draft, events, savedSig)`;必填提醒:`missingRequired(ev, draft.attrs[id])`。
- 保存:`buildPutPayload(draft, events, sftLabel, baseSig)`,`baseSig` 来自 GET /api/results/{stem}
  响应的 `file_sig`(注意:body 字段名是 `base_sig`;与证据的 `evidence_sig` 是两个字段)。
- chips 仅在 `draft.mentions && draft.mentions[event_id]` 非空时渲染(声明通道);否则纯文本卡。

## 已知 legacy 口径(有意锁定,未「修复」)

- `swapSkeletonPrefix` 的 edit.newLen = `newSk.length - ce`(含公共前缀 cs 的偏移):cs > 0 且
  骨架后还有 span 时平移量偏大,缓存失锚后由 `spansMatchText` 校验 + `computeDeclSpans` 重算兜底。
  spans.test.ts「骨架变长」用例锁定该行为。
- 全角冒号 U+FF1A 仅作分隔符(事件名前缀/env 行/最终结论);文本内的逗号为半角(与 legacy 一致)。

测试:`__tests__/`(vitest,58 例),夹具对齐 `traffic_analyzer/config/event_options.yaml`
与 `交通事件数据标注说明文档_v4.5.md` 的事件名/action 编号。
