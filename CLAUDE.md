# 高速交通事件检测 Agent — 项目指南

## 项目定位

基于多模态大模型 (VLM) 的高速公路监控视频交通事件检测框架。

- **输入**:监控视频片段
- **输出**:11 位二进制编码 + Markdown 分析报告
- **设计核心**:事件定义 / Prompt 模板 / 裁决规则 全部通过 YAML 配置,新增事件无需改代码

---

## 标注权威基准 ★

**当前权威版本:`docs/交通事件数据标注说明文档_v4.5.md`** (2026-04-24 版)。任何事件定义、互斥规则、Prompt 设计的修改都应以 v4.5 为准。

`docs/v4.5_images/` 下 10 张官方示例图(action 01-11,缺 09=正常)。

### event_id 全局编号（= 标注文档 v4.5 的 action 编号）

event_id 全局采用 v4.5 的 action 编号，不再做 0-based 映射；action 9 = 正常指示位(ADR-0001:已分析且无事件时位 9=1)，不对应任何事件。

| event_id | 名称 | 检测模式 | 备注 |
|---|---|---|---|
| 1 | 违法停车 | expert_agent | 主车道,30s+ |
| 2 | 应急车道占用 | expert_agent | 含主路应急道 |
| 3 | 交通事故 | expert_agent | v4.5:机械故障≠事故 |
| 4 | 行人出现 | expert_agent | 含 reflective vest |
| 5 | 摩托车出现 | expert_agent | 摩托/自行车/三轮 |
| 6 | 严重拥堵 | expert_agent | 静止 30s+ |
| 7 | 道路施工 | expert_agent | v4.5:仅停车+捡垃圾不标 |
| 8 | 车辆逆行/倒车 | expert_agent | 像素位移估计 |
| 10 | 抛洒物 | expert_agent | v4.5:三角牌起提醒作用不标 |
| 11 | 实线变道 | expert_agent | 已于 2026-08 激活,is_active=true |
| 9 | normal | — | 正常指示位:无事件检出时为 1,有事件检出时为 0;预筛拒绝时编码全 _ |

### v4.5 vs v4.4 新增条款(尚未完全落入代码)

1. **action 3 (事故)**:机械故障(无碰撞)不算事故,需 visible 剐蹭 / 伤亡 / 财产损失
2. **action 7 (施工)**:施工车辆停车 + 人员仅捡垃圾等非施工操作 → 不标施工
3. **action 10 (抛洒物)**:三角牌起 *提醒* 作用 → 不标;无提醒 → 按抛洒物
4. **视频时长规则**(新增):
   - 视频 < 5s → 不标
   - 视频 > 15s → 切到 15s
   - 短事件可在头尾切,但要保留完整事件 + 整段 ≥5s

---

## 关键目录

```
.
├── docs/
│   ├── 交通事件数据标注说明文档_v4.5.md   ★ 权威标注文档(以此为准)
│   ├── CHANGELOG.md                      更新日志
│   ├── v4.5_images/                      10 张事件示例 png
│   └── web_ui_review_and_refactor_plan.md  web 重构诊断与路线图(v6 已执行完毕)
├── frontend/                            Web 前端(Vue 3 + TS + Naive UI,构建挂 /;含 /chat 统一对话视图)
├── agent/                               TS agent 运行时(src/kosong vendored LLM 抽象层、llm、
│                                        tools、permissions、sandbox、loop、server;npx vitest run)
├── traffic_analyzer/
│   ├── config/
│   │   ├── event_categories.yaml      事件定义 + 裁决规则
│   │   ├── event_options.yaml         SFT 结构化属性选项(封闭枚举)
│   │   └── prompt_templates.yaml      VLM Prompt 模板(多版本)
│   ├── core/
│   │   ├── pipeline_steps.py          ExpertAgentLayer / AdjudicationStep
│   │   ├── grounding_verification.py  裁决后原始帧锚定核验(GROUNDING_CHECK_ENABLE)
│   │   ├── vlm_engine.py              VLM 封装 + 缓存
│   │   ├── video_preprocessor.py      帧提取(coarse + precision)
│   │   ├── config_manager.py          配置加载/校验
│   │   └── report_generator.py
│   ├── orchestrator/analysis_orchestrator.py
│   ├── models/schemas.py              Pydantic 数据模型
│   ├── toolserver/                    Python 视频工具服务(127.0.0.1:8601,--workspace 必填)
│   └── web/                           FastAPI 层(jobs/evidence/workspace/dashboard 包 +
│                                      realtime.py SSE 总线;推理子进程 JSONL 进度契约;
│                                      agentproxy/ 反向代理 /api/agent/*(含 uploads 对话
│                                      文件上传),startup 拉起 toolserver+agent)
├── design.md                            前端设计系统(Hallmark 锁定,token 唯一源)
└── README.md                            系统全量说明
```

---

## 运行入口

```bash
# 配置校验(改 YAML 后必跑)
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config

# 分析视频(批量流水线)
python3 -m traffic_analyzer analyze \
  --video <path> --format markdown --output report.md
# 可选: --min-frames N (默认 10)

# Web 服务(批量推理 UI;startup 自动拉起 toolserver + TS agent 服务,
# AGENT_RUNTIME_ENABLE=0 关闭;顶部「Agent模式」进入 /chat 统一对话视图,
# 问答与正式检测同一入口,意图由模型判断)
python3 -m traffic_analyzer web

# TS agent 运行时单独运行(一般不需要,web 层会自动拉起)
cd agent && npm run serve        # npx tsx src/server/main.ts,127.0.0.1:8602
```

LLM 配置在 `traffic_analyzer/config/.env`(`LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` 等)。详细参数见 README §快速开始。

---

## 检测模式

| 模式 | 说明 |
|---|---|
| `expert_agent` | 每个事件类别由独立的事件专家(单次 VLM 调用)进行事实识别,`ThreadPoolExecutor` 并行执行。所有 10 个活跃事件均采用此模式。 |

---

## 当前状态

- 10 个事件类别全部激活,统一使用 `expert_agent` 模式
- 三层审查架构:事件专家 → 裁决 → 锚定核验(可选),详见 [ADR-0003](docs/adr/0003-three-layer-review-architecture.md)
- v4.5 标注文档新增条款部分已落入 definition,详见 `docs/交通事件数据标注说明文档_v4.5.md`
- 领域术语表见 [CONTEXT.md](CONTEXT.md),架构决策记录见 [docs/adr/](docs/adr/)

---

## 协作约定

- **改 YAML 配置时**:必须跑 `validate-config` 通过
- **新增事件**:用 `is_active: false` 关闭比注释更安全(保留二进制编码位)
- **Prompt 调整**:在 `prompt_templates.yaml` 加新 version,通过 `traffic_percentage` 或 `PROMPT_VERSION_*` env 切换
- **长任务**:批量 `analyze` 时遵循全局 `~/.claude/CLAUDE.md` 的 *静默心跳* 规则(nohup + watcher,只在完成/硬错/stall 推送)
- **路径敏感**:工作区已从 `11.64.37.48_ch1_20260401_142812/` 重命名为 `高速交通事件Agent/`,脚本里如还有旧路径需替换
- **目录隔离**:本仓库是 git repo,Claude 后台 session 写文件时会先用 worktree(`.claude/worktrees/<name>/`)隔离,改动以分支形式提交

---

## 跨会话同步说明

- `CLAUDE.md` (**本文件**) = **稳定指南 / 事实 / 约定** — 关键变更才更新,不是日志
- `docs/CHANGELOG.md` = **版本更新日志** — 阶段性变更在此记录
- `~/.claude/projects/.../memory/MEMORY.md` = 私有记忆(仅当前 Claude 用户可见,不跨用户)

> **本文件不会自动更新**;由 Claude 在阶段性里程碑或用户明确要求时维护。日常进展看 `docs/CHANGELOG.md` 与 git 历史,只有当稳定事实/约定变化时才修订本文。

---

*Last reviewed: 2026-08-25 — 统一对话(问答+检测双能力)落地后更新。*
