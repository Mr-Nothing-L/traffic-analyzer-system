/**
 * 生成物,禁止手工编辑(AGENTS.md「生成物是下游契约,不是手改文档」)。
 * 来源:scripts/gen_frontend_contracts.py ← traffic_analyzer/web/evidence_schema.py
 * 修改后端 schema 后运行 `python3 scripts/gen_frontend_contracts.py` 重新生成;
 * 同步由 traffic_analyzer/tests/test_frontend_contracts.py 守护(漂移即 fail)。
 */

/**
 * 关键帧条目:文件位于 analysis/<stem>/关键帧/ 下,由 web/keyframes.py
即时维护(列表位置即时间顺序);文本编辑 PUT 不改写它(sft_api 落盘时
无条件沿用磁盘值)。
 */
export interface SftKeyframe {
  filename: string
  frame_index: number
  time_sec: number
}

/**
 * 完整 SFT 样本;chunk/idx/时间戳/chunk_name 与磁盘版本不一致时拒绝。
 */
export interface SftSample {
  chunk: unknown
  idx: unknown
  action: number[]
  description: string
  start_timestamp: unknown
  end_timestamp: unknown
  chunk_name: unknown
  event_attributes?: Record<string, Record<string, unknown>> | null
  attr_mentions?: Record<string, Record<string, unknown>> | null
  keyframes?: SftKeyframe[]
  base_sig?: string | null
  last_edited_by?: string | null
  last_edited_at?: string | null
}
