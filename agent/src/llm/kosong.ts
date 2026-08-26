/**
 * 业务侧 kosong(LLM 抽象层)统一门面。
 *
 * 所有业务代码(含测试)从本模块导入 kosong 符号,不直接引用 kosong/
 * 内部路径或 #/* 子路径;kosong/ 目录内部保持 vendored 不变。
 */

// Message types and helpers
export {
  createAssistantMessage,
  createToolMessage,
  createUserMessage,
  extractText,
} from '#/message';
export type {
  ContentPart,
  Message,
  StreamedMessagePart,
  ToolCall,
  VideoURLPart,
} from '#/message';

// Provider interfaces
export type {
  ChatProvider,
  FinishReason,
  GenerateOptions,
  StreamedMessage,
  ThinkingEffort,
} from '#/provider';

// OpenAI-compatible provider adapter(TS agent 当前唯一接入点)
export { OpenAILegacyChatProvider } from '#/providers/openai-legacy';

// Tool wire schema
export type { Tool } from '#/tool';

// Token usage
export type { TokenUsage } from '#/usage';

// Core generation function
export { generate } from '#/generate';

// Errors
export { isAbortError } from '#/errors';
