# design.md — 高速交通事件分析台 Web UI 设计系统(锁定)

> Hallmark 系统管理文件。本文件是新前端(`frontend/`,Vue 3 + TS + Naive UI)的唯一设计源。
> 所有组件只准引用命名 token;缺值先在此文件与 `frontend/src/styles/tokens.css` 中提升为新 token,再引用。
> 后续 Hallmark 运行必须遵循本文件,不做主题轮换(diversification 规则在本项目反转:页面共享系统)。

- **Genre**:modern-minimal(开发者工具/内部工作台)
- **Tone**:technical / utilitarian——信息密度优先,无营销页节奏
- **Anchor**:保留 legacy 界面的视觉 DNA:米白纸面 + 烧橙 accent + 缝合像素字体骨架

## 1. 调色板(canonical hex = 现状源;OKLCH 由 tokens.css 承载,两者一一对应)

| Token | hex(源) | 用途 |
|---|---|---|
| `--color-paper` | `#F7F4EE` | 页面底色 |
| `--color-card` | `#FFFFFF` | 卡片面 |
| `--color-border` | `#E8E2D5` | 常规描边 |
| `--color-line-strong` | `#C9C0AF` | 强描边/滚动条 |
| `--color-text` | `#2A2620` | 主文字 |
| `--color-text2` | `#6B6257` | 次级文字 |
| `--color-accent` | `#D97757` | 主 accent(烧橙) |
| `--color-accent-hover` | `#C4664A` | accent hover |
| `--color-accent-soft` | `#F6E3DA` | accent 软底 |
| `--color-on-accent` | `#FFFFFF` | accent 上文字 |
| `--color-sage` / `--color-sage-soft` | `#7A9B76` / `#E6EEE3` | 成功/通过 |
| `--color-red` / `--color-red-soft` | `#B26B5B` / `#F3E2DD` | 失败/危险 |
| `--color-blue` / `--color-blue-soft` | `#3E7CB1` / `#E2ECF4` | 信息/运行中 |
| `--color-gold` | `#C9A227` | 警示/待审 |
| `--color-surface-2..5` | `#FBF9F4`/`#EFEAE0`/`#F1EDE4`/`#FCFAF6` | 次级表面 |
| `--color-hover-bg` | `#F4EFE5` | 悬停底 |
| `--color-stage-bg` | `#1C1A17` | 视频/证据舞台深色底(阶段 4 提升) |
| `--color-accent-deep` | `#F0C4AB` | chip hover 联动加深底(阶段 5 提升) |
| `--color-dot-muted` | `#D8D1C2` | 排队状态点(阶段 5 提升) |
| `--color-stage-bg` | `#1C1A17` | 视频/逐帧/证据画布舞台深色底(legacy pv-wrap/ev-stage) |

阴影/圆角:`--shadow`(1px 2px + 4px 14px, 5–6% 黑)、`--shadow-hover`、`--radius: 12px`、`--radius-sm: 8px`。

## 2. 字体角色(2+1 纪律)

- `--font-pixel`:"Fusion Pixel" 像素字体(woff2 子集,SIL OFL 1.1)→ **仅 UI 骨架**:工具条、侧栏、卡片头、按钮、空态、泳道面板。display 角色。
- `--font-sans`:系统黑体栈 → 正文、报告、表格内容、**文件名(精确辨认)**。
- `--font-mono`:SFMono/Consolas/Menlo → 日志、代码、进度数值。
- **标题一律 roman,禁止斜体标题**;强调用字重/accent 色/下划线,不用斜体。

## 3. 间距与尺度

4pt 基准:`--space-xs:4px --space-sm:8px --space-md:16px --space-lg:24px --space-xl:32px --space-2xl:48px`。
文本尺度:`--text-xs:11px --text-sm:12px --text-md:13px --text-lg:15px --text-xl:18px --text-2xl:22px --text-display:28px`(工具台密度,比营销页小一档,沿用 legacy 密度)。

## 4. Motion(motion-cut)

- 只动 `transform`/`opacity`;三命名 easing:`--ease-out: cubic-bezier(.22,1,.36,1)`、`--ease-in: cubic-bezier(.64,0,.78,0)`、`--ease-in-out: cubic-bezier(.83,0,.17,1)`;时长 `--dur-fast:120ms --dur-med:200ms`。
- `prefers-reduced-motion: reduce` → 全部折叠为 ≤150ms opacity 淡入。
- 进度反馈用像素块动画(legacy 特色),不引入第三方动效库。

## 5. 组件 8 态纪律(强制)

每个交互组件必须有样式/行为覆盖:default · hover · `:focus-visible`(≥3:1 可见环,瞬时出现不动画)· `:active` · disabled · loading · error · success。
silent success 优先(无庆功 toast);破坏性操作用 optimistic update + 可撤销,不用确认弹窗(legacy 的删除/取消既有语义除外,保持一致)。

## 6. Honest copy 硬门

- 看板/指标卡只渲染 API 真实返回;无数据 → 空态,禁止编造数字/百分比/趋势。
- 不发明用户证言、logo 墙、"+47%"式指标。
- 文案全中文,动词开头("加载工作区""开始推理"),沿用 legacy 措辞。

## 7. 响应式底线

内部桌面工具,≥768px 完善;底线:`html/body overflow-x: clip`;grid 图轨 `minmax(0,1fr)`;长词 `overflow-wrap:anywhere`;可点击文本不折行。

## 8. 禁区(AI slop)

- 不重绘假浏览器/假窗口 chrome;不用 hero→3 特性→CTA 节奏;不用渐变紫蓝 AI 配色;
- 组件内禁止 inline hex/OKLCH(只准 `var(--token)`);禁止 emoji 当图标(用 Naive UI 图标或像素块)。

## Provenance

由 `docs/web_ui_review_and_refactor_plan.md` 评审决策产生(2026-08-04);调色板/字体提取自 `traffic_analyzer/web/static/css/tokens.css`(legacy `:root`),非新发明。
