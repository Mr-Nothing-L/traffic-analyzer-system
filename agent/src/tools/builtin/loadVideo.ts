/**
 * load_video: load an entire video into the model context as a single
 * `video_url` content part (data:video/mp4;base64).
 *
 * Flow: sandbox-validate `video_path` (read) → POST /tools/prepare_video on
 * the Python toolserver (transcodes / downsamples when over `max_mb`; the
 * 40MB default lives in toolserver/server.py — TS 只透传,未提供时不发送) →
 * sandbox-validate the returned prepared path (same strict
 * workspace resolver) → read the prepared file back and inline it as a base64
 * data URL. If the prepared file still exceeds 50MB the base64 payload would
 * be too large for the chat request, so the call fails with an isError result
 * telling the model to inspect key moments with draw_boxes / extract_frames
 * instead.
 */
import { readFile } from 'node:fs/promises';

import { z } from 'zod';

import type { ContentPart } from '../../kosong/message';
import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolResult,
} from '../contract';
import type { ToolserverClient } from './httpToolserver';
import { resolveWorkspacePath } from './fileTools';
import type { ToolsetEntrySpec } from './videoTools';
import {
  invalidInputResult,
  toolserverErrorResult,
} from './utils';

/**
 * Hard ceiling on the prepared file; base64 inflates it ~33% on the wire.
 * 该上限唯一执法点在 TS 侧(agent 聊天请求的体积约束,toolserver 无对应
 * 检查;load_video 与 spawn_subagent 视频直传共用本常量);模型可见文案
 * (40MB 默认/50MB 拒绝)以 toolset.json 的 limits 为准。
 */
export const PREPARED_VIDEO_HARD_MAX_BYTES = 50 * 1024 * 1024;
const READ_TIMEOUT_MS = 60_000;

interface PrepareVideoResponse {
  path: string;
  size_bytes: number;
  fps: number;
  duration_s: number | null;
  transcoded: boolean;
}

const loadVideoInputSchema = z.strictObject({
  video_path: z.string(),
  max_mb: z.number().positive().optional(),
});

export function createLoadVideoTool(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'load_video',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = loadVideoInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('load_video', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `load_video(${videoPath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          // max_mb 未提供时不发送,由 toolserver 取默认值(server.py 单一权威)。
          const body: Record<string, unknown> = { video_path: videoPath };
          if (parsed.data.max_mb !== undefined) body['max_mb'] = parsed.data.max_mb;
          const result = await client.post<PrepareVideoResponse>('/tools/prepare_video', body);
          if (!result.ok) return toolserverErrorResult(result.error);
          const prepared = result.data;

          // Defense in depth: the toolserver resolves paths against its own
          // allowed roots, but before node:fs touches the returned path it must
          // also pass the same strict workspace resolver (transcoded artifacts
          // land under <allowed root>/.agent/transcoded/, i.e. inside the
          // workspace; anything else is rejected instead of read).
          const preparedResolved = resolveWorkspacePath(prepared.path, workspace, 'read');
          if (!preparedResolved.ok) return preparedResolved.result;

          if (prepared.size_bytes > PREPARED_VIDEO_HARD_MAX_BYTES) {
            const sizeMb = (prepared.size_bytes / (1024 * 1024)).toFixed(1);
            return {
              output:
                `视频经降帧/转码后仍有 ${sizeMb}MB,超过 ${PREPARED_VIDEO_HARD_MAX_BYTES / (1024 * 1024)}MB 上限,` +
                '无法整段加载。请改用 extract_frames(fps=1 全片采样,或对关键时刻用 timestamps)分析,必要时用 draw_boxes 逐帧核对。',
              isError: true,
            };
          }

          let bytes: Buffer;
          try {
            bytes = await readFile(prepared.path, {
              signal: AbortSignal.timeout(READ_TIMEOUT_MS),
            });
          } catch (error) {
            const reason = error instanceof Error ? error.message : String(error);
            return {
              output: `读取预处理后的视频文件失败(${prepared.path}): ${reason}`,
              isError: true,
            };
          }

          const sizeMb = (prepared.size_bytes / (1024 * 1024)).toFixed(1);
          const duration =
            prepared.duration_s === null ? '未知' : `${prepared.duration_s}s`;
          const transcodedNote = prepared.transcoded
            ? '已降帧/转码(超出体积上限)'
            : '未转码(原始内容)';
          const parts: ContentPart[] = [
            {
              type: 'text',
              text:
                `已加载完整视频:时长 ${duration},fps ${prepared.fps},` +
                `大小 ${sizeMb}MB,${transcodedNote}。视频内容如下:`,
            },
            {
              type: 'video_url',
              videoUrl: { url: `data:video/mp4;base64,${bytes.toString('base64')}` },
            },
          ];
          return { output: parts };
        },
      };
    },
  };
}
