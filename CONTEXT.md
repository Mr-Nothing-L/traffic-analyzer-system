# 高速交通事件检测 (Traffic Event Detection)

基于多模态大模型 (VLM) 的高速公路监控视频交通事件检测框架。输入一段监控视频,输出 11 位二进制事件编码 + Markdown 分析报告 + SFT 训练样本。

## 语言

### 事件定义

**事件类别 (Event Category)**:
一种可检测的交通事件类型,全局唯一编号。当前共 10 个活跃类别(编号 1-8, 10-11),覆盖违法停车、应急车道占用、交通事故、行人出现、摩托车出现、严重拥堵、道路施工、车辆逆行/倒车、抛洒物、实线变道。新增类别只需追加 YAML 配置,无需改代码。
_Avoid_: action, event type

**事件编号 (Event ID)**:
事件类别的全局标识,采用标注文档 v4.5 的 action 编号(1-8, 10-11)。编号同时定义二进制编码的位序。编号 9 = 正常占位(见「二进制编码」),不对应任何事件类别。
_Avoid_: action number, bit position, index

### 检测流水线

**关键帧序列 (Keyframe Sequence)**:
从视频中提取的代表帧集合,分两层采样:粗采样(coarse)覆盖全片用于全局理解,精细采样(precision)聚焦运动区域用于细节识别。
_Avoid_: frames, screenshots

**事件专家 (Event Expert)**:
负责单一事件类别事实识别的角色。只报告"看到/没看到",不做排除或冲突判断。每个事件专家做一次 VLM 调用,产出事件候选。
_Avoid_: detector, classifier, analyzer

**事件专家模式 (Expert Agent Mode)**:
当前唯一启用的检测模式。每个事件类别由一个独立的事件专家(单次 VLM 调用)进行事实识别,多位专家通过线程池并行执行。
_Avoid_: direct_vlm, logic_chain, scene_tag(均为遗留概念,无执行路径)

**远距离增强 (Far-Object Enhancement)**:
事件专家内部的可选子步骤。对配置了 `far_object_enhancement` 的事件模板,专家在首次 VLM 调用后对感兴趣区域(ROI)做逐帧放大、运动对比和候选打分,再发起一次最终分类器 VLM 调用。对行人(4)和摩托车(5)事件,最终分类器附带汽车语义否决(car veto)——若 VLM 判定 ROI 内目标实为四轮汽车,则否决该检出以修正远距离误判。远距离增强不改变事件专家"只做事实识别"的职责边界。
_Avoid_: zoom, upscaling, super-resolution

**事件候选 (Event Candidate)**:
事件专家的原始输出,记录该专家是否检测到目标事件及其细节(实例、置信度、VLM 原始回复)。候选可能被后续的裁决或锚定核验推翻。
_Avoid_: detection result, raw output

**裁决 (Adjudication)**:
流水线的第二层审查。接收全部事件专家的候选,通过单次 VLM 调用进行跨事件冲突裁决——哪些候选应保留、哪些应排除,依据业务规则(如"应急车道静止→双事件")。裁决产出事件结果。
_Avoid_: post-processing, filtering

**事件结果 (Event Result)**:
裁决后的单事件判定,包含是否检出、实例详情、裁决推理过程。仍可能被锚定核验推翻(`grounding_overturned`)。
_Avoid_: final result(锚定核验后才是最终判定)

**锚定核验 (Grounding Verification)**:
流水线的第三层审查(可选)。对裁决判定为阳性的事件结果,再用原始粗关键帧做一次 VLM 调用,验证其关键视觉元素能否在画面中锚定。无法锚定的阳性判定被视为幻觉并推翻(`detected` 置 false,`grounding_overturned` 置 true)。
_Avoid_: validation, double-check

### 系统输出

**预筛拒绝 (Prefilter Reject)**:
视频不符合分析条件(时长 < 5s、解码失败、无可用帧)时,流水线在预处理阶段直接退出,产出拒绝报告。预筛拒绝不是"检测到正常"——后者是分析完成后的结论,前者是分析未执行。拒绝报告标记 `rejected=True`,二进制编码为全 `_`(不可用)。
_Avoid_: skip, early-return, error

**二进制编码 (Binary Encoding)**:
系统对一段视频的最终判定,用固定 11 位二进制串表示,格式 `{bit_1_bit_2_..._bit_11}`。位序 = 事件编号(1-11)。三种编码语义:
- `{_ _ _ _ _ _ _ _ _ _ _}`(全 `_`)= 预筛拒绝(不可分析)
- `{0_0_0_0_0_0_0_0_1_0_0}`(位 9=1)= 正常(已分析,无事件)
- 其余 = 有事件检出(位 9=0,对应事件位=1)

_Avoid_: classification code, event flags

> **⚠️ 代码与目标语义差异**:当前代码中位 9 恒为 0(不作为正常指示),全零编码 `{0_0_0_0_0_0_0_0_0_0_0}` 表示正常。目标语义为位 9 = 正常指示位。详见 [ADR-0001](docs/adr/0001-binary-encoding-bit-9-semantics.md)。

**SFT 标签 (SFT Label)**:
一段视频的结构化训练样本,基于分析结果(事件结果 + 证据帧)改写而成。属性值受 `event_options.yaml` 封闭枚举约束,可在 Web UI 中人工编辑。与二进制编码/报告并列为系统的核心输出。
_Avoid_: training data, annotation, ground truth

**证据 (Evidence)**:
逐视频的可视化标注文件(`<stem>_evidence.json`),包含多边形和矩形标注,标注事件实例在关键帧中的空间位置。证据与 SFT 标签紧密关联,为训练样本提供空间锚定。
_Avoid_: bbox, annotation file

### Agent 运行时

**Agent 运行时 (Agent Runtime)**:
`agent/` 下的 TypeScript 服务:多轮 loop 驱动 LLM 调用工具完成任务,经 HTTP+SSE 对外服务(默认 127.0.0.1:8602),由 web 层 `/api/agent/*` 代理。内含 kosong(vendored 自 kimi-code 的 LLM 抽象层)、工具注册表与调度器、权限链、沙盒。
_Avoid_: Node backend, chatbot service

**检测 Agent (Detection Agent)**:
Agent 运行时中执行视频检测的会话角色:按系统 prompt 编排 video_meta / extract_frames / draw_boxes 等工具逐步观察视频,最终以 `submit_detection` 工具提交结构化结果(11 位编码 + Markdown 报告),输出契约与批量流水线一致。
_Avoid_: expert agent(事件专家是批量流水线内角色,与检测 Agent 不同)

**工具 (Tool)**:
Agent 运行时可调用的最小能力单元,声明 `{name, description, parameters}` 与资源访问(accesses);注册表管理,调度器按访问冲突并行执行同批调用。内置 7 个:video_meta / extract_frames / draw_boxes / read_file / write_file / run_script / submit_detection。
_Avoid_: function call, action

**权限模式 (Permission Mode)**:
工具执行前的裁决策略,三档:`yolo`(全部放行)、`manual`(逐次人工确认)、`auto`(自动裁决,危险操作仍询问)。policy 责任链首个命中生效;会话级批准记忆免除重复确认。沙盒越界属硬 veto,不进权限链。
_Avoid_: approval flow, access control

**沙盒 (Sandbox)**:
文件访问的硬边界:所有路径解析后必须落在 workspace(+ additionalDirs)内,敏感文件(`.env`、私钥、credentials 等)一律 veto。沙盒约束不可被任何权限模式豁免。
_Avoid_: chroot, container

**工具服务 (Tool Server)**:
`traffic_analyzer/toolserver/` 下的 Python 本地 HTTP 服务(默认 127.0.0.1:8601),向 Agent 运行时暴露视频元信息、抽帧、画框能力;`--workspace` 必填,视频路径越界返回 403。CV 计算全部留在 Python,TS 侧只做 HTTP client。
_Avoid_: tool API, video service
