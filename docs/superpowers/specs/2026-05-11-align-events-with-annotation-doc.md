# 事件定义对齐标注文档 v4.4 修改方案

**日期:** 2026-05-11
**对标文档:** 交通事件数据标注说明文档_v4.4.md
**状态:** 待实施（用户选择性分阶段执行）

---

## 概述

本方案梳理了代码中 10 个事件类别（不含实线变道）与标注文档 v4.4 之间的不一致，按模块分组、按优先级排序。用户可选择性分阶段实施。

---

## 修改分组

### 组 A：跨事件互斥规则（最高优先级）

文档中定义了 6 条跨事件互斥/联合规则，当前代码只有 1 条（parking→emergency）。缺少这些规则会导致：
- 事故视频同时输出违停+行人+占用+抛洒物
- 施工视频同时输出逆行+行人+抛洒物+占用
- 拥堵视频同时输出违停

| # | 修改项 | 涉及事件 | 修改内容 | 影响文件 |
|---|-------|---------|---------|---------|
| A1 | 事故排除子事件 | 3→0,1,3,4,8,10 | 当 action 3（事故）detected=true 时，自动将 action 0/1/3/4/8/10 的 detected 设为 false | `event_categories.yaml` 新增互斥规则 + `PostProcessStep` |
| A2 | 拥堵排除违停 | 6→0 | 当 action 6（拥堵）detected=true 时，自动将 action 0（违停）detected 设为 false（仅当违停实例的时间区间在拥堵区间内） | `event_categories.yaml` |
| A3 | 施工区域互斥 | 7→3,7,8,1,10 | 当 action 7（施工）detected=true 时，自动将 action 3/7/8/1/10 设为 false（违停除外） | `event_categories.yaml` |
| A4 | 应急车道停车联合 | 0↔1 | 当 action 0（违停）在应急车道上时，同时触发 action 1（占用） | `event_categories.yaml` 扩展推断规则 |
| A5 | 应急车道逆行排除占用 | 8→1 | 当 action 8（逆行）在应急车道上时，将 action 1（占用）设为 false | `event_categories.yaml` |
| A6 | 施工区域行人排除 | 7→4 | 当 action 7（施工）detected=true 时，将 action 4（行人）设为 false（施工人员在施工区域内） | `event_categories.yaml` |

**推荐首批实施：A1 + A3（影响最大）**

---

### 组 B：事件定义/Prompt 修正（高优先级）

单个事件的定义或 prompt 与文档不符。

| # | 修改项 | 事件 | 当前问题 | 修改内容 | 影响文件 |
|---|-------|------|---------|---------|---------|
| B1 | 违停排除摩托车 | 0 | 文档说"只针对机动车，摩托车不算" | 在 `direct_event_detection` prompt 中增加"排除摩托车/非机动车"的明确指令 | `prompt_templates.yaml` |
| B2 | 违停区域扩展 | 0 | 文档区域包括应急车道/导流区/路肩，代码只说 main travel lanes | 更新 event definition 和 prompt | `event_categories.yaml`, `prompt_templates.yaml` |
| B3 | 违停持续时间 | 0 | 文档要求静止≥10s，代码无阈值 | 在 prompt 中增加"静止持续时间≥10s"的要求 | `prompt_templates.yaml` |
| B4 | 应急车道阈值 | 1 | 文档要求停留≥10s 或行进≥5m | 在 prompt 中增加阈值要求 | `prompt_templates.yaml` |
| B5 | 逆行距离阈值 | 8 | 文档要求行驶距离≥5m | 在 `direct_reversing_detection` prompt 中增加"行驶距离≥5m"的要求 | `prompt_templates.yaml` |
| B6 | 逆行施工排除 | 8 | prompt 说"工程车倒车必须报告"，与文档"施工区域内不算逆行"直接矛盾 | 修改 prompt：增加"施工区域内车辆往返不算逆行"的排除规则 | `prompt_templates.yaml` |
| B7 | 行人包含动物 | 4 | 文档包含牛/羊/野猪等，代码只说 Pedestrians | 更新 event definition 和 prompt | `event_categories.yaml`, `prompt_templates.yaml` |
| B8 | 抛洒物排除项 | 10 | 文档要求排除锥桶/施工牌/事故碎片/施工区域杂物 | 在 `direct_event_detection` prompt 中增加排除列表 | `prompt_templates.yaml` |
| B9 | 施工判定条件 | 7 | 文档要求三要素可配置（满足其一/其二），prompt 规则固定 | 将判定条件改为可配置（通过 YAML 参数），默认与文档一致 | `prompt_templates.yaml` + `event_categories.yaml` |
| B10 | 拥堵门槛 | 6 | 文档说"单条车道通行能力下降也标"，代码强调"Severe" | 降低 prompt 中的门槛描述 | `prompt_templates.yaml` |
| B11 | 事故判定细化 | 3 | 文档有具体判定条件（停车数量+行人+倾覆），代码泛泛而谈 | 在 prompt 中增加具体判定条件 | `prompt_templates.yaml`（当前是 scene_tag，可能需要改为 direct_vlm 或增强逻辑链） |
| B12 | 非机动车改名 | 5 | 代码叫"摩托车出现"，文档是"非机动车"（含电动自行车等） | 更新 name/name_zh/definition | `event_categories.yaml` |

**推荐首批实施：B1 + B2 + B6（最严重的不符）**

---

### 组 C：Scene Understanding 标签增强（中优先级）

| # | 修改项 | 当前问题 | 修改内容 | 影响文件 |
|---|-------|---------|---------|---------|
| C1 | 缺少抛洒物标签 | scene_description 没有 `{抛洒物：...}` 标签 | 在 `scene_understanding` prompt 的必需标签列表中增加第 10 项 `{抛洒物：无/有}` | `prompt_templates.yaml` |
| C2 | 交通事故标签过简 | `{交通事故：无/有}` 无法反映复杂判定条件 | 增强标签格式为 `{交通事故：无}` 或 `{交通事故：有，N辆车停止，M个行人，是否倾覆}` | `prompt_templates.yaml` |
| C3 | 工程车≠施工 | `{工程车：...}` 标签被误用为施工判定依据 | 在 prompt 中增加说明："工程车存在≠施工事件，施工需同时满足多要素" | `prompt_templates.yaml` |

---

### 组 D：检测模式调整（中优先级）

| # | 修改项 | 事件 | 当前模式 | 建议模式 | 理由 |
|---|-------|------|---------|---------|------|
| D1 | 交通事故 | 3 | scene_tag | logic_chain 或 direct_vlm | 文档判定条件复杂，仅靠场景标签无法准确判断 |
| D2 | 行人/施工人员 | 4 | scene_tag | logic_chain | 需要判断是否在施工区域内、是否事故涉及 |
| D3 | 抛洒物 | 10 | direct_vlm | direct_vlm（需增强 prompt） | 当前通用模板无法处理排除项 |

---

## 实施建议顺序

### 第一批（核心互斥 + 最严重定义错误）
1. **A1** — 事故排除子事件（解决事故视频输出冗余）
2. **A3** — 施工区域互斥（解决施工视频大量误报）
3. **B6** — 逆行施工排除（prompt 与文档直接矛盾）
4. **B1** — 违停排除摩托车
5. **B2** — 违停区域扩展

### 第二批（事件定义补全）
6. **A2** — 拥堵排除违停
7. **A4/A5** — 应急车道联合/排除
8. **B3-B5** — 持续时间/距离阈值
9. **B7-B10** — 其他定义修正

### 第三批（增强与优化）
10. **C1-C3** — Scene Understanding 标签增强
11. **D1-D3** — 检测模式调整
12. **B11-B12** — 事故判定细化 + 非机动车改名

---

## 技术实现要点

### 跨事件互斥规则的实现方式

在 `event_categories.yaml` 的 `cross_event_inference_rules` 区域新增互斥规则类型：

```yaml
# 当前只有推断规则（正向）
cross_event_inference_rules:
  - rule_id: "parking_to_emergency"
    ...

# 建议新增互斥规则（负向）
cross_event_mutual_exclusion_rules:
  - rule_id: "accident_excludes_parking"
    when_event_id: 3        # 当事故检测到
    exclude_event_ids: [0, 1, 4, 8, 10]  # 排除这些事件
    reasoning: "交通事故涉及的其他子事件无需单独标注"

  - rule_id: "construction_excludes_others"
    when_event_id: 7
    exclude_event_ids: [1, 3, 7, 8, 10]
    except: [0]              # 违停除外
    reasoning: "施工区域内除违停外其他事件不标"
```

对应修改 `PostProcessStep._execute()` 以处理互斥规则。

### Prompt 修改原则

所有修改遵循"最小改动"原则：
- 优先修改 `user_prompt` 中的 instructions 部分
- 不修改 JSON schema 输出格式（保持下游兼容）
- 新增要求用 `CRITICAL` 或 `IMPORTANT` 前缀强调

---

## 验证方案

每批次修改完成后，应验证：
1. `python3 -m traffic_analyzer validate-config` 通过
2. 用已知标注的样本视频跑分析，检查二进制编码与标注是否一致
3. 特别关注之前发现不一致的场景：
   - 事故视频（应只输出事故）
   - 施工视频（应只输出施工+可能的违停）
   - 应急车道停车视频（应同时输出违停+占用）
