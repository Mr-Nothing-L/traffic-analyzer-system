# 基于LLM/VLM的高速公路监控视频交通事件分析框架设计文档

**版本**: V1.0  
**设计日期**: 2026-04-30  
**设计目标**: 基于大模型视觉语言模型(VLM) API，构建配置驱动、可扩展、Agent化的高速公路交通事件智能分析框架  
**与现有系统关系**: 本框架为LLM-VLM原生架构，可与现有传统CV系统（`高速交通事件判别系统_Agent工作流.md`、`merge_tracks.py`）互补融合

---

## 目录

1. [系统架构图](#一系统架构图)
2. [核心模块设计](#二核心模块设计)
3. [配置文件格式](#三配置文件格式)
4. [LLM调用策略](#四llm调用策略)
5. [逆行判断完整示例](#五逆行判断完整示例)
6. [输出报告格式](#六输出报告格式)
7. [与现有系统的整合建议](#七与现有系统的整合建议)
8. [实现路线图](#八实现路线图)

---

## 一、系统架构图

### 1.1 整体架构（分层视图）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              应用接口层 (API/GUI)                              │
│   - 视频上传接口    - 任务状态查询    - 报告查看/导出    - 配置管理界面          │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           编排调度层 (Orchestrator)                            │
│   - 任务生命周期管理    - 模块调用编排    - 上下文状态机    - 异常重试机制       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────────┐
│   视频预处理模块        │ │   VLM推理引擎模块      │ │   逻辑推理引擎模块      │
│   (VideoPreprocessor)  │ │   (VLMInferenceEngine) │ │   (LogicEngine)        │
│   - 自适应采样         │ │   - Prompt组装        │ │   - 判断链执行         │
│   - 关键帧提取         │ │   - 多模态调用        │ │   - 条件分支路由       │
│   - 片段裁剪           │ │   - 响应解析          │ │   - 置信度聚合         │
│   - 元数据提取         │ │   - Token管理         │ │   - 难例递归分析       │
└───────────────────────┘ └───────────────────────┘ └───────────────────────┘
                    │                 │                 │
                    └─────────────────┼─────────────────┘
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          数据融合与报告生成层                                   │
│   - CV轨迹数据融合    - VLM语义结果融合    - 冲突消解    - 格式化报告输出       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────────┐
│   配置管理层           │ │   外部数据适配层        │ │   存储与缓存层         │
│   (ConfigManager)      │ │   (ExternalAdapter)    │ │   (Storage)            │
│   - 事件类别配置       │ │   - 轨迹数据接入       │ │   - 中间结果缓存       │
│   - 逻辑链配置         │ │   - 地图/GIS数据       │ │   - 报告持久化         │
│   - Prompt模板配置     │ │   - 天气/时间数据      │ │   - 视频帧索引         │
└───────────────────────┘ └───────────────────────┘ └───────────────────────┘
```

### 1.2 Agent工作流调用流程

```
输入视频
    │
    ▼
[Step 0] 配置加载
    │ 读取 event_categories.yaml + logic_chains.yaml + prompt_templates.yaml
    │
    ▼
[Step 1] 视频预处理
    │ 粗采样(1 FPS) → 场景分析 → 精采样(4 FPS, 按需) → 关键帧序列
    │
    ▼
[Step 2] 全局场景理解 (VLM调用 #1)
    │ 输入: 关键帧序列
    │ 输出: 道路结构、车道方向、交通流量、天气光照
    │
    ▼
[Step 3] 事件类别循环 (对每类事件执行)
    │
    ├── 若事件为"简单类别" (直接VLM可识别)
    │   │   └── [Step 3a] 单次VLM检测 → 直接输出结果
    │
    └── 若事件为"难例类别" (配置逻辑链)
        │   └── [Step 3b] 逻辑链执行引擎
        │       ├── 步骤1: VLM调用 (如判断车流方向)
        │       ├── 步骤2: 条件判断/数据计算
        │       ├── 步骤3: VLM调用 (如判断目标行为)
        │       ├── 步骤4: CV数据融合 (如轨迹方向验证)
        │       └── ... (根据配置动态展开)
    │
    ▼
[Step 4] 结果融合与冲突消解
    │ 多类别结果聚合、同一车辆的多种事件关联、置信度校准
    │
    ▼
[Step 5] 报告生成
    │ 按模板组装结构化报告 + 二进制编码输出
    │
    ▼
输出: 结构化JSON报告 + 可视化HTML/Markdown报告
```

### 1.3 状态机模型

框架内部维护一个`AnalysisContext`状态对象，贯穿整个分析流程：

```
AnalysisContext {
    video_meta: VideoMetadata        # 视频基本信息
    config: SystemConfig              # 加载的配置全集
    scene_understanding: SceneInfo    # 全局场景理解结果
    keyframes: List[Keyframe]         # 关键帧序列（带时间戳）
    cv_tracks: Dict[track_id, Track]  # 外部CV轨迹数据（可选）
    event_results: Dict[event_type, EventResult]  # 各类事件分析结果
    llm_call_log: List[LLMCallRecord] # LLM调用日志（用于审计和调试）
    final_report: Report              # 最终报告
}
```

---

## 二、核心模块设计

### 2.1 配置管理模块 (ConfigManager)

**职责**: 加载、校验、热更新所有配置文件；为其他模块提供配置查询接口。

**输入**: 配置文件路径 (`configs/` 目录)

**输出**: 配置对象树

**关键接口**:

```python
class ConfigManager:
    def load_all(self, config_dir: str) -> SystemConfig
    def get_event_categories(self) -> List[EventCategory]
    def get_logic_chain(self, event_type: str) -> Optional[LogicChain]
    def get_prompt_template(self, template_id: str) -> PromptTemplate
    def validate_config(self) -> List[ConfigError]  # 校验配置一致性
    def reload(self) -> SystemConfig  # 热更新支持
```

**配置校验规则**:
- 所有 `event_type` 必须在逻辑链配置中有对应定义（或标记为`direct_vlm`）
- 逻辑链中引用的 `prompt_template_id` 必须存在
- 逻辑链步骤间的 `output_key` 和 `input_key` 必须匹配
- 二进制编码位宽与事件类别数量一致

---

### 2.2 视频预处理模块 (VideoPreprocessor)

**职责**: 将原始视频转换为适合VLM输入的关键帧序列；管理采样策略；生成帧索引。

**输入**: 视频文件路径

**输出**: `KeyframeSequence` 对象

**关键接口**:

```python
class VideoPreprocessor:
    def __init__(self, sampling_config: SamplingConfig)
    
    def process(self, video_path: str) -> KeyframeSequence:
        """
        主处理流程:
        1. 提取视频元数据 (FPS, 分辨率, 时长, 码率)
        2. 第一轮粗采样 (默认 1 FPS)
        3. 运动分析 → 识别高动态/静止区域
        4. 第二轮精采样 (对可疑区域 4 FPS)
        5. 关键帧选择 (去重、质量评分、代表性筛选)
        6. 生成时间戳索引
        """
        
    def extract_segment(self, video_path: str, 
                       start_sec: float, 
                       end_sec: float,
                       fps: float = 4.0) -> KeyframeSequence
        # 对特定时间段提取高精度帧序列
        
    def generate_thumbnail_grid(self, keyframes: List[Keyframe], 
                                grid_size: Tuple[int, int] = (4, 4)) -> Image
        # 将多个关键帧拼接为网格图（用于单次VLM多帧输入）
```

**采样策略说明**:

| 阶段 | 采样率 | 目的 | 触发条件 |
|------|--------|------|----------|
| 粗采样 | 1 FPS | 全局概览、场景理解 | 所有视频必做 |
| 精采样 | 4 FPS | 运动细节、行为判定 | 检测到可疑区域/事件时触发 |
| 片段裁剪 | 原帧率 | 单事件深度分析 | 逻辑链要求细粒度分析时 |

**关键帧质量评分维度**:
- 清晰度 (Laplacian方差)
- 光照充足度
- 车辆遮挡程度（通过运动检测估计）
- 与已选帧的差异度（避免冗余）

---

### 2.3 VLM推理引擎模块 (VLMInferenceEngine)

**职责**: 封装对大模型API的调用；管理Prompt组装、多模态输入、响应解析、Token消耗、重试机制。

**输入**: PromptTemplate + 图像/视频帧 + 上下文变量

**输出**: 结构化解析结果 (JSON/Dict)

**关键接口**:

```python
class VLMInferenceEngine:
    def __init__(self, provider_config: LLMProviderConfig)
    
    def call(self, 
             template_id: str,
             images: List[ImageInput],
             context_vars: Dict[str, Any],
             response_schema: JSONSchema) -> LLMResponse:
        """
        1. 从ConfigManager获取PromptTemplate
        2. 使用Jinja2渲染Prompt（注入context_vars）
        3. 组装多模态消息（system + user含images）
        4. 调用VLM API（带重试、超时、流式可选）
        5. 解析响应（优先JSON模式，fallback正则提取）
        6. 校验输出schema
        7. 记录调用日志（token消耗、耗时、原始响应）
        """
        
    def call_with_video(self,
                        template_id: str,
                        video_segments: List[VideoSegment],
                        context_vars: Dict[str, Any],
                        response_schema: JSONSchema) -> LLMResponse
        # 对支持原生视频输入的模型（如Gemini、Claude 4）直接传视频
        # 对仅支持图像的模型，自动提取关键帧网格
        
    def batch_call(self, 
                   requests: List[BatchRequest]) -> List[LLMResponse]
        # 批量调用优化（若API支持）
        
    def get_usage_stats(self) -> UsageStats
        # 返回累计Token消耗、调用次数、平均延迟
```

**支持的VLM Provider**:

| Provider | 模型示例 | 视频输入 | 图像输入 | 备注 |
|----------|----------|----------|----------|------|
| Anthropic | Claude 4 (Sonnet/Opus) | 是 | 是 | 长上下文，推理强 |
| OpenAI | GPT-4o, GPT-4.1 | 是 | 是 | 结构化输出稳定 |
| Google | Gemini 2.5 Pro/Flash | 是 | 是 | 原生视频支持好 |
| 阿里云 | Qwen-VL-Max | 是 | 是 | 中文场景优化 |

---

### 2.4 逻辑推理引擎模块 (LogicEngine)

**职责**: 执行配置定义的判断逻辑链；管理步骤间的数据流；支持条件分支、循环、VLM调用和CV数据计算的混合编排。

**输入**: LogicChain配置 + AnalysisContext

**输出**: EventResult

**关键接口**:

```python
class LogicEngine:
    def __init__(self, vlm_engine: VLMInferenceEngine, 
                 config_manager: ConfigManager)
    
    def execute(self, 
                logic_chain: LogicChain,
                context: AnalysisContext) -> EventResult:
        """
        执行逻辑链:
        1. 初始化局部变量空间 (local_vars)
        2. 按顺序执行每个Step
        3. 根据step_type路由到不同执行器
        4. 处理条件分支和跳转
        5. 收集证据和中间结果
        6. 输出最终EventResult
        """
        
    def _execute_vlm_step(self, step: VLMStep, local_vars: Dict) -> Dict
    def _execute_compute_step(self, step: ComputeStep, local_vars: Dict) -> Dict
    def _execute_condition_step(self, step: ConditionStep, local_vars: Dict) -> bool
    def _execute_cv_fusion_step(self, step: CVFusionStep, local_vars: Dict) -> Dict
    def _execute_aggregate_step(self, step: AggregateStep, local_vars: Dict) -> Dict
```

**步骤类型定义**:

```python
class StepType(Enum):
    VLM_CALL = "vlm_call"           # 调用VLM进行视觉推理
    COMPUTE = "compute"             # 本地计算（数值比较、统计等）
    CONDITION = "condition"         # 条件分支判断
    CV_FUSION = "cv_fusion"         # 融合外部CV数据
    AGGREGATE = "aggregate"         # 多源结果聚合
    LOOP = "loop"                   # 循环执行子链（如逐车辆分析）
```

---

### 2.5 数据融合与报告生成模块 (ReportGenerator)

**职责**: 融合多类别事件结果、消解冲突、生成结构化报告。

**输入**: AnalysisContext（含全部event_results）

**输出**: `Report` 对象（JSON + Markdown/HTML）

**关键接口**:

```python
class ReportGenerator:
    def __init__(self, report_template: ReportTemplate)
    
    def generate(self, context: AnalysisContext) -> Report:
        """
        1. 全局场景描述生成
        2. 按事件类别顺序输出分析结果
        3. 冲突检测与消解（同一车辆的矛盾判定）
        4. 二进制编码生成
        5. 处置建议映射
        6. 组装最终报告
        """
        
    def _resolve_conflicts(self, 
                          results: List[EventResult]) -> List[EventResult]
        # 冲突规则示例:
        # - 若车辆被判定"逆行"，则同车辆的"违停"判定降级或移除
        # - 若"抛洒物"与"行人闯入"空间重叠，优先输出置信度高的
        
    def _encode_binary(self, 
                      event_results: List[EventResult],
                      category_order: List[str]) -> str
        # 生成 {0_1_0_1} 格式编码
```

---

### 2.6 外部数据适配模块 (ExternalAdapter)

**职责**: 接入现有CV系统的输出（如`merge_tracks.py`的轨迹数据），转换为框架内部标准格式。

**关键接口**:

```python
class ExternalAdapter:
    def load_cv_tracks(self, tracks_json_path: str) -> Dict[TrackID, Track]
    def load_video_info(self, video_info_json_path: str) -> VideoMetadata
    
    def convert_track_format(self, 
                            raw_tracks: Dict) -> Dict[TrackID, StandardTrack]:
        """
        将merge_tracks.py输出转换为标准Track格式:
        - 统一坐标系
        - 计算速度/方向向量
        - 标记合并轨迹的原始片段
        """
        
    def query_track_by_time_range(self,
                                  tracks: Dict[TrackID, StandardTrack],
                                  start_sec: float,
                                  end_sec: float,
                                  road_id: Optional[int] = None) -> List[StandardTrack]
```

**标准Track数据结构**:

```python
@dataclass
class StandardTrack:
    track_id: str
    road_id: int
    enter_frame: int
    exit_frame: int
    boxes: List[BoundingBox]  # [frame, x1, y1, w, h, cx, cy, area]
    total_displacement: float
    lifetime_sec: float
    direction_vector: Tuple[float, float]  # (dx, dy) 归一化方向
    speed_px_per_sec: float
    merged_from: List[TrackFragment]  # 若来自merge_tracks.py
    appearance_feature: Optional[np.ndarray]  # HSV直方图等
```

---

## 三、配置文件格式

### 3.1 事件类别定义配置 (`event_categories.yaml`)

```yaml
# 事件类别定义
# 每个事件类别对应一个二进制编码位
# 新增类别只需在此文件追加，系统自动识别

event_categories:
  - event_id: 0
    name: "违停"
    name_en: "illegal_parking"
    description: "车辆在非允许停车区域静止停留超过规定时长"
    definition: |
      判定标准：
      1. 车辆在行车道或应急车道内持续静止
      2. 停留时长 >= 5秒（正常车道）或 >= 2秒（应急车道）
      3. 排除等红灯、堵车等正常停滞场景
    severity: "high"
    detection_mode: "direct_vlm"  # 直接VLM检测，无需复杂逻辑链
    default_confidence_threshold: 0.7

  - event_id: 1
    name: "逆行"
    name_en: "wrong_way_driving"
    description: "车辆行驶方向与所在车道规定方向相反"
    definition: |
      判定标准：
      1. 先确定各车道正常行驶方向
      2. 检测目标车辆实际位移方向
      3. 若实际方向与规定方向相反，则判定逆行
      4. 倒车行为视为逆行的一种特殊形式
    severity: "critical"
    detection_mode: "logic_chain"  # 需要逻辑链判断
    logic_chain_id: "wrong_way_chain"
    default_confidence_threshold: 0.8

  - event_id: 2
    name: "应急车道占用"
    name_en: "emergency_lane_occupation"
    description: "非紧急情况下车辆占用应急车道行驶或停车"
    definition: |
      判定标准：
      1. 车辆位于应急车道/路肩区域内
      2. 车辆处于行驶或静止状态
      3. 排除救援车辆、施工车辆等特许车辆
    severity: "high"
    detection_mode: "direct_vlm"
    default_confidence_threshold: 0.75

  - event_id: 3
    name: "抛洒物"
    name_en: "road_debris"
    description: "道路上有从车辆掉落的物体或障碍物"
    definition: |
      判定标准：
      1. 道路上出现非固定物体
      2. 物体位置对交通构成潜在威胁
      3. 排除路面固有设施（如反光锥、标线等）
    severity: "medium"
    detection_mode: "direct_vlm"
    default_confidence_threshold: 0.7

  - event_id: 4
    name: "行人闯入"
    name_en: "pedestrian_intrusion"
    description: "行人进入高速公路行车区域"
    definition: |
      判定标准：
      1. 检测到人体形态目标
      2. 目标位于行车道、应急车道或中央隔离带
      3. 排除道路维护人员（通过服装/装备判断）
    severity: "critical"
    detection_mode: "direct_vlm"
    default_confidence_threshold: 0.8

# 系统配置
system:
  binary_encoding:
    format: "{bit0_bit1_bit2_bit3_bit4}"
    # 例如: 违停+抛洒物 = {1_0_0_1_0}
    # 位顺序与event_id严格对应
  
  # 新增类别自动分配event_id的规则
  auto_assign_id: true
  
  # 严重级别映射到处置建议
  severity_to_advice:
    critical: "立即拦截/紧急处置"
    high: "巡逻车核查/重点监控"
    medium: "监控跟踪/记录备案"
    low: "日志记录/定期巡检"
```

### 3.2 逻辑判断链配置 (`logic_chains.yaml`)

```yaml
# 难例事件的逻辑判断链配置
# 每个逻辑链是一个步骤序列，支持条件分支和变量传递

logic_chains:
  - chain_id: "wrong_way_chain"
    name: "逆行/倒车判定逻辑链"
    description: "通过多步骤分析判定逆行事件，解决VLM直接识别困难的问题"
    
    # 输入参数定义
    inputs:
      - name: "keyframes"
        source: "context.keyframes"
        description: "视频关键帧序列"
      - name: "scene_info"
        source: "context.scene_understanding"
        description: "全局场景理解结果"
      - name: "cv_tracks"
        source: "context.cv_tracks"
        description: "CV轨迹数据（可选）"
    
    # 局部变量初始化
    local_vars:
      road_directions: null      # 各车道正常方向
      suspicious_vehicles: []    # 可疑车辆列表
      verified_events: []        # 最终确认的事件
    
    steps:
      # ---- 步骤1: 全局车流方向分析 ----
      - step_id: "s1_direction_analysis"
        type: "vlm_call"
        name: "车道正常行驶方向判定"
        description: "分析视频中各车道的正常车流方向"
        prompt_template_id: "direction_analysis"
        images:
          source: "keyframes.grid_sample"  # 使用网格采样图
          max_frames: 16
        context_vars:
          scene_description: "{{ scene_info.description }}"
          road_count: "{{ scene_info.road_count }}"
        output_key: "road_directions"
        output_schema:
          type: "object"
          properties:
            roads:
              type: "array"
              items:
                type: "object"
                properties:
                  road_id: {type: "integer"}
                  road_name: {type: "string"}
                  normal_direction:  # 正常方向向量
                    type: "object"
                    properties:
                      dx: {type: "number", description: "X方向分量 (-1~1)"}
                      dy: {type: "number", description: "Y方向分量 (-1~1)，负为远离摄像头，正为朝向摄像头"}
                  confidence: {type: "number", minimum: 0, maximum: 1}
                  evidence: {type: "string"}
          required: ["roads"]
        
      - step_id: "s1_validate"
        type: "compute"
        name: "方向自洽性校验"
        script: |
          # Python表达式，操作local_vars
          roads = road_directions['roads']
          if len(roads) >= 2:
              # 双向道路的方向应大致相反
              dy_sum = sum(r['normal_direction']['dy'] for r in roads)
              # dy_sum应接近0（一正一负）
              local_vars['direction_coherent'] = abs(dy_sum) < 0.5
          else:
              local_vars['direction_coherent'] = True
        output_key: "direction_validation"
      
      - step_id: "s1_branch"
        type: "condition"
        name: "方向校验分支"
        condition: "direction_validation.direction_coherent == true"
        on_true: "continue"   # 继续执行下一步
        on_false:
          action: "retry"
          retry_step: "s1_direction_analysis"
          max_retries: 2
          on_retry_exhausted: "abort_with_low_confidence"
      
      # ---- 步骤2: 可疑车辆检测 ----
      - step_id: "s2_suspicious_detection"
        type: "vlm_call"
        name: "可疑逆行/倒车车辆检测"
        description: "检测视频中疑似存在逆行或倒车行为的车辆"
        prompt_template_id: "suspicious_vehicle_detection"
        images:
          source: "keyframes.all"
          max_frames: 20
        context_vars:
          road_directions: "{{ road_directions }}"
        output_key: "suspicious_vehicles"
        output_schema:
          type: "object"
          properties:
            suspicious_vehicles:
              type: "array"
              items:
                type: "object"
                properties:
                  vehicle_id: {type: "string"}
                  vehicle_desc: {type: "string"}
                  road_id: {type: "integer"}
                  suspicion_type: {type: "string", enum: ["wrong_way", "backing", "unclear"]}
                  time_range:  # 可疑时间段
                    type: "object"
                    properties:
                      start_sec: {type: "number"}
                      end_sec: {type: "number"}
                  bounding_box_approx:  # 大致位置
                    type: "object"
                    properties:
                      x: {type: "number"}
                      y: {type: "number"}
                      w: {type: "number"}
                      h: {type: "number"}
                  confidence: {type: "number"}
                  evidence_frames:  # 证据帧索引
                    type: "array"
                    items: {type: "integer"}
      
      # ---- 步骤3: 对每辆可疑车辆深度分析 ----
      - step_id: "s3_loop_vehicles"
        type: "loop"
        name: "逐车辆深度分析"
        loop_over: "suspicious_vehicles.suspicious_vehicles"
        loop_var: "vehicle"
        sub_chain: "vehicle_deep_analysis"
        output_key: "vehicle_analysis_results"
      
      # ---- 步骤4: CV轨迹数据融合验证 ----
      - step_id: "s4_cv_fusion"
        type: "cv_fusion"
        name: "CV轨迹方向验证"
        description: "使用CV轨迹数据验证VLM的逆行判定"
        condition: "cv_tracks is not null"
        fusion_logic:
          # 匹配VLM检测到的车辆与CV轨迹
          match_by: ["time_range", "spatial_overlap", "road_id"]
          # 若CV轨迹方向与VLM判定一致，提升置信度
          # 若CV轨迹方向与VLM判定矛盾，标记为冲突待人工复核
          confidence_boost_on_agreement: 0.15
          flag_conflict_on_disagreement: true
        output_key: "cv_fusion_results"
      
      # ---- 步骤5: 结果聚合 ----
      - step_id: "s5_aggregate"
        type: "aggregate"
        name: "逆行事件最终聚合"
        sources:
          - "vehicle_analysis_results"
          - "cv_fusion_results"
        aggregation_rules:
          # 聚合规则
          - rule: "confidence_weighted_vote"
            description: "按置信度加权投票"
          - rule: "temporal_merge"
            description: "时间重叠的同一车辆事件合并"
            time_gap_threshold_sec: 5.0
        output_key: "final_wrong_way_events"

  # ---- 子链: 单车辆深度分析 ----
  - chain_id: "vehicle_deep_analysis"
    name: "单车辆逆行深度分析子链"
    inputs:
      - name: "vehicle"
        source: "parent_loop.vehicle"
      - name: "road_directions"
        source: "parent.road_directions"
    
    steps:
      - step_id: "va_s1_extract_segment"
        type: "compute"
        name: "提取车辆时间段"
        script: |
          start_sec = max(0, vehicle['time_range']['start_sec'] - 5)
          end_sec = vehicle['time_range']['end_sec'] + 5
          local_vars['segment_range'] = {'start': start_sec, 'end': end_sec}
        output_key: "segment_range"
      
      - step_id: "va_s2_precise_frames"
        type: "compute"
        name: "获取精采样帧"
        # 调用VideoPreprocessor提取该时间段的高精度帧
        action: "call_module"
        module: "VideoPreprocessor.extract_segment"
        params:
          start_sec: "{{ segment_range.start }}"
          end_sec: "{{ segment_range.end }}"
          fps: 4.0
        output_key: "precise_frames"
      
      - step_id: "va_s3_direction_verify"
        type: "vlm_call"
        name: "车辆移动方向精判"
        description: "在精采样帧上判断该车辆的实际移动方向"
        prompt_template_id: "vehicle_direction_verify"
        images:
          source: "precise_frames"
          max_frames: 16
        context_vars:
          vehicle_desc: "{{ vehicle.vehicle_desc }}"
          road_normal_direction: "{{ road_directions.roads[vehicle.road_id].normal_direction }}"
        output_key: "vehicle_direction_result"
        output_schema:
          type: "object"
          properties:
            actual_direction:
              type: "object"
              properties:
                dx: {type: "number"}
                dy: {type: "number"}
            direction_description: {type: "string"}
            is_opposite_to_normal: {type: "boolean"}
            is_backing: {type: "boolean"}  # 是否为倒车（车头朝向与移动方向相反）
            confidence: {type: "number"}
            evidence_frames: {type: "array", items: {type: "integer"}}
      
      - step_id: "va_s4_final_decision"
        type: "condition"
        name: "最终判定"
        condition: "vehicle_direction_result.is_opposite_to_normal == true"
        on_true:
          action: "emit_event"
          event:
            event_type: "wrong_way_driving"
            subtype: "{{ 'backing' if vehicle_direction_result.is_backing else 'wrong_way' }}"
            vehicle_id: "{{ vehicle.vehicle_id }}"
            vehicle_desc: "{{ vehicle.vehicle_desc }}"
            road_id: "{{ vehicle.road_id }}"
            time_range: "{{ vehicle.time_range }}"
            confidence: "{{ vehicle_direction_result.confidence }}"
            evidence_frames: "{{ vehicle_direction_result.evidence_frames }}"
            reasoning_chain:
              - "检测到可疑车辆: {{ vehicle.vehicle_desc }}"
              - "所在车道正常方向: {{ road_directions.roads[vehicle.road_id].normal_direction }}"
              - "车辆实际移动方向: {{ vehicle_direction_result.actual_direction }}"
              - "判定结果: 方向相反，确认逆行"
        on_false:
          action: "discard_suspicion"
          reason: "车辆实际方向与车道正常方向一致，排除逆行"

```

### 3.3 Prompt模板配置 (`prompt_templates.yaml`)

```yaml
# Prompt模板库
# 使用Jinja2语法，支持变量注入
# 每个模板包含system prompt和user prompt

prompt_templates:
  - template_id: "direction_analysis"
    name: "车道方向分析"
    description: "分析视频中各车道的正常行驶方向"
    system_prompt: |
      你是一位专业的高速公路交通监控分析专家。你的任务是通过观察监控视频帧，分析各车道的正常车流行驶方向。
      
      分析原则：
      1. 观察多辆正常行驶的车辆，综合判断车流行进趋势
      2. 使用坐标系描述方向：X轴为水平方向（左负右正），Y轴为垂直方向（上负下正）
      3. Y方向尤为重要：dy > 0 表示车辆朝向摄像头方向行驶（画面下方移动），dy < 0 表示远离摄像头（画面上方移动）
      4. 高速公路通常为双向对向行驶，两侧车道方向应相反
      5. 只输出客观观察结果，不做主观推断
      
      输出必须为JSON格式。
    
    user_prompt: |
      请分析以下高速公路监控视频的关键帧，判断每条主路/车道的正常行驶方向。
      
      场景信息：
      - 道路数量: {{ road_count }}
      - 场景描述: {{ scene_description }}
      
      视频关键帧按时间顺序排列。请仔细观察车辆的移动趋势。
      
      请输出以下JSON格式：
      ```json
      {
        "roads": [
          {
            "road_id": 0,
            "road_name": "左侧主路",
            "normal_direction": {"dx": 0.0, "dy": 1.0},
            "confidence": 0.95,
            "evidence": "观察到3辆白色轿车和1辆货车均从画面上方向下方移动"
          }
        ]
      }
      ```

  - template_id: "suspicious_vehicle_detection"
    name: "可疑逆行车辆检测"
    description: "检测疑似逆行或倒车的车辆"
    system_prompt: |
      你是一位高速公路交通监控分析专家。已知各车道的正常行驶方向，请在视频中检测疑似逆行或倒车的车辆。
      
      特别注意：
      1. 倒车行为（车辆车头朝向正常方向，但整车向后移动）属于逆行的一种
      2. 应急车道上的异常移动车辆需要重点关注
      3. 区分正常变道、掉头与真正的逆行
      4. 如果无法确定，标记为"unclear"
      
      输出必须为JSON格式。
    
    user_prompt: |
      已知各车道正常方向如下：
      {% for road in road_directions.roads %}
      - {{ road.road_name }}: 方向向量(dx={{ road.normal_direction.dx }}, dy={{ road.normal_direction.dy }})
        {% if road.normal_direction.dy > 0 %}朝向摄像头行驶{% else %}远离摄像头行驶{% endif %}
      {% endfor %}
      
      请仔细观察视频关键帧，检测所有疑似逆行或倒车的车辆。
      对每辆可疑车辆，请输出：
      - 车辆描述（颜色、车型、大致位置）
      - 可疑类型（wrong_way/backing/unclear）
      - 可疑时间段
      - 置信度
      - 证据帧编号

  - template_id: "vehicle_direction_verify"
    name: "车辆移动方向精判"
    description: "在精采样帧上精确判断单辆车的移动方向"
    system_prompt: |
      你是一位高精度交通运动分析专家。请通过对比同一车辆在不同时刻的位置，精确判断其移动方向。
      
      分析要点：
      1. 逐帧对比车辆中心位置的变化
      2. 注意区分车头朝向和实际移动方向
      3. 倒车行为的特征：车头朝向正常，但整车向相反方向移动
      4. 给出方向向量估计值
      
      输出必须为JSON格式。
    
    user_prompt: |
      目标车辆描述: {{ vehicle_desc }}
      所在车道正常方向: dx={{ road_normal_direction.dx }}, dy={{ road_normal_direction.dy }}
      
      以下是一组精采样帧（4 FPS），请追踪该车辆的移动轨迹：
      - 若dy与正常方向相反，则判定为逆行
      - 若车头朝向与移动方向相反，则标记为倒车(backing=true)
      
      请输出精确的JSON分析结果。

  - template_id: "scene_understanding"
    name: "全局场景理解"
    description: "首次VLM调用，理解整体场景"
    system_prompt: |
      你是一位交通监控场景分析专家。请对提供的高速公路监控视频进行全局场景理解。
    user_prompt: |
      请分析这段高速公路监控视频，输出以下信息：
      ```json
      {
        "scene_type": "highway_bidirectional",
        "road_count": 2,
        "road_description": "双向四车道高速公路，有中央隔离带",
        "weather": "sunny",
        "lighting": "daylight",
        "traffic_density": "medium",
        "visible_events": ["是否有明显可见的异常事件"],
        "notes": "其他值得注意的信息"
      }
      ```

  - template_id: "direct_event_detection"
    name: "直接事件检测"
    description: "用于可直接VLM识别的事件类别"
    system_prompt: |
      你是一位交通事件检测专家。请在监控视频中检测以下类型的事件：{{ event_name }}
      
      事件定义: {{ event_definition }}
      
      输出必须为JSON格式。若无此类事件，返回空数组。
    user_prompt: |
      请检测视频中的"{{ event_name }}"事件。
      
      判定标准：
      {{ event_definition }}
      
      请输出检测到的所有事件实例：
      ```json
      {
        "events": [
          {
            "event_type": "{{ event_type }}",
            "location": "事件发生位置描述",
            "time_range": {"start_sec": 10.5, "end_sec": 25.0},
            "confidence": 0.88,
            "evidence_frames": [10, 15, 20],
            "description": "详细描述"
          }
        ],
        "event_count": 1
      }
      ```
```

---

## 四、LLM调用策略

### 4.1 视频输入处理策略

#### 策略A: 关键帧序列（适用于图像型VLM）

```
原始视频
    │
    ├── 粗采样 (1 FPS) ──→ 场景理解用帧 (最多32帧)
    │
    ├── 精采样 (4 FPS) ──→ 事件分析用帧 (最多16帧/片段)
    │
    └── 关键帧选择算法:
            1. 均匀时间分布采样
            2. 运动显著性加权
            3. 去冗余（SSIM相似度<0.85才保留）
            4. 质量过滤（模糊帧丢弃）
```

**多帧输入方式**:
- **网格拼接**: 将最多16帧拼成4x4网格，作为单张图输入（节省Token）
- **序列输入**: 按时间顺序排列，在Prompt中标注时间戳
- **分批次调用**: 超过16帧时分多次调用，结果聚合

#### 策略B: 原生视频输入（适用于视频型VLM）

```
原始视频
    │
    ├── 直接上传视频片段（Gemini/Claude 4支持）
    │
    └── 或预裁剪关键片段:
            - 按事件时间范围裁剪MP4
            - 压缩至合适分辨率（如720p）
            - 控制时长（建议30秒内）
```

**选择策略**:

| 场景 | 推荐策略 | 原因 |
|------|----------|------|
| 全局场景理解 | 关键帧网格 | 覆盖时间长，Token可控 |
| 单事件深度分析 | 原生视频/精采样帧 | 需要运动连续性 |
| 实时性要求高 | 关键帧序列 | 预处理快，API延迟低 |
| 长视频(>5分钟) | 分段+摘要 | 避免上下文溢出 |

### 4.2 Prompt设计原则

1. **角色锚定**: 每个Prompt开头明确专家角色（"你是一位...专家"）
2. **坐标系统一**: 统一定义画面坐标系，避免方向描述歧义
3. **输出格式强制**: 要求JSON输出，提供完整schema示例
4. **Few-shot示例**: 对难例类别，在Prompt中嵌入1-2个正例和反例
5. **置信度要求**: 强制模型输出置信度分数，便于阈值过滤
6. **证据追溯**: 要求模型引用证据帧编号，便于人工复核

### 4.3 多轮调用管理

```python
class LLMCallManager:
    """管理分析流程中的所有LLM调用"""
    
    def __init__(self):
        self.call_history = []
        self.token_budget = 100000  # 单次分析总Token预算
        self.used_tokens = 0
    
    def call(self, request: LLMRequest) -> LLMResponse:
        # 1. 预算检查
        if self.used_tokens + request.estimated_tokens > self.token_budget:
            raise TokenBudgetExceeded()
        
        # 2. 缓存检查（相同输入复用结果）
        cache_key = self._compute_cache_key(request)
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        # 3. 执行调用
        response = self._execute_call(request)
        
        # 4. 记录日志
        self.call_history.append({
            'timestamp': time.now(),
            'template_id': request.template_id,
            'input_tokens': response.usage.input_tokens,
            'output_tokens': response.usage.output_tokens,
            'duration_ms': response.duration_ms,
            'cache_hit': False
        })
        
        self.used_tokens += response.usage.total_tokens
        return response
    
    def get_call_chain(self) -> List[Dict]:
        """返回完整的调用链，用于审计和调试"""
        return self.call_history
```

**调用优化策略**:
- **结果缓存**: 相同帧+相同Prompt的结果缓存复用
- **并行调用**: 无依赖关系的VLM调用并行执行
- **降级策略**: Token预算耗尽时，降级为低精度分析或跳过非关键步骤
- **重试机制**: 网络/API错误时指数退避重试（最多3次）

### 4.4 响应解析与容错

```python
class ResponseParser:
    """解析VLM响应为结构化数据"""
    
    def parse(self, raw_response: str, schema: JSONSchema) -> ParseResult:
        # 策略1: 直接JSON解析
        try:
            data = json.loads(self._extract_json_block(raw_response))
            self._validate_schema(data, schema)
            return ParseResult(success=True, data=data)
        except:
            pass
        
        # 策略2: 正则提取 + 补全
        try:
            data = self._regex_extract_and_repair(raw_response, schema)
            return ParseResult(success=True, data=data, repaired=True)
        except:
            pass
        
        # 策略3: 二次VLM调用，要求重新格式化
        return ParseResult(success=False, raw=raw_response)
```

---

## 五、逆行判断完整示例

### 5.1 场景设定

- **输入**: 一段30秒的高速公路监控视频（双向车道）
- **已知问题**: 应急车道有一辆白色小车，粗采样下看似静止，实际在缓慢倒车
- **配置**: 使用上述 `wrong_way_chain` 逻辑链

### 5.2 执行流程详解

#### Phase 1: 视频预处理

```
输入: 02_ch1_20260401_142812_2.mp4 (30秒, 25 FPS)

执行:
1. 粗采样: 提取30帧 (1 FPS)
2. 运动分析: 检测到应急车道区域有微弱运动信号
3. 精采样触发: 对5-15秒区间提升采样至4 FPS (40帧)
4. 关键帧选择: 
   - 全局理解帧: 均匀选取8帧 (0s, 4s, 8s, 12s, 16s, 20s, 24s, 28s)
   - 可疑区域帧: 5-15秒精采样中选6帧 (5.0s, 7.5s, 10.0s, 12.5s, 15.0s)
```

#### Phase 2: 全局场景理解 (VLM Call #1)

```
输入: 8帧全局网格图
Prompt: scene_understanding 模板

VLM输出:
{
  "scene_type": "highway_bidirectional",
  "road_count": 2,
  "road_description": "双向四车道，中央有绿化带隔离，左侧两车道为南向，右侧两车道为北向",
  "weather": "sunny",
  "traffic_density": "light",
  "visible_events": ["应急车道有白色车辆停留"],
  "notes": "应急车道车辆位置在画面右侧，需关注其运动状态"
}
```

#### Phase 3: 逻辑链执行 - 逆行判定

**步骤1: 车道方向分析 (VLM Call #2)**

```
输入: 16帧网格图（覆盖完整时间跨度）
Prompt: direction_analysis 模板

VLM输出:
{
  "roads": [
    {
      "road_id": 0,
      "road_name": "左侧主路（南行车道）",
      "normal_direction": {"dx": 0.1, "dy": 0.98},
      "confidence": 0.96,
      "evidence": "观察到5辆车均从画面上方向下方移动，朝向摄像头方向"
    },
    {
      "road_id": 1,
      "road_name": "右侧主路（北行车道）",
      "normal_direction": {"dx": -0.05, "dy": -0.95},
      "confidence": 0.94,
      "evidence": "观察到3辆车从画面下方向上方移动，远离摄像头方向"
    }
  ]
}

本地校验: dy_sum = 0.98 + (-0.95) = 0.03 < 0.5 → 方向自洽 ✓
```

**步骤2: 可疑车辆检测 (VLM Call #3)**

```
输入: 20帧网格图
Prompt: suspicious_vehicle_detection 模板
上下文: road_directions（来自步骤1）

VLM输出:
{
  "suspicious_vehicles": [
    {
      "vehicle_id": "susp_001",
      "vehicle_desc": "白色小轿车，位于右侧应急车道",
      "road_id": 1,
      "suspicion_type": "backing",
      "time_range": {"start_sec": 6.0, "end_sec": 14.0},
      "bounding_box_approx": {"x": 1450, "y": 600, "w": 180, "h": 120},
      "confidence": 0.72,
      "evidence_frames": [6, 8, 10, 12, 14]
    }
  ]
}
```

**步骤3: 逐车辆深度分析 - 子链执行**

```
子链输入:
- vehicle = susp_001
- road_directions = 步骤1结果

子链步骤1: 提取时间段
  segment_range = {start: 1.0, end: 19.0}  # 前后各扩5秒

子链步骤2: 获取精采样帧
  VideoPreprocessor.extract_segment(1.0s, 19.0s, 4 FPS) → 72帧
  进一步筛选代表性帧: 每2秒取2帧 → 16帧

子链步骤3: 方向精判 (VLM Call #4)
  输入: 16帧精采样序列（含时间戳标注）
  Prompt: vehicle_direction_verify 模板
  
  VLM输出:
  {
    "actual_direction": {"dx": 0.02, "dy": 0.92},
    "direction_description": "车辆整体向画面下方移动（Y增加方向）",
    "is_opposite_to_normal": true,
    "is_backing": true,
    "confidence": 0.85,
    "evidence_frames": [8, 10, 12, 14, 16],
    "reasoning": "车辆车头朝向北（与正常方向一致），但整车向南移动（Y增加），
                 与右侧主路正常方向（Y减小）相反，确认为应急车道倒车"
  }

子链步骤4: 条件判断
  is_opposite_to_normal = true → 触发emit_event
  
  生成事件:
  {
    event_type: "wrong_way_driving",
    subtype: "backing",
    vehicle_id: "susp_001",
    vehicle_desc: "白色小轿车，位于右侧应急车道",
    road_id: 1,
    time_range: {start_sec: 6.0, end_sec: 14.0},
    confidence: 0.85,
    evidence_frames: [8, 10, 12, 14, 16],
    reasoning_chain: [...]
  }
```

**步骤4: CV轨迹融合**

```
输入: merge_tracks.py 输出的 vehicles_merged.json

执行:
1. 时间范围匹配: 查找6-14秒区间内的轨迹
2. 空间匹配: 轨迹位置与 (1450, 600) 区域重叠
3. 找到匹配轨迹: merged_track "12+15" (原ID 12和15合并)
   
   轨迹数据:
   - road_id: 1
   - enter_frame: 150 (6s), exit_frame: 350 (14s)
   - total_displacement: 45像素
   - direction_vector: (0.01, 0.89)  # Y正方向
   - lifetime_sec: 8.0s

4. 方向比对:
   VLM判定方向: dy = +0.92
   CV轨迹方向: dy = +0.89
   → 方向一致 ✓
   
5. 置信度提升: 0.85 + 0.15 = 1.0 (封顶0.95)
   最终置信度: 0.95
```

**步骤5: 结果聚合**

```
输入: 单车辆分析结果 + CV融合结果

聚合输出:
{
  "event_type": "wrong_way_driving",
  "subtype": "backing",
  "vehicle": {
    "description": "白色小轿车",
    "location": "右侧主路应急车道",
    "time_range": {"start": "00:06", "end": "00:14"}
  },
  "confidence": 0.95,
  "evidence": {
    "vlm_frames": [8, 10, 12, 14, 16],
    "cv_track_id": "12+15",
    "cv_displacement": "45px (Y正向)"
  },
  "reasoning": "VLM检测到车辆向Y正方向移动，与右侧主路正常方向(Y负)相反；
               CV轨迹验证一致，方向向量dy=+0.89；
               车头朝向与正常方向一致，判定为倒车行为。"
}
```

### 5.3 与传统CV系统的对比

| 维度 | 传统CV系统 | LLM-VLM框架 |
|------|-----------|-------------|
| 倒车检测 | 依赖精采样+模板匹配，容易遗漏慢速倒车 | VLM语义理解，能识别"车头朝北但车往南移" |
| 配置灵活性 | 硬编码判定逻辑 | YAML配置，新增类别无需改代码 |
| 可解释性 | 数值阈值，难以理解 | 自然语言推理链，人工可复核 |
| 处理速度 | 本地计算，实时性好 | API调用依赖网络，适合离线分析 |
| 成本 | 算力成本 | API Token成本 |

---

## 六、输出报告格式

### 6.1 结构化JSON报告

```json
{
  "report_version": "1.0",
  "generated_at": "2026-04-30T14:32:18+08:00",
  "analysis_id": "anal_20260430_143218_abc123",
  
  "video_info": {
    "filename": "02_ch1_20260401_142812_2.mp4",
    "camera_id": "ch1",
    "record_time": "2026-04-01T14:28:12+08:00",
    "duration_sec": 30.0,
    "resolution": "1920x1080",
    "fps": 25,
    "coarse_sample_frames": 30,
    "fine_sample_frames": 40
  },
  
  "scene_summary": {
    "scene_type": "highway_bidirectional",
    "road_count": 2,
    "road_description": "双向四车道高速公路，中央绿化带隔离",
    "weather": "sunny",
    "lighting": "daylight",
    "traffic_density": "light",
    "traffic_flow": {
      "road_0": {"direction": "southbound", "vehicle_count": 5, "avg_speed": "normal"},
      "road_1": {"direction": "northbound", "vehicle_count": 3, "avg_speed": "normal"}
    }
  },
  
  "event_analysis": [
    {
      "event_id": 0,
      "event_name": "违停",
      "event_name_en": "illegal_parking",
      "detected": false,
      "instances": [],
      "summary": "未检测到违停事件"
    },
    {
      "event_id": 1,
      "event_name": "逆行",
      "event_name_en": "wrong_way_driving",
      "detected": true,
      "instance_count": 1,
      "instances": [
        {
          "instance_id": "evt_001",
          "subtype": "backing",
          "subtype_name": "倒车",
          "location": "右侧主路应急车道",
          "time_range": {"start_sec": 6.0, "end_sec": 14.0, "start_time": "00:06", "end_time": "00:14"},
          "vehicle": {
            "description": "白色小轿车",
            "cv_track_id": "12+15",
            "road_id": 1
          },
          "confidence": 0.95,
          "confidence_level": "high",
          "evidence": {
            "vlm_evidence_frames": [8, 10, 12, 14, 16],
            "cv_verification": {
              "track_matched": true,
              "direction_agreement": true,
              "displacement_px": 45,
              "displacement_direction": "Y_positive"
            }
          },
          "reasoning_chain": [
            "步骤1: 判定右侧主路正常方向为北向(Y减小)，置信度0.94",
            "步骤2: VLM检测到应急车道白色小轿车可疑，置信度0.72",
            "步骤3: 精采样分析显示车辆实际向Y正方向移动，与正常方向相反",
            "步骤4: 车头朝向与正常方向一致，确认是倒车行为而非对向闯入",
            "步骤5: CV轨迹(12+15)验证方向一致，dy=+0.89，置信度提升至0.95"
          ],
          "llm_call_chain": [
            {"call_id": 1, "template": "scene_understanding", "tokens": 2048},
            {"call_id": 2, "template": "direction_analysis", "tokens": 3156},
            {"call_id": 3, "template": "suspicious_vehicle_detection", "tokens": 2890},
            {"call_id": 4, "template": "vehicle_direction_verify", "tokens": 3421}
          ]
        }
      ],
      "summary": "检测到1起倒车事件，位于右侧主路应急车道，置信度高"
    },
    {
      "event_id": 2,
      "event_name": "应急车道占用",
      "event_name_en": "emergency_lane_occupation",
      "detected": true,
      "instance_count": 1,
      "instances": [
        {
          "instance_id": "evt_002",
          "location": "右侧主路应急车道",
          "time_range": {"start_sec": 6.0, "end_sec": 14.0},
          "vehicle": {"description": "白色小轿车"},
          "confidence": 0.95,
          "note": "该车同时被判定为倒车，已关联"
        }
      ],
      "summary": "检测到1起应急车道占用，与倒车事件为同一车辆"
    },
    {
      "event_id": 3,
      "event_name": "抛洒物",
      "event_name_en": "road_debris",
      "detected": false,
      "instances": [],
      "summary": "未检测到抛洒物"
    },
    {
      "event_id": 4,
      "event_name": "行人闯入",
      "event_name_en": "pedestrian_intrusion",
      "detected": false,
      "instances": [],
      "summary": "未检测到行人闯入"
    }
  ],
  
  "conflict_resolution": {
    "resolved_conflicts": [
      {
        "conflict_type": "同一车辆多事件",
        "description": "白色小轿车同时触发'逆行'和'应急车道占用'",
        "resolution": "保留两个事件，标记关联关系，不重复计数处置"
      }
    ]
  },
  
  "final_classification": {
    "binary_encoding": "{0_1_1_0_0}",
    "encoding_explanation": {
      "bit_0": "违停 = 0 (未发生)",
      "bit_1": "逆行 = 1 (发生)",
      "bit_2": "应急车道占用 = 1 (发生)",
      "bit_3": "抛洒物 = 0 (未发生)",
      "bit_4": "行人闯入 = 0 (未发生)"
    },
    "active_events": ["逆行", "应急车道占用"]
  },
  
  "recommendations": [
    {
      "level": "critical",
      "event": "逆行(倒车)",
      "action": "立即拦截",
      "details": "应急车道倒车行为极度危险，建议立即调度巡逻车处置",
      "target_vehicle": "白色小轿车，右侧应急车道，00:06-00:14"
    },
    {
      "level": "high",
      "event": "应急车道占用",
      "action": "巡逻车核查",
      "details": "确认是否为故障停车或故意占用",
      "note": "与倒车事件为同一车辆，已合并处置建议"
    }
  ],
  
  "statistics": {
    "total_llm_calls": 4,
    "total_input_tokens": 11515,
    "total_output_tokens": 1892,
    "analysis_duration_sec": 45.3,
    "event_categories_checked": 5,
    "events_detected": 2,
    "events_by_severity": {
      "critical": 1,
      "high": 1,
      "medium": 0,
      "low": 0
    }
  }
}
```

### 6.2 可视化Markdown报告模板

```markdown
# 高速公路交通事件分析报告

**分析ID**: anal_20260430_143218_abc123  
**视频文件**: 02_ch1_20260401_142812_2.mp4  
**分析时间**: 2026-04-30 14:32:18  
**监控点位**: ch1  
**视频时长**: 30.0秒

---

## 一、整体交通情况

**场景类型**: 双向四车道高速公路  
**天气/光照**: 晴天/白天  
**交通密度**: 稀疏

**各主路车流情况**:

| 主路 | 方向 | 车辆数 | 平均速度 |
|------|------|--------|----------|
| 左侧主路 | 南向（朝向摄像头） | 5 | 正常 |
| 右侧主路 | 北向（远离摄像头） | 3 | 正常 |

---

## 二、交通事件分析

### 2.1 逆行/倒车 [发生]

**置信度**: 95% (高)

**事件详情**:
- **类型**: 倒车（逆行子类）
- **位置**: 右侧主路应急车道
- **时间**: 00:06 - 00:14
- **目标车辆**: 白色小轿车

**判定依据**:
1. 右侧主路正常方向为北向（远离摄像头，Y减小）
2. 目标车辆实际移动方向为南向（朝向摄像头，Y增加）
3. 车头朝向与正常方向一致，确认为倒车行为
4. CV轨迹验证：方向向量dy=+0.89，与VLM判定一致

**关键证据帧**: frame_8.jpg, frame_10.jpg, frame_12.jpg, frame_14.jpg, frame_16.jpg

**推理链**:
```
步骤1: 判定右侧主路正常方向为北向(Y减小)，置信度0.94
步骤2: VLM检测到应急车道白色小轿车可疑，置信度0.72
步骤3: 精采样分析显示车辆实际向Y正方向移动，与正常方向相反
步骤4: 车头朝向与正常方向一致，确认是倒车行为而非对向闯入
步骤5: CV轨迹(12+15)验证方向一致，dy=+0.89，置信度提升至0.95
```

### 2.2 应急车道占用 [发生]

**置信度**: 95% (高)

**事件详情**:
- **位置**: 右侧主路应急车道
- **时间**: 00:06 - 00:14
- **目标车辆**: 白色小轿车（与倒车事件为同一车辆）

### 2.3 违停 [未发生]

未检测到违停事件。

### 2.4 抛洒物 [未发生]

未检测到抛洒物。

### 2.5 行人闯入 [未发生]

未检测到行人闯入。

---

## 三、最终分类结果

**二进制编码**: `{0_1_1_0_0}`

| 位 | 事件类别 | 状态 | 说明 |
|----|----------|------|------|
| 0 | 违停 | 0 | 未发生 |
| 1 | 逆行 | 1 | **发生** |
| 2 | 应急车道占用 | 1 | **发生** |
| 3 | 抛洒物 | 0 | 未发生 |
| 4 | 行人闯入 | 0 | 未发生 |

---

## 四、处置建议

| 优先级 | 事件 | 建议措施 | 目标 |
|--------|------|----------|------|
| P0-紧急 | 逆行(倒车) | **立即拦截** | 白色小轿车，右侧应急车道 |
| P1-高 | 应急车道占用 | 巡逻车核查 | 与P0为同一车辆，合并处置 |

---

## 五、技术统计

- **LLM调用次数**: 4次
- **Token消耗**: 输入11,515 / 输出1,892
- **分析耗时**: 45.3秒
- **CV轨迹融合**: 已启用，验证通过
```

### 6.3 二进制编码规范

```
格式: {bit0_bit1_bit2_..._bitN}

规则:
1. 位顺序严格对应 event_categories.yaml 中的 event_id
2. 每位只能是 0 或 1
3. 1 表示该类事件在视频中发生（至少1个实例）
4. 0 表示该类事件未发生
5. 位宽自动扩展：新增类别时，编码自动增加对应位数

示例:
  配置: [违停(id=0), 逆行(id=1), 应急车道(id=2), 抛洒物(id=3), 行人(id=4)]
  编码: {0_1_1_0_0}
  含义: 逆行和应急车道占用发生，其他未发生

  新增"火灾(id=5)"后:
  编码: {0_1_1_0_0_0}
  含义: 同上，火灾未发生

兼容性:
  - 旧系统解析新编码时，忽略超出预期的位数
  - 新系统解析旧编码时，缺失位补0
```

---

## 七、与现有系统的整合建议

### 7.1 与 `merge_tracks.py` 的整合

```
整合点1: 轨迹数据输入
  LLM-VLM框架 ←── vehicles_merged.json ──← merge_tracks.py
  
  适配方式:
  1. ExternalAdapter.load_cv_tracks() 读取 merge_tracks.py 输出
  2. 转换为 StandardTrack 格式
  3. 在逻辑链的 CV_FUSION 步骤中使用

整合点2: 轨迹质量增强
  merge_tracks.py 解决ID切换 → LLM-VLM获得更完整的车辆轨迹
  → 方向判定更准确，减少碎片化误判

整合点3: 双向验证闭环
  VLM检测到可疑车辆 → 查询CV轨迹确认
  CV轨迹发现异常运动 → 触发VLM深度分析
```

### 7.2 与现有Agent工作流的整合

```
┌─────────────────────────────────────────────────────────────┐
│                    融合分析模式 (推荐)                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   传统CV系统                    LLM-VLM框架                   │
│   ───────────                  ───────────                   │
│   视频拆帧(粗+精) ─────┐       视频预处理(粗+精)              │
│   主路判定 ────────────┼────→  全局场景理解(VLM)              │
│   车辆检测+追踪 ───────┤       事件语义检测(VLM)              │
│   轨迹合并 ────────────┼────→  难例逻辑链分析                 │
│   方向计算 ────────────┤       CV数据融合验证                 │
│   阈值判定 ────────────┤       结果融合与冲突消解              │
│                        │                                    │
│                        └────→  统一报告输出                   │
│                                                             │
│   优势: CV提供精确运动数据，VLM提供语义理解，互相验证         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**具体整合方案**:

1. **数据层整合**
   - 统一视频拆帧目录结构，避免重复处理
   - 共享 `video_info.json` 元数据
   - CV轨迹输出直接写入框架缓存目录

2. **判定层互补**
   - 简单事件（违停、抛洒物、行人）: 优先使用VLM直接检测
   - 运动相关事件（逆行、倒车）: VLM + CV轨迹融合判定
   - VLM置信度低时: 降级依赖CV阈值判定
   - CV数据缺失时: 纯VLM推理，标记"无CV验证"

3. **验证层交叉**
   - 传统系统的"四层验证"可映射到框架的验证步骤
   - 主路方向自洽验证 → LogicEngine的compute_step
   - 精采样重判验证 → VideoPreprocessor的精采样触发
   - 位移-方向交叉验证 → CV_FUSION步骤
   - 应急车道强制判定 → 逻辑链中的条件分支

4. **配置层统一**
   - 将传统系统的阈值参数（如停留5秒、位移30像素）提取到配置文件中
   - 传统CV系统和LLM框架共享同一套配置

### 7.3 渐进式迁移路径

```
阶段1: 并行运行（1-2周）
  - 传统CV系统和LLM框架各自独立运行
  - 对比输出结果，建立一致性基准
  - 收集LLM难例，优化Prompt和逻辑链

阶段2: 融合运行（2-4周）
  - LLM框架接入CV轨迹数据
  - 对逆行/倒车等难例启用融合判定
  - 简单事件逐步切换为VLM主导

阶段3: LLM主导（4周后）
  - 新事件类别通过配置快速上线
  - 传统CV作为后备和验证手段
  - 构建事件案例库，持续优化VLM表现
```

---

## 八、实现路线图

### Phase 1: 基础框架搭建（第1-2周）

**目标**: 搭建可运行的最小可行产品(MVP)

**任务清单**:
- [ ] 项目脚手架搭建（Python 3.10+，目录结构）
- [ ] 配置管理模块 (`ConfigManager`) 实现
  - YAML配置加载与校验
  - 事件类别配置解析
- [ ] 视频预处理模块 (`VideoPreprocessor`) 实现
  - OpenCV视频拆帧
  - 粗采样 + 精采样策略
  - 关键帧选择与质量评分
- [ ] VLM推理引擎 (`VLMInferenceEngine`) 实现
  - 支持至少1个Provider（建议OpenAI GPT-4o）
  - Prompt模板渲染（Jinja2）
  - JSON响应解析与容错
- [ ] 基础报告生成器 (`ReportGenerator`) 实现
  - JSON报告输出
  - 二进制编码生成

**交付物**:
- 可处理单视频、输出JSON报告的原型系统
- 支持3个直接VLM检测事件类别

### Phase 2: 逻辑链引擎与难例处理（第3-4周）

**目标**: 实现配置驱动的逻辑判断链，解决逆行等难例

**任务清单**:
- [ ] 逻辑推理引擎 (`LogicEngine`) 实现
  - 步骤执行器框架（VLM_CALL, COMPUTE, CONDITION, AGGREGATE）
  - 局部变量空间管理
  - 条件分支和跳转
- [ ] LOOP步骤类型实现
  - 支持逐车辆/逐片段循环分析
- [ ] CV融合步骤实现
  - 轨迹数据格式转换
  - 时空匹配算法
  - 置信度调整规则
- [ ] 逆行判定逻辑链完整实现
  - 方向分析 → 可疑检测 → 精判 → CV验证 → 聚合
- [ ] Prompt模板库扩充
  - direction_analysis
  - suspicious_vehicle_detection
  - vehicle_direction_verify

**交付物**:
- 可配置逻辑链的完整框架
- 逆行/倒车判定准确率达标（对比传统CV系统）

### Phase 3: 多Provider支持与优化（第5-6周）

**目标**: 提升系统鲁棒性和性能

**任务清单**:
- [ ] 多VLM Provider支持
  - Anthropic Claude 4
  - Google Gemini
  - 阿里云 Qwen-VL
- [ ] 调用优化
  - 结果缓存（Redis/本地）
  - 并行调用执行
  - Token预算管理
- [ ] 外部数据适配层
  - `merge_tracks.py` 输出适配器
  - 其他CV系统轨迹格式适配
- [ ] 错误处理与重试机制
  - API限流处理
  - 响应解析失败降级
  - 超时与熔断

**交付物**:
- 支持多Provider的生产级系统
- 与现有CV系统的数据对接完成

### Phase 4: 可扩展性与工具化（第7-8周）

**目标**: 使系统易于扩展和运维

**任务清单**:
- [ ] 事件类别动态扩展
  - 配置热更新
  - 自动二进制编码位分配
  - 新增类别Prompt自动生成（可选）
- [ ] 可视化报告生成
  - HTML报告（含证据帧高亮）
  - Markdown报告
  - 与JSON报告同步生成
- [ ] 分析任务API服务
  - RESTful API封装
  - 异步任务队列（Celery/RQ）
  - 任务状态查询与回调
- [ ] 日志与审计系统
  - 完整LLM调用链记录
  - 结果可追溯
  - 成本统计

**交付物**:
- 带API服务的完整系统
- 可视化报告界面
- 运维监控面板

### Phase 5: 迭代优化与场景扩展（第9周及以后）

**目标**: 持续优化，扩展应用场景

**任务清单**:
- [ ] 案例库建设
  - 收集难例样本
  - 建立黄金标准标注集
  - A/B测试不同Prompt策略
- [ ] 场景扩展
  - 城市道路监控适配
  - 隧道/桥梁特殊场景
  - 夜间/恶劣天气优化
- [ ] 性能优化
  - 视频预处理加速（GPU）
  - 模型蒸馏/本地化部署探索
  - 边缘计算适配
- [ ] 人机协同
  - 低置信度结果人工复核接口
  - 专家反馈闭环优化
  - 在线学习机制

---

## 附录A: 项目目录结构建议

```
highway-vlm-analyzer/
├── configs/
│   ├── event_categories.yaml      # 事件类别定义
│   ├── logic_chains.yaml          # 逻辑判断链配置
│   └── prompt_templates.yaml      # Prompt模板库
├── src/
│   ├── __init__.py
│   ├── config_manager.py          # 配置管理模块
│   ├── video_preprocessor.py      # 视频预处理模块
│   ├── vlm_engine.py              # VLM推理引擎
│   ├── logic_engine.py            # 逻辑推理引擎
│   ├── report_generator.py        # 报告生成模块
│   ├── external_adapter.py        # 外部数据适配
│   └── models/
│       ├── __init__.py
│       ├── config_models.py       # 配置数据模型
│       ├── context_models.py      # 分析上下文模型
│       └── report_models.py       # 报告数据模型
├── providers/
│   ├── __init__.py
│   ├── base.py                    # Provider抽象基类
│   ├── openai_provider.py         # OpenAI/GPT-4o
│   ├── anthropic_provider.py      # Anthropic/Claude
│   ├── google_provider.py         # Google/Gemini
│   └── aliyun_provider.py         # 阿里云/Qwen
├── prompts/
│   └── templates/                 # Prompt模板文件（可选分离）
├── cache/                         # 中间结果缓存
│   ├── frames/                    # 拆帧输出
│   ├── llm_responses/             # LLM响应缓存
│   └── tracks/                    # CV轨迹数据
├── outputs/                       # 报告输出
│   ├── json/                      # JSON报告
│   └── markdown/                  # Markdown报告
├── tests/
│   ├── unit/                      # 单元测试
│   ├── integration/               # 集成测试
│   └── fixtures/                  # 测试数据
├── scripts/
│   └── run_analysis.py            # 命令行入口
├── api/
│   └── main.py                    # FastAPI服务入口
├── requirements.txt
├── pyproject.toml
└── README.md
```

## 附录B: 关键技术选型建议

| 组件 | 推荐方案 | 备选方案 | 说明 |
|------|----------|----------|------|
| 编程语言 | Python 3.10+ | - | 生态丰富，VLM SDK完善 |
| 配置解析 | PyYAML + Pydantic | JSON Schema | 类型安全，校验方便 |
| Prompt模板 | Jinja2 | f-string | 复杂逻辑和循环支持 |
| 视频处理 | OpenCV + decord | ffmpeg-python | 高性能帧提取 |
| API框架 | FastAPI | Flask | 异步支持，自动生成文档 |
| 任务队列 | Celery + Redis | RQ | 异步分析任务 |
| 缓存 | Redis | 本地JSON文件 | LLM响应缓存 |
| 日志 | structlog + loguru | logging | 结构化日志 |
| 测试 | pytest | unittest |  fixtures和参数化 |
| 部署 | Docker | systemd | 容器化便于扩展 |

## 附录C: 风险与缓解策略

| 风险 | 影响 | 缓解策略 |
|------|------|----------|
| VLM API延迟高 | 分析时间长 | 异步任务队列；结果缓存；并行调用 |
| VLM API成本高 | 运营成本高 | Token预算管理；智能采样减少帧数；本地缓存 |
| VLM幻觉/误判 | 事件漏报/误报 | 多步骤验证；CV数据交叉确认；置信度阈值过滤 |
| 配置错误导致逻辑链失败 | 系统不稳定 | 配置校验；步骤级错误捕获；降级策略 |
| 长视频超出上下文限制 | 分析失败 | 分段处理；摘要级联；关键帧压缩 |
| 网络/API不可用 | 服务中断 | 重试机制；熔断降级；离线队列 |

---

*文档结束*
