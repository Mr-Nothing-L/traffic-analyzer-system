/**
 * Video tools backed by the Python toolserver over HTTP:
 * video_meta / extract_frames / draw_boxes.
 *
 * Parameter schemas mirror traffic_analyzer/toolserver/server.py. Frames and
 * annotated images come back as `jpeg_base64` and are converted to kosong
 * image ContentParts so the model can see them. `video_path` is validated
 * through the workspace sandbox (read operation); a sandbox violation is a
 * hard veto returned as an isError result.
 */
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

const HARD_MAX_FRAMES = 8;
const DEFAULT_MAX_FRAMES = 4;

export type ToolDescriptionLookup = (toolName: string) => string;

interface VideoMetaResponse {
  duration_s: number | null;
  fps: number;
  width: number;
  height: number;
  frame_count: number;
}

interface ExtractedFrame {
  timestamp: number;
  jpeg_base64: string;
  width: number;
  height: number;
}

interface ExtractFramesResponse {
  frames: ExtractedFrame[];
}

interface DrawBoxesResponse {
  jpeg_base64: string;
  width: number;
  height: number;
}

const videoMetaInputSchema = z.strictObject({
  video_path: z.string(),
});

const extractFramesInputSchema = z.strictObject({
  video_path: z.string(),
  timestamps: z.array(z.number().min(0)).optional(),
  count: z.number().int().min(1).optional(),
  max_frames: z.number().int().min(1).optional(),
});

const boxSchema = z.strictObject({
  x1: z.number().min(0).max(1),
  y1: z.number().min(0).max(1),
  x2: z.number().min(0).max(1),
  y2: z.number().min(0).max(1),
  label: z.string().optional(),
});

const drawBoxesInputSchema = z.strictObject({
  video_path: z.string(),
  timestamp: z.number().min(0),
  boxes: z.array(boxSchema).min(1),
});

function jpegImagePart(jpegBase64: string): ContentPart {
  return {
    type: 'image_url',
    imageUrl: { url: `data:image/jpeg;base64,${jpegBase64}` },
  };
}

export function createVideoMetaTool(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  description: string,
): ExecutableTool {
  return {
    name: 'video_meta',
    description,
    parameters: {
      type: 'object',
      properties: {
        video_path: { type: 'string', description: '视频文件路径(沙盒工作区内)' },
      },
      required: ['video_path'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = videoMetaInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('video_meta', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `video_meta(${videoPath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const result = await client.post<VideoMetaResponse>('/tools/video_meta', {
            video_path: videoPath,
          });
          if (!result.ok) return toolserverErrorResult(result.error);
          return { output: JSON.stringify(result.data) };
        },
      };
    },
  };
}

export function createExtractFramesTool(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  description: string,
): ExecutableTool {
  return {
    name: 'extract_frames',
    description,
    parameters: {
      type: 'object',
      properties: {
        video_path: { type: 'string', description: '视频文件路径(沙盒工作区内)' },
        timestamps: {
          type: 'array',
          items: { type: 'number', minimum: 0 },
          description: '指定抽帧时间点(秒);与 count 二选一,优先级更高',
        },
        count: {
          type: 'integer',
          minimum: 1,
          description: '未给 timestamps 时,在整段视频上均匀抽取的帧数',
        },
        max_frames: {
          type: 'integer',
          minimum: 1,
          maximum: HARD_MAX_FRAMES,
          default: DEFAULT_MAX_FRAMES,
          description: `单次最多返回帧数,默认 ${DEFAULT_MAX_FRAMES},上限 ${HARD_MAX_FRAMES}`,
        },
      },
      required: ['video_path'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = extractFramesInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('extract_frames', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      const input = parsed.data;
      const maxFrames = Math.min(
        HARD_MAX_FRAMES,
        Math.max(1, input.max_frames ?? DEFAULT_MAX_FRAMES),
      );
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `extract_frames(${videoPath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const body: Record<string, unknown> = { video_path: videoPath, max_frames: maxFrames };
          if (input.timestamps !== undefined) body['timestamps'] = input.timestamps;
          if (input.count !== undefined) body['count'] = input.count;
          const result = await client.post<ExtractFramesResponse>('/tools/extract_frames', body);
          if (!result.ok) return toolserverErrorResult(result.error);
          const frames = result.data.frames ?? [];
          if (frames.length === 0) {
            return { output: '未能从视频中抽取到任何帧(时间戳可能均不可用)。', isError: true };
          }
          const parts: ContentPart[] = [];
          for (const frame of frames) {
            parts.push({
              type: 'text',
              text: `帧 @ ${frame.timestamp}s (${frame.width}x${frame.height}):`,
            });
            parts.push(jpegImagePart(frame.jpeg_base64));
          }
          return { output: parts };
        },
      };
    },
  };
}

export function createDrawBoxesTool(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  description: string,
): ExecutableTool {
  return {
    name: 'draw_boxes',
    description,
    parameters: {
      type: 'object',
      properties: {
        video_path: { type: 'string', description: '视频文件路径(沙盒工作区内)' },
        timestamp: { type: 'number', minimum: 0, description: '目标帧时间点(秒)' },
        boxes: {
          type: 'array',
          minItems: 1,
          description: '归一化 xyxy 框列表(0~1 浮点),可附标签',
          items: {
            type: 'object',
            properties: {
              x1: { type: 'number', minimum: 0, maximum: 1 },
              y1: { type: 'number', minimum: 0, maximum: 1 },
              x2: { type: 'number', minimum: 0, maximum: 1 },
              y2: { type: 'number', minimum: 0, maximum: 1 },
              label: { type: 'string' },
            },
            required: ['x1', 'y1', 'x2', 'y2'],
            additionalProperties: false,
          },
        },
      },
      required: ['video_path', 'timestamp', 'boxes'],
      additionalProperties: false,
    },
    resolveExecution(rawInput: unknown) {
      const parsed = drawBoxesInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('draw_boxes', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      const input = parsed.data;
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `draw_boxes(${videoPath} @ ${input.timestamp}s)`,
        execute: async (): Promise<ExecutableToolResult> => {
          const result = await client.post<DrawBoxesResponse>('/tools/draw_boxes', {
            video_path: videoPath,
            timestamp: input.timestamp,
            boxes: input.boxes,
          });
          if (!result.ok) return toolserverErrorResult(result.error);
          return {
            output: [
              {
                type: 'text',
                text: `已在 ${input.timestamp}s 帧上绘制 ${input.boxes.length} 个标注框 (${result.data.width}x${result.data.height}):`,
              },
              jpegImagePart(result.data.jpeg_base64),
            ],
          };
        },
      };
    },
  };
}

/** Create all three video tools; descriptions come from agent/config/toolset.json. */
export function createVideoTools(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  describe: ToolDescriptionLookup,
): ExecutableTool[] {
  return [
    createVideoMetaTool(client, workspace, describe('video_meta')),
    createExtractFramesTool(client, workspace, describe('extract_frames')),
    createDrawBoxesTool(client, workspace, describe('draw_boxes')),
  ];
}
