/** 统一对话(agent 运行时 /api/agent/*)的前端契约类型:权限模式、会话信息、
 * 时间线条目(user/assistant/tool/approval/detection/system)与发送选项。
 * 自 stores/agentchat.ts 拆出(纯类型,无运行时);store 模块 re-export
 * 保持既有引用面不变。后端契约详见 stores/agentchat.ts 模块头。 */
/** 权限模式三档:逐条确认 manual / 自动通过 auto / 完全自主 yolo。 */
export type AgentMode = 'manual' | 'auto' | 'yolo'

/** idle 待开始 / connecting 连接中(建会话、切会话或流发起)/ running 运行中 /
 * awaiting_approval 等待审批 / done 已完成 / failed 失败(可重试)。 */
export type AgentStatus =
  | 'idle'
  | 'connecting'
  | 'running'
  | 'awaiting_approval'
  | 'done'
  | 'failed'

/** approval_request 事件里的资源访问声明(与 agent 端 ToolAccesses 对齐)。 */
export interface AgentAccess {
  kind: 'file' | 'all'
  operation?: 'read' | 'write' | 'readwrite' | 'search'
  path?: string
  recursive?: boolean
}

/** 审批内容预览(与 agent 端 PreviewContent 对齐)。 */
export interface AgentPreviewContent {
  language: string
  content: string
  truncated: boolean
}

/** GET /sessions 的列表项(createdAt/lastActiveAt 为 epoch ms)。 */
export interface AgentSessionInfo {
  id: string
  workspaceDir?: string
  mode?: AgentMode
  title?: string
  createdAt?: number
  lastActiveAt?: number
  /** 最近一次已知上下文占用(token,有真实 usage 才返回)。 */
  usedTokens?: number
}

export interface AgentUserEntry {
  /** 前端条目 id(进入时间线即赋,全局唯一):v-for key/折叠态/撤回定位都用它。 */
  id: string
  kind: 'user'
  text: string
  /** 后端落盘序号(1 起,seq-1=后端持久化下标):历史加载按数组位置赋;
   * 乐观 steer 在 SSE/轮询确认落盘时绑定;正常发送按会话落盘水位推算。
   * 撤回按它直送后端 entryIndex(后端契约不变,前端不再做下标换算)。 */
  seq?: number
  videoPath?: string
  /** 上传视频的预览地址(/api/agent/uploads/{name});仅本次会话内有效,
   * 历史重载由 videoPath 确定性推 /api/workspace/stream(见 utils/chatDisplay)。 */
  videoSrc?: string
  /** dataURL 图片附件(发送时随消息上传,历史原样返回)。 */
  images?: string[]
  /** 发送时间(epoch ms),用于气泡 HH:MM。 */
  at?: number
  /** 进行中插话(steer)标记:仅本地流式期间存在,历史重载不恢复。 */
  steered?: boolean
}

export interface AgentAssistantEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'assistant'
  text: string
  think: string
  /** 该条对应 generate 步的 LLM 调用壁钟耗时(ms;实时由 generate_done SSE 写入,
   * 历史由后端落盘的 assistant 条目恢复;旧数据无此字段)。 */
  generateMs?: number
  /** 气泡创建时间(epoch ms)。 */
  at?: number
}

/** spawn_subagent 工具条目下挂的子代理迷你时间线条目(subagent_event 流式期间聚合,
 * 不落盘不恢复):think=think_delta 聚合的思考;text=text_delta 聚合的子结论文本;
 * tool=子代理工具调用(tool_call_start 压入,tool_result 按 id 置 done)。 */
export type AgentSubItem =
  | { kind: 'think'; text: string }
  | { kind: 'text'; text: string }
  | { kind: 'tool'; id: string; name: string; args: string; done: boolean }

export interface AgentToolEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'tool'
  /** tool_call_start 的 call.id,tool_result 按它回填(区别于前端条目 id)。 */
  callId: string
  name: string
  /** 创建时刻(epoch ms):流式本地打点/历史取落盘 at。分析链路流程图耗时推导用。 */
  at?: number
  /** call.arguments 原文(JSON 字符串)。 */
  args: string
  result: string
  /** 工具输出中的图片(展示地址:dataURL 或 /api/agent 代理的 media 引用 URL;
   * extract_frames/draw_boxes 等返回的 image ContentPart)。 */
  images: string[]
  /** 工具输出含 video part(load_video 整段视频直传);只显示静态提示,不做播放器。 */
  hasVideo: boolean
  /** 子代理迷你时间线(subagent_event 按 toolCallId 挂到本条目)。 */
  children: AgentSubItem[]
  isError: boolean
  done: boolean
}

export interface AgentApprovalEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'approval'
  requestId: string
  toolName: string
  approvalRule: string
  description?: string
  accesses: AgentAccess[]
  /** 创建时刻(epoch ms),见 AgentToolEntry.at。 */
  at?: number
  /** 工具调用参数的内容预览。 */
  preview?: AgentPreviewContent
  /** 已回执后记录:'approved' | 'rejected' | 'approved_session' | 'cancelled'(历史)。 */
  decision?: string
  /** 历史载入的未决审批:后端不再接受回执,UI 显示「已失效」,不渲染按钮。 */
  stale?: boolean
}

/** detection 事件的 data:submit_detection 提交的结构化检测载荷(以 agent 端
 * submitDetection.ts 的 zod schema 为权威对齐);仅迁移前的旧落盘条目可能是
 * 未解析成功的字符串,ChatView 按 detection-raw 降级渲染。 */
export interface DetectionPayload {
  video_path?: string
  binary_encoding?: string
  normal?: boolean
  events?: Array<{
    event_id: number
    detected: boolean
    confidence?: number
    instances?: Array<{
      description: string
      location: string
      start_sec: number
      end_sec: number
    }>
    reasoning: string
    /** 证据帧时间点(秒)。 */
    evidence_frames: number[]
    /** 可选定位框(归一化 xyxy)及其所属帧;用于服务端渲染标注图。 */
    boxes?: Array<{ x1: number; y1: number; x2: number; y2: number; label?: string }>
    box_frame?: number
    /** 逐事件标注图(submit_detection 服务端生成;展示地址为 dataURL(旧条目)
     * 或 /api/agent 代理的 media 引用 URL(新条目,store 解析后);无框/画框失败时缺省)。 */
    annotated_image?: string
  }>
  /** 标注降级元信息(两级):missing_boxes=检出但未给 boxes/box_frame;annotation_failed=画框失败。 */
  meta?: {
    missing_boxes?: number[]
    annotation_failed?: number[]
  }
  report_markdown?: string
}

export interface AgentDetectionEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'detection'
  data: unknown
  /** 创建时刻(epoch ms),见 AgentToolEntry.at。 */
  at?: number
}

/** 系统提示条目(如自动压缩提示、输出截断警示),不进历史,仅流式期间插入时间线。
 * tone:'warn' 用警示色系(gold)渲染,缺省为中性灰。 */
export interface AgentSystemEntry {
  /** 前端条目 id(见 AgentUserEntry.id)。 */
  id: string
  kind: 'system'
  text: string
  tone?: 'warn'
  /** 创建时刻(epoch ms),见 AgentToolEntry.at。 */
  at?: number
}

export type AgentEntry =
  | AgentUserEntry
  | AgentAssistantEntry
  | AgentToolEntry
  | AgentApprovalEntry
  | AgentDetectionEntry
  | AgentSystemEntry

/** 待发送的视频附件(同一时刻最多一个):
 * 上传得来:path=uploads 返回路径,src=/api/agent/uploads/{name} 供预览;
 * 工作区树「送入对话」:path=工作区相对路径,无 src(composer 显示图标块,
 * 气泡由 path 确定性推 /api/workspace/stream 小播放器预览)。 */
export interface PendingVideo {
  path: string
  name: string
  src?: string
}

/** send 的附件参数(图片 dataURL ≤4;视频二选一:工作区路径或上传后路径)。 */
export interface SendOptions {
  videoPath?: string
  videoSrc?: string
  images?: string[]
}

/** 非 2xx 统一成 ApiError:agent 服务错误为 {error:{code,message}},
 * FastAPI 代理/其他后端为 {detail},两种都认。 */
