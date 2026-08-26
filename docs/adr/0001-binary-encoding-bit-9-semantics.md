# 二进制编码第 9 位语义:正常指示位

## 背景

二进制编码位序采用标注文档 v4.5 的 action 编号(1-11)。编号 9 对应 action 9 = "正常",不对应任何事件类别。

## 决策

目标语义:**位 9 = 正常指示位**。当视频无任何事件检出(正常)时,位 9 = 1,其余位 = 0,编码为 `{0_0_0_0_0_0_0_0_1_0_0}`。当有事件检出时,位 9 = 0,对应事件位置 1。

## 迁移状态:已完成

位 9 = 正常指示位的目标语义已在两侧实现,全零编码不再是合法的正常编码:

- **批量侧**:`traffic_analyzer/core/report_generator.py` 的 `to_binary_encoding()` 在无事件检出时置位 9=1;预筛拒绝产出全 `_` 编码(`reject_report_factory.py`)。
- **Agent 侧**:`agent/src/tools/builtin/submitDetection.ts` 的编码正则放开位 9 为 0/1,运行时校验要求:位 i(i∈1-8、10、11)与 `events.detected` 一致;位 9=1 当且仅当无任何事件检出;`normal` 与位 9 一致。模型侧契约(`agent/config/submit_detection.schema.json`、`agent/config/toolset.json`、`agent/prompts/chat_system.md`、`agent/prompts/detect_system.md`)同步更新。

## 影响范围

- `traffic_analyzer/core/report_generator.py` — `to_binary_encoding()`(已完成)
- `traffic_analyzer/orchestrator/reject_report_factory.py` — 预筛拒绝的编码(已完成)
- `traffic_analyzer/models/report.py` — `BinaryEncoding` 模型注释(已完成)
- `CLAUDE.md` — 事件编号表备注(已完成)
- `agent/src/tools/builtin/submit_detection.ts` + `agent/config/*` + `agent/prompts/*` — agent 侧提交契约(已完成)
- 下游:报告消费者、SFT 标签改写、批量评估脚本(已随迁移校验)
