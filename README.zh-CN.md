[English](README.md) | [简体中文](README.zh-CN.md)

# 交通事件分析系统

基于多模态视觉语言模型（VLM）的高速公路监控视频事件检测：输入一段视频，输出一个
**11 位二进制事件编码**和一份结构化分析报告（Markdown / JSON）；开启 SFT label 模式时
每个视频再产出一条 **SFT 训练样本**，可在自带的 Web 界面中编辑。

> 当前版本：v6.0.0。

## 架构

三层结构，靠明确契约（REST schema、JSONL 进度文件、工作区目录约定）解耦：

```
┌──────────────────────────────────────────────┐
│ Web 界面 — Vue 3 + TS + Naive UI 单页应用     │  frontend/
│ 文件树 · 分析详情 · SFT 标注编辑器 · 数据看板  │
└───────────────────┬──────────────────────────┘
                    │ REST /api/* · SSE /api/events · Range 视频流
┌───────────────────▼──────────────────────────┐
│ FastAPI web 层                               │  traffic_analyzer/web/
│ 认证 · 串行任务队列 · 数据看板 · SSE 推送      │
└───────────────────┬──────────────────────────┘
                    │ 每个分析任务一个子进程
┌───────────────────▼──────────────────────────┐
│ 分析管线(YAML 配置驱动)                       │  orchestrator + core/
│ 预处理 → 专家并行检测 → 裁决 → 锚定核验        │
│   → SFT 标签改写 → 生成报告                   │
└───────────────────┬──────────────────────────┘
                    │ 写出结构化进度事件(JSONL),由 web 层尾随
                    ▼
              工作区目录(视频 + 分析结果)
```

- **前端**只经 REST 和一条 SSE 通道（任务进度、看板变更、在线名册）与后端交互，无轮询。
- **web 层**以串行子进程跑推理，尾随任务的结构化进度文件驱动界面实时更新。
- **分析核心**全部由 YAML 配置（`traffic_analyzer/config/`）：事件定义、Prompt 模板、
  逻辑链——新增事件无需改代码。

## 快速开始

### 环境要求

- Python 3.10+（Docker 镜像用 3.11）
- `ffmpeg`（视频解码；用系统包管理器安装）
- `pip install -r requirements.txt`（跑测试再加 `pip install -r requirements-dev.txt`）

### 1. 配置 LLM 提供者（.env）

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# 编辑 .env，填入 API Key 与模型
```

LLM 配置**只从 `.env` 文件读取**（`traffic_analyzer/config/.env`，兼容回退到仓库根目录
`.env`），**不读取 shell 环境变量**。最小单提供者配置：

```ini
VLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_MODEL=claude-sonnet-4-6
```

可选：先校验配置再运行：

```bash
python3 -m traffic_analyzer validate-config --config-dir ./traffic_analyzer/config
```

### 2. 分析一个视频

```bash
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./output/report.md
```

省略 `--output` 时报告打印到 stdout（默认 JSON，`--format markdown` 输出 Markdown）。
常用参数：`--min-frames N`（每次 VLM 调用最大帧数，默认 10）、`--sft-label`（每视频额外
导出一条 SFT 训练样本到 `--sft-output-dir`，默认 `output/sft_labels`）、`--config-dir`、
`--log-level`。完整列表见 `python3 -m traffic_analyzer analyze --help`。

在终端中运行时显示 **rich 实时进度面板**：每个专家一条泳道（8 个事件专家 + 裁决 + SFT
标注 + 报告）；非 TTY 输出（Web 子进程、管道）自动退化为 `EXPERT_PROGRESS` 标记行，供
Web 前端解析。退出码：`0` 成功，`1` 错误，`2` 视频被预过滤器筛除（不写报告文件）。

## Web 界面

Web 界面（FastAPI 后端 + SPA 前端）是推理、SFT 标注编辑与数据集审核的主入口
（前端为 Vue 3 单页应用，源码在 `frontend/`，`npm run build` 产出 `frontend/dist`，
由后端挂载在 `/`）：

```bash
python3 -m traffic_analyzer web            # 默认 http://127.0.0.1:8600
python3 -m traffic_analyzer web --host 0.0.0.0 --port 9000 --workspace ./workspace
```

![数据看板](docs/images/ui_dashboard.png)

- **工作区** — 视频与分析结果统一放在一个工作目录下，可在工具栏切换（或用 `--workspace`
  预选）。
- **推理** — 勾选单个或多个视频开始推理，任务在后台队列执行。**专家工作间**面板以
  像素风格泳道动画实时展示进度（8 个类别专家 + 裁决 + SFT 标注 + 报告，共 11 条泳道）。
  运行中的任务可随时点「停止」（先 SIGTERM 后 SIGKILL），已停止/失败的视频可点 ↻ 重试。
  任务跑的是同一条 `analyze` 流水线并开启 `--sft-label`，因此每个任务同时产出 SFT 样本
  与证据文件。
- **SFT 标注编辑** — 逐视频结果卡片展示 SFT 样本；**结构化选项 chips**（封闭枚举，来自
  `event_options.yaml`）与描述文本双向联动，首次人工修改前把推理原始输出冻结为
  `<stem>_raw.json`，人工改动单独统计。
- **证据编辑** — 可视化证据画布编辑器：拖拽多边形/矩形的顶点与边，保存回
  `<stem>_evidence.json`。
- **数据看板** — 整页 GT vs 模型检出视图：逐视频一致性（一致/分歧/无 GT/未推理）、
  审核三态（未确认/已确认/需复核，持久化到 `analysis/review_states.json`）、实时聚合的
  逐事件 precision/recall/F1（含宏/微平均）、「人工已改」徽章与过滤（对照 `_raw.json`
  快照计算）、名称搜索。

![专家工作间](docs/images/ui_expert_panel.png)

## 多人协同部署

部署到共享服务器时，绑定全部网卡并给每人一个账号：

```bash
python3 -m traffic_analyzer web --host 0.0.0.0 --port 8600
```

- **登录与 30 天免登** — 只要存在任一账号，认证自动开启。用户在 `/login` 登录后会话
  Cookie 30 天有效（期间免登），并绑定登录 IP。没有任何账号时认证完全关闭，行为与
  单用户本地使用一致。
- **按人账号** — 每个登录是独立用户；编辑会记录 `last_edited_by`，看板与在线状态都能
  看到是谁改的。
- **presence（谁正在编辑）** — 界面显示谁正在查看/编辑哪个视频（30 秒心跳名册）。
- **409 冲突保护** — 保存 SFT 样本或证据文件时，若文件在你加载后已被他人改动（乐观
  指纹校验），或该视频正有推理任务排队/运行，保存会被 409 拒绝——不会静默覆盖。

### 用户管理 CLI

账号存放在 `traffic_analyzer/config/users.db`，用脚本管理（省略 `--password` 时交互式
输入密码）：

```bash
python3 scripts/manage_users.py add zhangsan        # 新建账号（交互输入密码）
python3 scripts/manage_users.py list                # 列出全部账号
python3 scripts/manage_users.py passwd zhangsan     # 修改密码
python3 scripts/manage_users.py remove zhangsan     # 删除账号
```

也可以用引导配置：在 `traffic_analyzer/config/.env` 写
`TRAFFIC_ANALYZER_USERS=zhangsan:pass1,lisi:pass2`，首次启动时账号自动导入 `users.db`
并注释掉该行。会话签名密钥 `TRAFFIC_ANALYZER_SECRET` 首次使用时自动生成并追加到 `.env`。

### 工作区白名单

限制用户可选的工作区目录：

```ini
# traffic_analyzer/config/.env —— 逗号分隔，支持 ~ 与相对路径
TRAFFIC_ANALYZER_WORKSPACE_DIRS=/data/videos,/srv/datasets
```

名单非空时，工作区选择与目录浏览只允许名单内目录及其子路径（越界返回 403）。**删掉该行
（或留空）即不限制。**

## mock 演示模式（已删除）

> **已废弃：** 旧的 `?mock=1` 演示模式及其配套脚本 `scripts/build_mock_data.py` /
> `scripts/e2e_mock_test.py` 已随 legacy 界面一并删除，旧命令不再可用，此处仅作历史说明。

端到端界面自测改用 `scripts/e2e_v2_smoke.py`（Playwright 驱动真实后端：登录 →
加载工作区 → 侧栏树 → 视频详情 → SFT 编辑器 → 数据看板 → 登出，截图存
`output/e2e_screenshots/v2_smoke_*.png`）：

```bash
python3 scripts/e2e_v2_smoke.py                # 无头 Chrome，默认端口 8608
python3 scripts/e2e_v2_smoke.py --headed       # 有头浏览器
python3 scripts/e2e_v2_smoke.py --port 8609 --video-fragment 01-02_Event_129
```

脚本会自建临时账号/临时工作区，并在 127.0.0.1:<port> 自行启动真实后端；不跑真实
推理（需 VLM/GPU）。

## 配置项速查

### `traffic_analyzer/config/.env`

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VLM_PROVIDER` / `LLM_PROVIDER` | `anthropic` | 提供者：`anthropic` / `google` / `aliyun` |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | — / `claude-sonnet-4-6` / — | 通用 Key / 模型 / 自定义端点 |
| `ANTHROPIC_*` / `GOOGLE_*` / `ALIYUN_*` | — | 提供者级 `_API_KEY` / `_MODEL` / `_BASE_URL` 覆盖 |
| `LLM_PROVIDER_<i>_PROVIDER` / `_API_KEY` / `_MODEL` / `_BASE_URL` | — | 多提供者故障转移列表（0 为主；存在时忽略单提供者变量） |
| `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` / `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | `4096` / `0.2` / `300` / `3` | 推理参数 |
| `LLM_ENABLE_CACHE` / `LLM_CACHE_MAX_SIZE` | `true` / `128` | 内存响应缓存 |
| `TRAFFIC_ANALYZER_DISK_CACHE` / `_MAX_ENTRIES` | — / `2000` | SQLite 磁盘缓存路径 / 容量（可选） |
| `VLM_MAX_FRAMES` | `10` | 每次 VLM 调用最大输入帧数 |
| `EXPERT_ENABLE_REFLECTION` | `true` | 专家候选反思一致性检查 |
| `GROUNDING_CHECK_ENABLE` | `true` | 裁决后锚定核验（推翻无法锚定的阳性幻觉） |
| `SFT_LABEL_ENABLE` / `SFT_LABEL_OUTPUT_DIR` | `false` / `output/sft_labels` | SFT 样本导出（等价 CLI `--sft-label` / `--sft-output-dir`） |
| `SAMPLING_FPS` | `1.0` | 抽帧帧率 |
| `PREFILTER_ENABLE` 及 `PREFILTER_*` | `false` | 质量预过滤器（自带 `.env.example` 默认开启） |
| `PROMPT_VERSION_<TEMPLATE_ID>` | — | 固定某 Prompt 模板的版本 |
| `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL` | `mid` | Tool-Call 风格日志粒度：`off` / `macro` / `mid` / `fine` |
| `TRAFFIC_ANALYZER_USERS` | — | 引导式 Web 账号，`zhangsan:pass1,lisi:pass2`（首次启动迁移到 `users.db`） |
| `TRAFFIC_ANALYZER_SECRET` | 自动生成 | 会话 Cookie 签名密钥 |
| `TRAFFIC_ANALYZER_WORKSPACE_DIRS` | —（不限制） | 工作区白名单（见「多人协同部署」） |

### 事件开关 — `config/event_categories.yaml`

每个事件含 `event_id`、`name`/`name_zh`、`prompt_template_id`、`confidence_threshold`、
`is_active` 和注入专家 Prompt 的 `definition`。**关闭事件用 `is_active: false`**（保留其
编码位、恒为 0），不要整段注释。同文件的 `adjudication_rules` 指导最终跨事件裁决；新增
或调整事件无需改代码，改完跑一次 `validate-config` 校验。

### 专家阶段文案 — `web/expert_phases.json`

专家工作间与 CLI 进度面板中各泳道的阶段文案（如「巡检应急车道」「交叉裁决」），按事件
和裁决泳道分别定义。纯展示层，可随意修改。

## 输出物说明

CLI 分析把报告写到 `--output`（或 stdout）；`--sft-label` 时另写
`<sft-output-dir>/<视频名>.json`。Web 推理任务把每个视频的全部结果放在
`<workspace>/analysis/<video_stem>/` 下：

- `<video_stem>.json` — SFT 训练样本（`action` / `description` 等），界面中可编辑
- `<video_stem>_raw.json` — 首次人工编辑前冻结的推理原始输出；看板的「人工已改」据此计算
- `report.md` — Markdown 分析报告（关键结论前置，细节在附录）
- `<video_stem>_evidence.json` — 可编辑的可视化证据（标定多边形、证据区域、画廊图像，坐标为归一化 [0,1]）
- `images/` — 证据 JSON 引用的图像

数据看板的审核三态持久化到 `<workspace>/analysis/review_states.json`。

## 测试

```bash
python3 -m pytest traffic_analyzer/tests -q
```

测试套件 mock 全部 VLM 调用，覆盖配置校验、CLI、分析流水线与 Web API。端到端界面检查
用上面的冒烟脚本（`scripts/e2e_v2_smoke.py`）。

## 事件类别

二进制编码格式 `{bit_1_..._bit_11}`，**bit i ↔ event_id i**（event_id 即标注文档 v4.5
的 action 编号）。bit 9 为保留的"正常"占位，恒为 0；未激活事件保留其位、恒报 0。
示例：`1_0_1_0_0_0_0_0_0_0_0` 表示检出事件 1 与 3。

| bit | 编码 | 事件 | 激活 |
|---|---|---|---|
| 1 | A | 违法停车 | ✓ |
| 2 | B | 应急车道占用 | ✓ |
| 3 | C | 交通事故 | ✓ |
| 4 | D | 高速公路行人出现 | ✓ |
| 5 | E | 摩托车出现 | ✓ |
| 6 | F | 拥堵 | ✓ |
| 7 | G | 道路施工 | ✓ |
| 8 | H | 车辆逆行/倒车 | ✓ |
| 9 | — | —（保留"正常"占位，恒 0） | — |
| 10 | J | 抛洒物 | ✗ |
| 11 | K | 实线变道 | ✗ |

---

[English](README.md) | [简体中文](README.zh-CN.md)
