/**
 * load_video: load an entire video into the model context as a single
 * `video_url` content part (data:video/mp4;base64).
 *
 * Flow: sandbox-validate `video_path` (read) → POST /tools/prepare_video on
 * the Python toolserver (transcodes / downsamples when over `max_mb`,
 * default 40) → read the prepared file back and inline it as a base64 data
 * URL. If the prepared file still exceeds 50MB the base64 payload would be
 * too large for the chat request, so the call fails with an isError result
 * telling the model to inspect key moments with draw_boxes instead
 * (extract_frames 已下线,不再作为回退建议)。
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
import {
  invalidInputResult,
  toolserverErrorResult,
} from './utils';

const DEFAULT_MAX_MB = 40;
/** Hard ceiling on the prepared file; base64 inflates it ~33% on the wire. */
const HARD_MAX_BYTES = 50 * 1024 * 1024;
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
  description: string,
): ExecutableTool {
  return {
    name: 'load_video',
    description,
    parameters: {
      type: 'object',
      properties: {
        video_path: { type: 'string', description: '视频文件路径(沙盒工作区内)' },
        max_mb: {
          type: 'number',
          exclusiveMinimum: 0,
          default: DEFAULT_MAX_MB,
          description: `超过该大小(MB)时由服务端降帧/转码,默认 ${DEFAULT_MAX_MB}`,
        },
      },
      required: ['video_path'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = loadVideoInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('load_video', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      const maxMb = parsed.data.max_mb ?? DEFAULT_MAX_MB;
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `load_video(${videoPath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const result = await client.post<PrepareVideoResponse>('/tools/prepare_video', {
            video_path: videoPath,
            max_mb: maxMb,
          });
          if (!result.ok) return toolserverErrorResult(result.error);
          const prepared = result.data;

          if (prepared.size_bytes > HARD_MAX_BYTES) {
            const sizeMb = (prepared.size_bytes / (1024 * 1024)).toFixed(1);
            return {
              output:
                `视频经降帧/转码后仍有 ${sizeMb}MB,超过 ${HARD_MAX_BYTES / (1024 * 1024)}MB 上限,` +
                '无法整段加载。请改用 draw_boxes 对关键时刻逐帧核对分析。',
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
