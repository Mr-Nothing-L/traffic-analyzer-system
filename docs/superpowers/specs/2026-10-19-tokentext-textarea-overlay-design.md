# TokenText 迁移 textarea + overlay — 设计文档

日期:2026-10-19  状态:待确认(未获用户批准前不实现)

## 背景:为什么必须迁

`frontend/src/components/detail/sft/TokenText.vue` 当前用 `contenteditable` div 承接
自由编辑,Vue 声明式渲染负责 token 分段高亮。两者对同一棵 DOM 的所有权冲突已两次
产生线上级 bug:

1. **98fd82e**(富文本 blur 重渲染清空):人工编辑让浏览器直接改动了
   contenteditable 的内部结构后,vdom 与真实 DOM 结构性分歧,blur 时的增量
   patch(patchKeyedChildren)把新分段写进游离节点、卸载存活文本节点,文本框
   视觉清空(草稿数据完好)。
2. **ced6b3f**(取消勾选误删模型原文):骨架撤销的兜底修复,同样源于
   contenteditable 的人工 DOM 与 vdom 预期不一致。

现行缓解是 `renderKey` 整段重挂载(不与浏览器改过的 DOM 做增量对账),这治标不
治本:contenteditable 的 DOM 所有权天然不属于 vdom,任何「编辑期间浏览器改
结构 + Vue 增量渲染」的组合都是潜在分歧点。sft/README 与 TokenText 文件头均已
记录长期方案:**textarea 承接输入、overlay 层渲染高亮,渲染与编辑彻底解耦**。

## 目标形态

```
.sft-richtext-wrap (position: relative, 自适应高度)
├── .sft-overlay (absolute inset-0, pointer-events: none)
│     └── 分段渲染:纯文本节点 + <span class="sft-tok" data-attr="组">(sft/spans.ts 分段引擎)
└── textarea.sft-richtext (relative, 背景透明, color: transparent, caret-color: 可见)
      ↑ 唯一的输入/光标/选区/输入法宿主;z 序在 overlay 之上
```

textarea 的文字透明、光标不透明,用户「看到」的文字全部来自下方 overlay;两层
排版度量完全一致(见 §度量对齐),视觉上等价于一个带高亮的文本框。

## 核心决策:光标/选区**不需要**映射算法

textarea 原生持有光标与选区(selectionStart/selectionEnd 是纯字符串偏移),
overlay 是纯展示层不参与交互(pointer-events: none)。因此:

- **不存在 caret DOM 节点 ↔ 字符偏移的映射问题**(contenteditable 方案的万恶之
  源):光标天然在正确的视觉位置,因为可见文字与真实输入共享同一套排版度量。
- 唯一需要的「偏移 ↔ 分段」关系是纯字符串级的:`TokenSegment[]` 由
  `tokenizeSpans(declaredSpans(...), text)` 产出,每段天然携带其在文本中的
  起止偏移(串接即得)。这组纯函数下沉 `sft/caret.ts` 并单测(见 §验证),
  服务后续可选能力(如点击 token 定位光标),本期组件不依赖。

## 输入法组合键(composition)处理

textarea 原生处理输入法:`compositionstart/update/end` 期间预编辑文本(preedit
带下划线)由浏览器绘制在 textarea 自身,光标位置由浏览器维护。设计要点:

- **编辑期间只回写草稿纯文本,不重算分段**(与现行口径一致):`@input` 里
  `draft.texts[event_id] = textarea.value`,overlay 用「旧 span 缓存 + 新文本」
  渲染(见 §同步时机);不监听 composition 事件、不做任何打断。
- overlay 重渲染发生在 composition 期间也是安全的:它只改 overlay 的 vdom,
  不触碰 textarea(光标宿主),不存在 contenteditable 方案的丢光标路径。
- 精确性兜底:composition 结束(compositionend 后的下一个 input)与 blur 都会
  走完整的 `declaredSpans` 重算,预编辑期间可能出现的短暂高亮错位(毫秒级、
  仅当预编辑串恰好与声明提及重叠)在 blur 时自动纠正——与 legacy 行为等价。

## 同步时机(overlay ↔ textarea)

| 事件 | textarea 侧 | overlay 侧 |
|---|---|---|
| `input`(含 IME) | `draft.texts[id] = value`;autoGrow | 以 `prev` span 缓存重渲染:`tokenizeSpans(computeDeclSpans(decl, text, prev), text)`(声明串精确子串定位,短文本开销可忽略) |
| `blur` | — | 完整重算 `declaredSpans`(不用缓存)+「提及被删」提醒(现有逻辑原样保留) |
| chip 变更 / 外部重建草稿(重置/切换视频/保存) | `textarea.value = draft.texts[id]`(受控回写,仅在非聚焦时) | 完整重算 + `pulseGroup` 脉冲 + 淡入重触发 |
| `scroll` | 事件源 | `overlay.scrollTop/Left = textarea.scrollTop/Left` |
| paste | `preventDefault` + 手工插入纯文本(现行逻辑保留) | 同 input 路径 |

关键差异:现行 `renderKey` 整段重挂载机制**整体删除**——textarea 的 value 由
代码显式赋值、overlay 的 vdom 由 Vue 管理,两者不再共享 DOM,vdom 分歧类 bug
的成因消失。

## 度量对齐(视觉等价的前提)

两层共用同一 CSS 类组 `.sft-richtext-metrics`,以下属性逐项相同:

`font-family / font-size / line-height / letter-spacing / font-variant-emoji /
padding / border(width+style)/ box-sizing / white-space: pre-wrap /
overflow-wrap: break-word / word-break: break-all(CJK 断行一致)/ tab-size`

- textarea 侧额外:`color: transparent; caret-color: var(--color-text);
  background: transparent; resize: none; outline: none; overflow-y: auto`,
  `::placeholder` 色走 token(替代现 data-placeholder + :empty 方案)。
- overlay 侧额外:`overflow: hidden`(滚动由 textarea 主导并同步)。
- 自适应高度:沿袭现行 autoGrow(量 `textarea.scrollHeight`,>300px 出滚动条)。

## 组件对外契约(不变)

`TokenText.vue` 重写为实现细节,对外 API 保持原样,`SftEventCard.vue` 零改动:

- props:`{ ev: EventDef; hasDecl: boolean }`
- emits:`tok-hover(group | null)`(overlay 上 mouseover 命中 `.sft-tok` 反查
  `data-attr`,与现行一致)
- expose:`refresh(pulseGroup?)`(chip 联动描边脉冲入口)

样式纪律:全部颜色/间距/字号走 `tokens.css` 变量,新增脉冲/占位所需色值先提
token;禁 emoji 图标(本组件无图标)。

## 验证

### vitest(纯函数,`sft/__tests__/caret.test.ts`)

新增 `sft/caret.ts`:

- `segmentAt(segs, offset) → index | null`:偏移所在分段(段首/段尾/段间边界)。
- `segmentsConcat(segs) → text`:分段串接回全文(与原文逐字节相等)。
- 用例覆盖:空分段表;单段;段首偏移(= 段 start)、段尾偏移(= 段 end,
  归后段)、两段交界、中文多字节字符内部的偏移、末尾越界偏移。

### 手动验收清单(实现后逐项过)

1. 中文输入法连续输入(拼音预编辑 → 上屏),光标不跳、无残留高亮错位;
2. 句中插入/删除字符,token 描边随内容平移,blur 后重算正确;
3. chip 换值 → 对应 token 就地替换 + 同组 chips 描边脉冲(hover 联动正常);
4. blur 不丢内容(98fd82e 场景):blur 后再聚焦继续编辑,内容完整;
5. 切换视频/保存重建草稿:文本框内容、分段、dirty 状态正确(假复活不出现);
6. 取消勾选撤销骨干句(ced6b3f 场景):仅当文本与「骨架+快照」逐字符一致时
   撤销,模型/人工原文不误删;
7. 长文本 >300px 出滚动条,滚动时 overlay 同步不错位;
8. 粘贴富文本 → 仅保留纯文本;
9. `npx vitest run` 全绿、`npm run typecheck`、`npm run build` 通过。

## 实施步骤(确认后)

1. `sft/caret.ts` + 单测(纯函数先行);
2. 重写 `TokenText.vue`(textarea + overlay,契约不变),删除 renderKey 机制
   与 TokenSegs 函数组件;
3. 样式:抽取共享度量类,核对 token 引用;
4. 跑全量前端回归 + 手动验收清单;
5. 提交:一个独立 commit(重写类变更,消息注明 98fd82e/ced6b3f 根因闭环)。

## 风险与回退

- 风险集中在度量不一致导致的视觉错位(字符错缝/断行不同):靠共享度量类 +
  手动清单第 7 项兜底;若仍有个别环境错位,再考虑 overlay 用等宽快照校验。
- 回退:单文件重写,git revert 即可恢复 contenteditable 版本(renderKey 机制
  在恢复后仍然有效)。
