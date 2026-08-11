# 标注文档为权威规范,YAML 为运行时实现

## 背景

系统的事件定义有两个来源:`docs/交通事件数据标注说明文档_v4.5.md`(人工标注规范,外部权威)和 `traffic_analyzer/config/event_categories.yaml`(运行时配置,VLM prompt 的实际数据源)。两者之间是人工同步,可能产生偏差。

## 决策

标注文档为事件定义的**权威规范**——事件编号、定义边界、排除条款以文档为准。`event_categories.yaml` 是文档定义在运行时的**实现映射**,其 `definition` 字段直接注入 VLM prompt,决定实际检测行为。

两者偏差由对齐 spec(`docs/superpowers/specs/`)跟踪。CLAUDE.md 的"v4.5 新增条款尚未完全落入代码"即为当前已知偏差清单。

## 原因

标注文档面向标注人员,需要精确的视觉边界描述和排除规则;YAML 面向 VLM,需要 prompt 友好的自然语言定义。两者的受众和粒度不同,无法合二为一。但变更必须从文档流向 YAML,不能反向——直接改 YAML 的 definition 绕过了标注规范,可能导致 VLM 行为与标注数据集不一致。

## 影响

- 新增/修改事件定义时,先更新标注文档,再将变更同步到 `event_categories.yaml`。
- YAML 的 `definition` 与文档不一致时,以文档为准。
- 版本升级(v4.5→v4.6)时,须同时产出偏差清单并逐项对齐。
