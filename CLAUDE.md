# 高速交通事件检测 Agent — 项目指南

## 项目定位

基于多模态大模型 (VLM) 的高速公路监控视频交通事件检测框架。

- **输入**:监控视频片段
- **输出**:10 位二进制编码 + Markdown 分析报告
- **设计核心**:事件定义 / 检测模式 / Prompt 模板 / 逻辑链 / 推断规则 全部通过 YAML 配置,新增事件无需改代码

---

## 标注权威基准 ★

**当前权威版本:`docs/交通事件数据标注说明文档_v4.5.md`** (2026-04-24 版)。任何事件定义、互斥规则、Prompt 设计的修改都应以 v4.5 为准。

`docs/v4.5_images/` 下 10 张官方示例图(action 01-11,缺 09=正常)。

### event_id 全局编号（= 标注文档 v4.5 的 action 编号）

event_id 全局采用 v4.5 的 action 编号，不再做 0-based 映射；action 9 = 正常占位，不对应任何事件。

| event_id | 名称 | 检测模式 | 备注 |
|---|---|---|---|
| 1 | 违法停车 | direct_vlm | 主车道,30s+ |
| 2 | 应急车道占用 | scene_tag | 含主路应急道 |
| 3 | 交通事故 | logic_chain | v4.5:机械故障≠事故 |
| 4 | 行人出现 | scene_tag | 含 reflective vest |
| 5 | 摩托车出现 | scene_tag | 摩托/自行车/三轮 |
| 6 | 严重拥堵 | direct_vlm | 静止 30s+ |
| 7 | 道路施工 | direct_vlm | v4.5:仅停车+捡垃圾不标 |
| 8 | 车辆逆行/倒车 | logic_chain | 多步链,像素位移估计 |
| 10 | 抛洒物 | direct_vlm | v4.5:三角牌起提醒作用不标 |
| 11 | 实线变道 | logic_chain | 已于 2026-08 激活(is_active=true),options 见 event_options.yaml |
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
├── frontend/                            Web 前端(Vue 3 + TS + Naive UI,构建挂 /)
├── traffic_analyzer/
│   ├── config/
│   │   ├── event_categories.yaml      事件定义 + cross_event_inference_rules
│   │   ├── event_options.yaml         SFT 结构化属性选项(封闭枚举)
│   │   ├── logic_chains.yaml          多步逻辑链
│   │   └── prompt_templates.yaml      VLM Prompt 模板(多版本)
│   ├── core/
│   │   ├── pipeline_steps.py          SceneUnderstanding / EventDetection / PostProcess
│   │   ├── grounding_verification.py  裁决后原始帧锚定核验(GROUNDING_CHECK_ENABLE)
│   │   ├── logic_engine.py            逻辑链执行
│   │   ├── vlm_engine.py              VLM 封装 + 缓存
│   │   ├── video_preprocessor.py      帧提取(coarse + precision)
│   │   ├── config_manager.py          配置加载/校验
│   │   └── report_generator.py
│   ├── orchestrator/analysis_orchestrator.py
│   ├── models/schemas.py              Pydantic 数据模型
│   └── web/                           FastAPI 层(jobs/evidence/workspace/dashboard 包 +
│                                      realtime.py SSE 总线;推理子进程 JSONL 进度契约)
├── design.md                            前端设计系统(Hallmark 锁定,token 唯一源)
└── README.md                            系统全量说明
```

---

## 运行入口

```bash
# 配置校验(改 YAML 后必跑)
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config

# 分析视频
python3 -m traffic_analyzer analyze \
  --video <path> --format markdown --output report.md
# 可选: --min-frames N (默认 30)、--cv-tracks tracks.json
```

LLM 配置在 `traffic_analyzer/config/.env`(`LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` 等)。详细参数见 README §快速开始。

---

## 三种检测模式简表

| 模式 | 数量 | 说明 |
|---|---|---|
| `direct_vlm` | 4 个事件 | 专用 Prompt + 单次 VLM;`ThreadPoolExecutor` 并行 |
| `logic_chain` | 3 个事件 | YAML 多步链(vlm_call→compute→condition→aggregate) |
| `scene_tag` | 3 个事件 | 零 VLM,从 scene_understanding 推断 |

> **现状**:目前仅 `expert_agent` 模式已落地(`core/pipeline_steps.py` 只执行该模式的类别);上表三种模式暂无执行路径。活跃类别若配置为这三种模式,`validate-config` 会报错拒绝。

---

## 对齐进度(对照 24 项 spec)

- ✅ **已完成 5**:event_categories.yaml 重构、scene_understanding 增字段、3 个 scene_tag 事件落地、cross_event_inference 框架、并行 direct_vlm
- 🚧 **部分完成 5**:违停/拥堵/施工 Prompt 精细化、跨事件 *互斥* 规则(目前只有 inference 没有 exclusion)、报告 markdown 格式、像素位移估计、is_active 关闭实线变道
- ⏳ **待办 14**:见 `docs/superpowers/specs/2026-05-11-align-events-with-annotation-doc.md`

**注意**:该 spec 基于 v4.4,v4.5 新增 4 项条款尚未纳入。下一次扩展应同时更新 spec。

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

*Last reviewed: 2026-08-05 — web 前端 v6 重构(Vue 3 SPA)后更新。*
