/**
 * 崩溃恢复(简化版,参照 deepseek-harness session/repair.ts):
 * 轮次按步增量落盘后,崩溃可能留下「assistant 带 toolCalls 但工具结果未
 * 全部落盘」的半截轮次——provider 会拒绝 dangling tool calls,历史无法续跑。
 *
 * repairTailMessages 扫描消息尾部:若最后一条带 toolCalls 的 assistant 消息
 * 之后只有 tool 消息(或没有消息)且配对不齐,为缺失的 toolCall 合成
 * isError 工具消息(文本说明中断原因),保证历史 provider-valid。
 * 尾部结构异常(assistant 后混有 user/assistant)不在简化版范围内,原样返回。
 */
import { createToolMessage, type Message } from '#/message';

/** 合成工具消息的文本:告知模型该调用未执行,由其决定是否重试。 */
export const INTERRUPTED_TOOL_MESSAGE = '该工具调用因服务重启中断,未执行';

/**
 * 修复消息尾部悬挂的 tool calls。返回修复后的数组;无需修复或尾部结构
 * 不在简化版范围内时原样返回(=== 输入,调用方按引用/长度判断是否回写)。
 */
export function repairTailMessages(messages: readonly Message[]): readonly Message[] {
  let assistantIndex = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message === undefined) break;
    if (message.role === 'assistant' && message.toolCalls.length > 0) {
      assistantIndex = i;
      break;
    }
    // 尾部是 user 或无 toolCalls 的 assistant:无悬挂,无需修复。
    if (message.role !== 'tool') break;
  }
  if (assistantIndex < 0) return messages;

  const assistant = messages[assistantIndex];
  if (assistant === undefined) return messages;
  const tail = messages.slice(assistantIndex + 1);
  // assistant 之后混有非 tool 消息:历史结构异常,超出简化版范围,不动。
  if (tail.some((message) => message.role !== 'tool')) return messages;

  const answered = new Set(tail.map((message) => message.toolCallId));
  const missing = assistant.toolCalls.filter((call) => !answered.has(call.id));
  if (missing.length === 0) return messages;

  const repaired = [...messages];
  for (const call of missing) {
    repaired.push(createToolMessage(call.id, INTERRUPTED_TOOL_MESSAGE));
  }
  return repaired;
}
