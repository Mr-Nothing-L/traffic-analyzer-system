# Web 界面诊断报告与重构计划

> 日期:2026-08-04
> 范围:`traffic_analyzer/web/`(FastAPI 后端 + 原生 JS 单页应用)
> 性质:评审文档,供排期前评审。本文不含代码改动。

---

## 0. 决策记录(评审前置约束)

| # | 问题 | 结论 |
|---|------|------|
| 1 | 成果级别 | 诊断 + 修复计划(本文档) |
| 2 | 使用场景 | 内部工具,桌面 Chrome 为主 → 移动端 / i18n / 可访问性**降级**为低优先 |
| 3 | UI 演进 | 未来 6–12 个月**持续增长** → 手工状态管理不可持续 |
| 4 | 前端架构 | **完整构建链重构:Vite + Vue 3 + TypeScript** |
| 5 | 迁移节奏 | **绞杀者模式**渐进迁移,旧界面全程可用,逐视图切换 |
| 6 | 后端范围 | 纳入 **SSE 改造 + 结构化进度契约**;串行任务队列**不动** |
| 7 | 视觉方向 | 保留像素风/Claude 浅色系个性,样式 **design token 化 + Naive UI** 主题定制 |
| 8 | SFT 编辑器 | 核心逻辑抽离为纯 TS 模块,Vue 组件只做渲染壳 |
| 9 | 组件库 | **Naive UI**(原生 TS、CSS 变量主题、按需引入) |

---

## 1. 现状摘要

- 后端:FastAPI(`app.py:55` 工厂模式),10 个 `APIRouter` 按领域拆分;推理走子进程队列(`jobs.py`),视频靠 ffmpeg 转码 LRU(`video_stream.py`),自建 HMAC cookie 认证(`auth.py:183-206`)。
- 前端:**无框架、无构建工具、无模板引擎**。`static/index.html` 骨架 + ES modules;全局可变单例 `state.js:4`(挂 `window.state`);47 处字符串拼 `innerHTML` 渲染;手写 `esc()` 防 XSS。
- 规模:前端 JS ~4900 行(22 个模块)+ `style.css` **1899 行**(全项目最大单文件)。
- 代码质量评价:锁、竞态、降级链处理认真,注释详尽;问题集中在**无框架架构的自然腐化**与**非功能需求欠账**,而非代码坏习惯。

## 2. 不足清单(严重度 × 修复成本 × 依赖)

严重度:P0 = 正在造成实际损害;P1 = 阻碍增长/维护;P2 = 欠账,可缓。
成本:S < 1 天,M = 数天,L = 周级。

### P0 — 工程卫生

| ID | 问题 | 位置 | 成本 |
|----|------|------|------|
| P0-1 | Mock 体系(~130KB)被 `main.js` 静态导入进生产包;`mock_data.js`(105KB)内含开发者机器绝对路径硬编码 | `main.js:8`, `mock_data.js:2` | S |
| P0-2 | 推理进度靠解析子进程 stdout 的 `[x/4]` 字符串契约——任何日志格式改动都会静默破坏进度条 | `jobs.py` 头注释 | M |
| P0-3 | `window.state` 调试句柄留在生产 | `state.js:33` | S |

### P1 — 架构腐化(增长的直接障碍)

| ID | 问题 | 位置 | 成本 |
|----|------|------|------|
| P1-1 | 无框架手写 SPA:全局可变单例 + 手动 `cleanups` 生命周期登记,易漏(`preview.js:80` 注释即此类 bug) | `state.js:27` 等 | L(重构解决) |
| P1-2 | 字符串拼 HTML 渲染(47 处 `innerHTML`),脆弱且阻碍组件化;零散内联样式混入 JS | `tree.js:100-118`, `dashboard.js:54-67`, `preview.js:55,115-125` | L(重构解决) |
| P1-3 | `style.css` 单文件 1899 行 + 补丁式 `usability.css` + login 内嵌样式,三处样式来源 | `style.css`, `usability.css`, `login.html:8-60` | M(并入 token 化) |
| P1-4 | 三个独立轮询循环(jobs 1.5s / dashboard 1.5s / presence 10s),无 SSE/WebSocket;`dashSnap` 字符串比对 = 手写 poor man's VDOM | `jobs.js:134-158`, `main.js:168`, `dashboard.js:36-39` | M(SSE 改造) |

### P2 — 欠账(内部工具场景下刻意降级)

| ID | 问题 | 处置 |
|----|------|------|
| P2-1 | 可访问性弱:文件树 `<div>`+click 无键盘导航,证据拖拽纯鼠标 | 新组件顺手补 `tabindex`/键盘事件,不立专项 |
| P2-2 | 无暗色模式 | token 化后成本骤降,列为可选项 |
| P2-3 | 移动端"能用级" | 内部桌面工具,不投入 |
| P2-4 | 无 i18n(全中文硬编码) | 无对外计划,不投入 |
| P2-5 | 串行任务队列(一次一个推理) | **明确不在本期范围**,如需并发另立项 |
| P2-6 | `dashboard.js:44` 筛选/分页状态为模块级全局 | 重构时自然消解 |

## 3. 目标架构

```
traffic_analyzer/web/
├── app.py …(API 层,基本不动)
├── realtime.py (新)          # SSE 总线:jobs / dashboard / presence 三合一
├── jobs.py (改)              # 进度改为结构化文件契约,事件推到 realtime
└── static/
    ├── legacy/               # 现有旧界面,绞杀者模式期间全程可用
    └── app/ (新,Vite 构建产物挂载于 /v2)

frontend/ (新,Vite 工程)
├── src/
│   ├── main.ts, router/, stores/ (Pinia)
│   ├── api/ (typed fetch 封装,OpenAPI 类型可选)
│   ├── components/ (Naive UI + 像素风主题)
│   ├── views/ (Tree → Dashboard → Preview → SFT,按迁移顺序)
│   └── sft/ (纯 TS 核心:model/spans,不依赖 DOM)
└── vite.config.ts (dev 代理到 FastAPI)
```

- **SSE 契约**:`GET /api/events` 单连接,事件类型 `job.progress` / `job.done` / `dashboard.changed` / `presence`。
- **进度契约**:子进程改为写 `progress.json`(或结构化日志行 `{phase, i, n}`),`jobs.py` 读文件推 SSE,废弃 stdout `[x/4]` 解析。

## 4. 分阶段路线图

每阶段独立完成判据 + 回滚点;旧界面在 `/`,新界面在 `/v2`,随时可切回。

### 阶段 0 — 工程卫生止血(不动架构)

1. mock 体系改为**动态 import**,仅 `?mock=1` 时加载;从 `mock_data.js` 剥离硬编码绝对路径(改用相对/占位路径)。
2. 移除生产环境的 `window.state` 句柄(或改为 `?debug=1` 才挂)。
3. `usability.css` 归并入 `style.css`,login 内嵌样式抽为 `login.css`。

**完成判据**:生产构建不再下载 mock 代码;仓库 grep 不到 `/media/wanji/` 路径;样式来源收敛为每页一个文件。
**回滚点**:单 commit 可 revert。

### 阶段 1 — 后端实时化(新前端的直接依赖)

1. 新增 `realtime.py` SSE 总线;jobs 进度、dashboard 变更、presence 三流合一。
2. `jobs.py` 进度改结构化契约(写 `progress.json`),解析逻辑替换 stdout 字符串匹配;旧轮询端点保留兼容。
3. 旧前端可切换到 SSE(可选,作为契约验证)。

**完成判据**:旧界面用 SSE 后进度/看板行为与轮询一致;`[x/4]` 解析代码删除;SSE 断线自动重连。
**回滚点**:轮询端点未删,前端切回即恢复原行为。

### 阶段 2 — 新前端骨架 + 首视图(文件树)

1. 初始化 `frontend/`:Vite + Vue 3 + TS + Pinia + Vue Router + Naive UI;dev proxy 到 FastAPI;产物挂载 `/v2`。
2. **像素风 token 化**:把 `style.css:4-36` 的 `:root` 变量抽成 design token,配置为 Naive UI 主题;像素字体随首屏加载。
3. 迁移**侧栏文件树**(状态徽标、像素进度条、过滤排序),作为首个验证视图。

**完成判据**:`/v2` 文件树功能与旧版逐项对齐(懒加载/勾选/徽标/过滤);主题 token 单一来源。
**回滚点**:`/` 旧界面未动。

### 阶段 3 — 数据看板迁移

1. 迁移 `dashboard.js`(精度卡 + GT/检出明细 + 行内审核),服务端分页/筛选 API 不变。
2. 用 Naive UI DataTable 替代手写表格;快照比对逻辑由响应式取代。

**完成判据**:看板交互对齐;`dashSnap`/`rowsSnap` 手写 diff 删除。
**回滚点**:路由级切回旧版。

### 阶段 4 — 分析详情页(视频预览 + 报告)

1. 迁移视频预览卡(Range 流播放 + 逐帧降级链)、markdown 报告卡、可拖拽分隔布局。
2. 证据编辑(canvas 多边形拖拽 + 乐观锁保存)抽为独立组件;顺手补键盘可操作性。

**完成判据**:预览/报告/证据编辑三卡功能对齐;`file_sig` 乐观锁行为不变(有冲突用例验证)。
**回滚点**:路由级切回旧版。

### 阶段 5 — SFT 编辑器(最难,最后迁)

1. 把 `sft_model.js`/`sft_spans.js` 的分词与 chips 逻辑抽成 **纯 TS 模块**(零 DOM 依赖),先补单元测试锁定现有行为。
2. Vue 壳组件负责 contenteditable 渲染与事件绑定;三层联动用 Pinia store 驱动。
3. 专家工作间泳道进度迁移,数据源切到 SSE。

**完成判据**:抽离的核心逻辑有单测覆盖;标注 v4.5 文档中的交互用例逐条人工验证通过。
**回滚点**:路由级切回旧版。

### 阶段 6 — 旧界面下线

1. 登录页迁移(像素风保留)。
2. 全量回归后删除 `static/legacy/`,`/v2` 提升为 `/`。
3. 可选:暗色主题(token 已就绪,成本仅为一套暗色变量)。

**完成判据**:E2E 冒烟(登录→加载工作区→推理→编辑→保存→看板)通过;仓库中无旧 JS 模块残留。

## 5. 风险与开放问题

| 风险 | 缓解 |
|------|------|
| contenteditable 在 Vue 中的光标/重渲染冲突 | 阶段 5 先做技术验证 spike(半天),失败则退回"保持原生组件、Vue 仅包壳挂载" |
| SSE 与 uvicorn worker 数的耦合(单进程内存总线不支持多 worker) | 本期固定单 worker(与串行队列一致);多 worker 是未来队列并发化的前置问题 |
| 逐视图迁移期间双栈并存,样式/约定漂移 | 每阶段完成判据含"旧视图冻结"条款;legacy 只修 P0 bug |
| `progress.json` 契约对子进程崩溃场景的语义 | 契约设计时定义 `failed` 终态与超时心跳,阶段 1 评审 |

**开放问题**(评审时需确认):
1. 阶段 2–6 是否由同一人连续执行,还是按阶段评审插入?
2. E2E 冒烟目前无自动化(`scripts/e2e_mock_test.py` 是 mock 级),阶段 6 的下线判据是否需要补 Playwright 级测试?
3. 像素字体子集(`font_subset_chars.txt`)在新构建链中的维护方式(构建时子集化 or 保持手工)?

## 6. 明确不做

- 任务队列并发化(多 worker 推理)
- i18n / 移动端专项 / 可访问性专项
- 后端 API 的 RESTful 重设计(现有端点全部保留兼容)

---

**阶段 6 已完成(2026-08-05)**:新前端(frontend/dist)提为 `/`,legacy
`traffic_analyzer/web/static/` 已删除;`/v2/*` 旧书签 301 重定向;`/login`
由 SPA LoginView 渲染;新增 Playwright 级真实后端冒烟
`scripts/e2e_v2_smoke.py`(mock 体系的 `e2e_mock_test.py` / `build_mock_data.py`
标注废弃存档)。
