/**
 * spawn_subagent:派生子代理处理「需要完整载入视频上下文 + 重度推理,
 * 但父代理只需要结论」的任务(只读参考 vendor/kimi-code agent-core-v2 的
 * task/agent 工具;按本运行时简化:无 DI、同步等待子 loop 结论)。
 *
 * 语义:
 * - 子 loop:同 provider / model / systemPrompt;工具集 = 父 registry 去掉
 *   spawn_subagent(禁递归);maxStepsPerTurn = 12;gate 复用父级(权限裁决
 *   语义一致,manual 模式下子代理的审批同样冒泡到父级审批桥)。
 * - 并发:模块级信号量,最多 4 个子代理并行,第 5 个起排队等待。
 * - 事件:子 loop 事件经 ctx.onSubagentEvent 逐个转发给父 loop(过滤
 *   context_usage / compaction,父级自己的上下文统计不被污染)。
 * - 视频:video_path 经沙盒校验后在 execute 时读文件转成 video_url
 *   dataURL ContentPart,随子代理首条 user 消息直传模型。
 * - 结果:completed → 最后一条 assistant 文本;stop_turn(submit_detection)
 *   → 从 stopResult.note 提取编码与结论;max_steps / error / cancelled →
 *   说明原因。note 统一为 JSON {reason, steps} 供调试。
 */
import { readFile } from 'node:fs/promises';
import path from 'node:path';

import { z } from 'zod';

import { extractText, type ContentPart, type Message } from '../../kosong/message';
import type { ChatProvider } from '../../kosong/provider';
import { runAgentLoop, type AgentLoopResult } from '../../loop/agentLoop';
import type { PermissionGate } from '../../permissions/gate';
import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolContext,
  type ExecutableToolErrorResult,
  type ExecutableToolResult,
} from '../contract';
import { ToolRegistry } from '../registry';
import { resolveWorkspacePath } from './fileTools';
import type { ToolserverClient } from './httpToolserver';
import { invalidInputResult, toolserverErrorResult } from './utils';

export const SPAWN_SUBAGENT_TOOL_NAME = 'spawn_subagent';
/** 子代理执行超时:600s(经 RunnableToolExecution.timeoutMs 生效)。 */
export const SUBAGENT_TIMEOUT_MS = 600_000;
/** 子代理单 turn 步数上限。 */
export const SUBAGENT_MAX_STEPS = 12;
/** 全局(进程级)子代理并发上限。 */
export const MAX_CONCURRENT_SUBAGENTS = 4;

// ---------------------------------------------------------------------------
// 模块级并发信号量:最多 MAX_CONCURRENT_SUBAGENTS 个子代理并行,其余排队
// ---------------------------------------------------------------------------

let activeSubagents = 0;
const waiters: Array<() => void> = [];

function acquireSubagentSlot(signal: AbortSignal): Promise<void> {
  if (activeSubagents < MAX_CONCURRENT_SUBAGENTS) {
    activeSubagents += 1;
    return Promise.resolve();
  }
  return new Promise<void>((resolve, reject) => {
    const grant = (): void => {
      activeSubagents += 1;
      resolve();
    };
    const onAbort = (): void => {
      const index = waiters.indexOf(grant);
      if (index >= 0) waiters.splice(index, 1);
      reject(new Error('spawn_subagent cancelled while waiting for a concurrency slot'));
    };
    waiters.push(grant);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function releaseSubagentSlot(): void {
  activeSubagents -= 1;
  waiters.shift()?.();
}

// ---------------------------------------------------------------------------
// 工具组装
// ---------------------------------------------------------------------------

export interface SpawnSubagentProviderHandle {
  readonly provider: ChatProvider;
  readonly model: string;
}

export interface SpawnSubagentDeps {
  /** 父级工具注册表;子 registry = 其副本去掉 spawn_subagent。 */
  readonly parentRegistry: ToolRegistry;
  /** 视频路径沙盒校验用(workspace + additionalDirs)。 */
  readonly workspace: WorkspaceConfig;
  /** 惰性取 provider(与父 loop 同实例/模型)。 */
  readonly providerFactory: () => SpawnSubagentProviderHandle;
  /** 复用父级权限门。 */
  readonly gate: PermissionGate;
  /** 子代理 system prompt(默认与父级一致)。 */
  readonly systemPrompt: string;
  /**
   * 可选 toolserver 客户端:提供时 video_path 走 /tools/prepare_video
   * (与 load_video 一致:超限降帧/转码,硬上限 50MB);缺省直接读原文件
   * (测试/无 toolserver 场景)。
   */
  readonly toolserverClient?: ToolserverClient;
  /** 子 loop 压缩上下文窗口;缺省不给子 loop 配压缩。 */
  readonly contextTokens?: number;
  readonly maxSteps?: number;
}

const inputSchema = z.strictObject({
  task: z.string().min(1),
  video_path: z.string().min(1).optional(),
});

const VIDEO_MIME_BY_EXT: Record<string, string> = {
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.mov': 'video/quicktime',
  '.webm': 'video/webm',
  '.avi': 'video/x-msvideo',
  '.mkv': 'video/x-matroska',
};

function videoMimeType(videoPath: string): string {
  return VIDEO_MIME_BY_EXT[path.extname(videoPath).toLowerCase()] ?? 'video/mp4';
}

/** prepare_video 响应(与 load_video 共用 toolserver 契约)。 */
interface PrepareVideoResponse {
  path: string;
  size_bytes: number;
  transcoded: boolean;
}

/** 预处理后的硬上限:base64 在线传输膨胀约 33%,与 load_video 一致。 */
const PREPARED_HARD_MAX_BYTES = 50 * 1024 * 1024;
const PREPARED_READ_TIMEOUT_MS = 60_000;

/**
 * 视频 ContentPart:有 toolserver 时先 prepare_video(降帧/转码,输出恒为
 * mp4),否则直接读原文件按扩展名猜 mime。失败返回 isError 结果。
 */
async function buildVideoPart(
  deps: SpawnSubagentDeps,
  videoPath: string,
): Promise<ContentPart | ExecutableToolErrorResult> {
  let filePath = videoPath;
  let mime = videoMimeType(videoPath);
  if (deps.toolserverClient !== undefined) {
    const prepared = await deps.toolserverClient.post<PrepareVideoResponse>(
      '/tools/prepare_video',
      { video_path: videoPath, max_mb: 40 },
    );
    if (!prepared.ok) return toolserverErrorResult(prepared.error);
    if (prepared.data.size_bytes > PREPARED_HARD_MAX_BYTES) {
      return {
        output:
          `视频经降帧/转码后仍有 ${(prepared.data.size_bytes / (1024 * 1024)).toFixed(1)}MB,` +
          '超过 50MB 上限,无法直传给子代理;请在 task 中让子代理改用 extract_frames 抽帧分析。',
        isError: true,
      };
    }
    filePath = prepared.data.path;
    mime = 'video/mp4';
  }
  try {
    const buffer = await readFile(filePath, {
      signal: AbortSignal.timeout(PREPARED_READ_TIMEOUT_MS),
    });
    return {
      type: 'video_url',
      videoUrl: { url: `data:${mime};base64,${buffer.toString('base64')}` },
    };
  } catch (error) {
    return {
      output: `读取视频文件失败(${filePath}): ${error instanceof Error ? error.message : String(error)}`,
      isError: true,
    };
  }
}

/** 子代理首条 user 消息:task 文本 + (可选)视频 dataURL 直传。 */
async function buildChildMessages(
  deps: SpawnSubagentDeps,
  task: string,
  videoPath: string | undefined,
): Promise<Message[] | ExecutableToolErrorResult> {
  const text =
    videoPath === undefined ? task : `${task}\n\n(视频文件:${videoPath},已随本条消息直接附上)`;
  const content: ContentPart[] = [{ type: 'text', text }];
  if (videoPath !== undefined) {
    const part = await buildVideoPart(deps, videoPath);
    if (!('type' in part)) return part;
    content.push(part);
  }
  return [{ role: 'user', content, toolCalls: [] }];
}

function lastAssistantText(result: AgentLoopResult): string {
  for (let i = result.messages.length - 1; i >= 0; i -= 1) {
    const message = result.messages[i];
    if (message?.role === 'assistant') {
      const text = extractText(message).trim();
      if (text !== '') return text;
    }
  }
  return '';
}

/** stop_turn(submit_detection):从 stopResult.note(JSON)提取编码与结论。 */
function describeStopResult(result: AgentLoopResult): string {
  const raw = result.stopResult?.note;
  if (raw === undefined) return '子代理已提交结构化结果(stop_turn)。';
  try {
    const data = JSON.parse(raw) as Record<string, unknown>;
    const parts: string[] = [];
    if (typeof data['binary_encoding'] === 'string') {
      parts.push(`编码 ${data['binary_encoding']}`);
    }
    if (typeof data['normal'] === 'boolean') {
      parts.push(data['normal'] ? '判定正常' : '判定异常');
    }
    const report = data['report_markdown'];
    if (typeof report === 'string' && report.trim() !== '') {
      const excerpt = report.trim().slice(0, 500);
      parts.push(`结论:${excerpt}${report.trim().length > 500 ? '…' : ''}`);
    }
    return (
      '子代理已通过 submit_detection 提交结构化检测结果。' +
      (parts.length > 0 ? parts.join(';') : `原始 note:${raw.slice(0, 500)}`)
    );
  } catch {
    return `子代理已提交结构化结果(stop_turn),note:${raw.slice(0, 500)}`;
  }
}

function toToolResult(result: AgentLoopResult): ExecutableToolResult {
  const note = JSON.stringify({ reason: result.reason, steps: result.steps });
  switch (result.reason) {
    case 'completed': {
      const conclusion = lastAssistantText(result);
      return {
        output: conclusion === '' ? '子代理已完成,但未产生文本结论。' : conclusion,
        note,
      };
    }
    case 'stop_turn':
      return { output: describeStopResult(result), note };
    case 'max_steps': {
      const partial = lastAssistantText(result);
      return {
        output:
          `子代理达到步数上限(${result.steps} 步),未能收敛出最终结论。` +
          (partial === '' ? '' : `最后的部分输出:${partial}`),
        note,
      };
    }
    case 'cancelled':
      return { output: '子代理执行被取消(父级中断)。', isError: true, note };
    case 'error':
      return {
        output: `子代理执行失败:${result.error ?? '未知错误'}`,
        isError: true,
        note,
      };
  }
}

async function runSubagent(
  deps: SpawnSubagentDeps,
  task: string,
  videoPath: string | undefined,
  ctx: ExecutableToolContext,
): Promise<ExecutableToolResult> {
  const { provider, model } = deps.providerFactory();

  // 禁递归:子 registry 复制父级工具,去掉 spawn_subagent。
  const childRegistry = new ToolRegistry();
  for (const tool of deps.parentRegistry.list()) {
    if (tool.name !== SPAWN_SUBAGENT_TOOL_NAME) childRegistry.register(tool);
  }

  const messages = await buildChildMessages(deps, task, videoPath);
  if (!Array.isArray(messages)) return messages;

  const result = await runAgentLoop({
    provider,
    model,
    systemPrompt: deps.systemPrompt,
    registry: childRegistry,
    gate: deps.gate,
    messages,
    maxStepsPerTurn: deps.maxSteps ?? SUBAGENT_MAX_STEPS,
    ...(deps.contextTokens !== undefined
      ? { compaction: { maxContextTokens: deps.contextTokens } }
      : {}),
    signal: ctx.signal,
    onEvent: async (event) => {
      // 子 loop 的上下文统计/压缩事件不转发(父级有自己的 context_usage)。
      if (event.type === 'context_usage' || event.type === 'compaction') return;
      await ctx.onSubagentEvent?.(event);
    },
  });
  return toToolResult(result);
}

export function createSpawnSubagentTool(deps: SpawnSubagentDeps): ExecutableTool {
  return {
    name: SPAWN_SUBAGENT_TOOL_NAME,
    description:
      '派生一个子代理执行需要完整载入视频上下文的重度分析任务,等待并返回其最终结论。' +
      '子代理拥有与本代理相同的工具(但不能再次派生子代理),步数上限 12,' +
      '最多 4 个子代理并行,超出的排队。适用:整段视频的独立深度研判;' +
      '不适用:只需抽几帧即可回答的轻量问题(直接用 video 工具更快)。',
    parameters: {
      type: 'object',
      properties: {
        task: {
          type: 'string',
          description:
            '给子代理的完整任务描述(自包含:背景、目标、输出要求),子代理看不到父级对话历史。',
        },
        video_path: {
          type: 'string',
          description: '可选。视频文件路径(沙盒工作区内);提供时整段视频直传给子代理。',
        },
      },
      required: ['task'],
    },
    resolveExecution(rawInput: unknown) {
      const parsed = inputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult(SPAWN_SUBAGENT_TOOL_NAME, parsed.error);
      const input = parsed.data;

      let videoPath: string | undefined;
      if (input.video_path !== undefined) {
        const resolved = resolveWorkspacePath(input.video_path, deps.workspace, 'read');
        if (!resolved.ok) return resolved.result;
        videoPath = resolved.path;
      }

      return {
        accesses:
          videoPath === undefined ? ToolAccesses.none() : ToolAccesses.readFile(videoPath),
        approvalRule: `spawn_subagent(${input.task.slice(0, 50)})`,
        timeoutMs: SUBAGENT_TIMEOUT_MS,
        execute: async (ctx: ExecutableToolContext): Promise<ExecutableToolResult> => {
          await acquireSubagentSlot(ctx.signal);
          try {
            return await runSubagent(deps, input.task, videoPath, ctx);
          } finally {
            releaseSubagentSlot();
          }
        },
      };
    },
  };
}
