/**
 * Video tools backed by the Python toolserver over HTTP:
 * video_meta / extract_frames / draw_boxes.
 *
 * 模型可见的 description 与 parameters 均来自 agent/config/toolset.json(经
 * ToolsetLookup 注入);帧数上限的唯一执法者是 toolserver
 * (traffic_analyzer/toolserver/server.py 的 _DEFAULT/_HARD/_FPS_MODE 上限),
 * TS 侧不做 clamp,请求参数原样透传,toolserver 的 truncated 标志转成
 * 模型可见的截断提示。输入的 zod 运行时校验(与模型可见 schema 职责不同)
 * 保留在本文件。Frames and annotated images come back as `jpeg_base64` and
 * are converted to kosong image ContentParts so the model can see them.
 * `video_path` is validated through the workspace sandbox (read operation);
 * a sandbox violation is a hard veto returned as an isError result.
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

/** toolset.json 条目中工具消费的部分:description + 模型可见 parameters。 */
export interface ToolsetEntrySpec {
  readonly description: string;
  readonly parameters: Record<string, unknown>;
}

/** 按工具名取 toolset.json 的 description 与 parameters(缺条目由实现方 fail-fast)。 */
export type ToolsetLookup = (toolName: string) => ToolsetEntrySpec;

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
  /** true 时说明请求模式下的帧数超过上限,响应被截断。 */
  truncated?: boolean;
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
  fps: z.number().min(0.2).max(5).optional(),
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
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'video_meta',
    description: spec.description,
    parameters: spec.parameters,
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
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'extract_frames',
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = extractFramesInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('extract_frames', parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      const input = parsed.data;
      // 帧数上限的 clamp 只在 toolserver 执行;TS 侧透传请求参数,
      // 未提供的字段不发(toolserver 按模式取默认值)。
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `extract_frames(${videoPath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const body: Record<string, unknown> = { video_path: videoPath };
          if (input.timestamps !== undefined) body['timestamps'] = input.timestamps;
          if (input.fps !== undefined) body['fps'] = input.fps;
          if (input.count !== undefined) body['count'] = input.count;
          if (input.max_frames !== undefined) body['max_frames'] = input.max_frames;
          const result = await client.post<ExtractFramesResponse>('/tools/extract_frames', body);
          if (!result.ok) return toolserverErrorResult(result.error);
          const frames = result.data.frames ?? [];
          if (frames.length === 0) {
            return { output: '未能从视频中抽取到任何帧(时间戳可能均不可用)。', isError: true };
          }
          const parts: ContentPart[] = [];
          if (result.data.truncated) {
            parts.push({
              type: 'text',
              text: '注意:请求帧数超过本次调用上限,已按上限截断(只覆盖视频前段);剩余时段请用 timestamps 对未覆盖时刻补抽。',
            });
          }
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
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: 'draw_boxes',
    description: spec.description,
    parameters: spec.parameters,
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

/** Create all three video tools; description/parameters come from agent/config/toolset.json. */
export function createVideoTools(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  lookup: ToolsetLookup,
): ExecutableTool[] {
  return [
    createVideoMetaTool(client, workspace, lookup('video_meta')),
    createExtractFramesTool(client, workspace, lookup('extract_frames')),
    createDrawBoxesTool(client, workspace, lookup('draw_boxes')),
  ];
}
