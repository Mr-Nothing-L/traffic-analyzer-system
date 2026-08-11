# 二进制编码第 9 位语义:正常指示位

## 背景

二进制编码位序采用标注文档 v4.5 的 action 编号(1-11)。编号 9 对应 action 9 = "正常",不对应任何事件类别。

## 决策

目标语义:**位 9 = 正常指示位**。当视频无任何事件检出(正常)时,位 9 = 1,其余位 = 0,编码为 `{0_0_0_0_0_0_0_0_1_0_0}`。当有事件检出时,位 9 = 0,对应事件位置 1。

## 当前代码与目标的差异

`report_generator.py` 的 `to_binary_encoding()` 遍历 event_id 1..11,仅检查每个事件是否被检出。event_id 9 不在 `event_categories.yaml` 中定义,因此 `detected_map.get(9, False)` 恒为 `False`——位 9 **恒为 0**。当前代码的"正常"表示为全零编码 `{0_0_0_0_0_0_0_0_0_0_0}`。

这与目标语义不兼容:目标语义下正常 = 位 9 为 1,全零编码在目标语义中无定义。

## 迁移计划

1. 在 `to_binary_encoding()` 中增加逻辑:当无事件检出且 `rejected=False` 时,设位 9 = 1。
2. 预筛拒绝(`rejected=True`)时编码改为全 `_`(当前代码产出全零,需修正)。
3. 更新 `BinaryEncoding` 模型文档。
4. 校验下游消费方(报告渲染、SFT 标签、Web UI)是否依赖"全零 = 正常"的假设。
5. 更新 CLAUDE.md 中"编号 9 = 正常占位,恒为 0"的描述。

## 影响范围

- `traffic_analyzer/core/report_generator.py` — `to_binary_encoding()`
- `traffic_analyzer/orchestrator/reject_report_factory.py` — 预筛拒绝的编码
- `traffic_analyzer/models/report.py` — `BinaryEncoding` 模型注释
- `CLAUDE.md` — 事件编号表备注
- 下游:报告消费者、SFT 标签改写、批量评估脚本
