# 检测模式枚举:仅 expert_agent 有执行路径

## 背景

`DetectionMode` 枚举定义了四种值:`direct_vlm`、`logic_chain`、`scene_tag`、`expert_agent`。CLAUDE.md 的"三种检测模式简表"仍在描述前三种。

## 决策

`expert_agent` 是当前唯一启用的检测模式。每个事件类别由独立的事件专家(单次 VLM 调用)进行事实识别,多位专家通过 `ThreadPoolExecutor` 并行执行。其余三种模式(`direct_vlm`/`logic_chain`/`scene_tag`)为遗留概念,`pipeline_steps.py` 中无执行路径,`validate-config` 会拒绝配置为这三种模式的活跃类别。

## 原因

`expert_agent` 模式统一了所有事件类别的检测路径——每位专家只做事实识别,排除/冲突判断统一交给裁决层。这取代了此前按 `direct_vlm`/`logic_chain`/`scene_tag` 分别处理的异构架构。

## 后续

三种遗留模式的枚举值、文档描述、配置校验逻辑应清理:
- `CLAUDE.md`"三种检测模式简表"章节需更新或移除。
- `DetectionMode` 枚举中 `direct_vlm`/`logic_chain`/`scene_tag` 可移除或标注 deprecated。
- `core/config_manager.py` 中针对这三种模式的校验分支可简化。
- `logic_chains.yaml` 若仅服务遗留模式,可考虑移除。
