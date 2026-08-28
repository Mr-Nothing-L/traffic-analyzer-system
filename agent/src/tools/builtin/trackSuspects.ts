/**
 * track_suspects: dynamic-event trajectory forensics backed by the Python
 * toolserver (POST /tools/track_suspects).
 *
 * Flow: sandbox-validate `video_path` (read) → POST the resolved path plus the
 * suspect anchors to the toolserver → map the response into ContentParts:
 * per-track numeric profile summary text + trajectory overlay image + each
 * track's best-frame crops + artifacts paths text. A business failure
 * (`failed: true`, e.g. no track could be recovered from the anchors) is NOT a
 * tool error: it returns a normal result whose text tells the model to fall
 * back to pure visual judgement instead.
 *
 * 耗时工具:一次调用约 10-25 次 VLM 调用、耗时可达数分钟,故显式声明
 * timeoutMs(900s),不受 loop 级 120s 截断影响(同 spawn_subagent 的做法)。
 */
import { z } from 'zod';

import type { ContentPart } from '../../llm/kosong';
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

export const TRACK_SUSPECTS_TOOL_NAME = 'track_suspects';
/** 单次执行超时:900s(经 RunnableToolExecution.timeoutMs 生效)。 */
export const TRACK_SUSPECTS_TIMEOUT_MS = 900_000;

interface TrackBestFrame {
  timestamp: number;
  jpeg_base64: string;
}

interface SuspectTrack {
  id: number | string;
  description: string;
  profile: string;
  side_hint: string;
  direction_verdict: string;
  best_frames: TrackBestFrame[];
}

interface TrackSuspectsResponse {
  tracks?: SuspectTrack[];
  annotated_image?: string;
  artifacts?: { dir: string; clip: string; csv: string };
  failed?: boolean;
  failure_reason?: string | null;
}

const suspectSchema = z.strictObject({
  box: z.strictObject({
    x1: z.number().min(0).max(1),
    y1: z.number().min(0).max(1),
    x2: z.number().min(0).max(1),
    y2: z.number().min(0).max(1),
  }),
  timestamp: z.number().min(0),
  description: z.string(),
});

const trackSuspectsInputSchema = z.strictObject({
  video_path: z.string(),
  suspects: z.array(suspectSchema).min(1).max(5),
  time_range: z.tuple([z.number().min(0), z.number().min(0)]).optional(),
});

function jpegImagePart(jpegBase64: string): ContentPart {
  return {
    type: 'image_url',
    imageUrl: { url: `data:image/jpeg;base64,${jpegBase64}` },
  };
}

export function createTrackSuspectsTool(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  spec: ToolsetEntrySpec,
): ExecutableTool {
  return {
    name: TRACK_SUSPECTS_TOOL_NAME,
    description: spec.description,
    parameters: spec.parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = trackSuspectsInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult(TRACK_SUSPECTS_TOOL_NAME, parsed.error);
      const resolved = resolveWorkspacePath(parsed.data.video_path, workspace, 'read');
      if (!resolved.ok) return resolved.result;
      const videoPath = resolved.path;
      const input = parsed.data;
      return {
        accesses: ToolAccesses.readFile(videoPath),
        approvalRule: `track_suspects(${videoPath})`,
        timeoutMs: TRACK_SUSPECTS_TIMEOUT_MS,
        execute: async (): Promise<ExecutableToolResult> => {
          // time_range 未提供时不发送:默认时段的单一权威在 toolserver。
          const body: Record<string, unknown> = {
            video_path: videoPath,
            suspects: input.suspects,
          };
          if (input.time_range !== undefined) body['time_range'] = input.time_range;
          const result = await client.post<TrackSuspectsResponse>(
            '/tools/track_suspects',
            body,
            TRACK_SUSPECTS_TIMEOUT_MS,
          );
          if (!result.ok) return toolserverErrorResult(result.error);

          if (result.data.failed === true) {
            // 业务失败不是工具错误(isError=false):明确指引退回纯视觉判断。
            const reason = result.data.failure_reason ?? '未知原因';
            return {
              output: `跟踪失败:${reason},请退回纯视觉判断`,
              isError: false,
            };
          }

          const tracks = result.data.tracks ?? [];
          const parts: ContentPart[] = [];

          // 数值档案摘要:只含文本字段;best_frames 仅保留时间戳(base64 过大)。
          // 数值问题(停多久/方向角/速度)以本档案为唯一引用来源。
          const summaries = tracks.map((track) => ({
            id: track.id,
            description: track.description,
            profile: track.profile,
            side_hint: track.side_hint,
            direction_verdict: track.direction_verdict,
            best_frame_timestamps: track.best_frames.map((frame) => frame.timestamp),
          }));
          parts.push({
            type: 'text',
            text:
              `已跟踪 ${summaries.length} 条目标轨迹,数值档案(JSON,数值问题的唯一引用来源)如下:\n` +
              JSON.stringify(summaries),
          });

          // 轨迹叠加图:车道归属等语义问题看这张图。
          if (result.data.annotated_image !== undefined) {
            parts.push({ type: 'text', text: '全部轨迹叠加图:' });
            parts.push(jpegImagePart(result.data.annotated_image));
          }

          // 每条轨迹的关键帧裁剪图。
          for (const track of tracks) {
            for (const frame of track.best_frames ?? []) {
              parts.push({
                type: 'text',
                text: `轨迹 ${track.id} 关键帧 @ ${frame.timestamp}s:`,
              });
              parts.push(jpegImagePart(frame.jpeg_base64));
            }
          }

          const artifacts = result.data.artifacts;
          if (artifacts !== undefined) {
            parts.push({
              type: 'text',
              text:
                `取证产物已保存:目录 ${artifacts.dir};轨迹片段 ${artifacts.clip};数据表 ${artifacts.csv}` +
                '(供用户复核与引用)',
            });
          }

          return { output: parts };
        },
      };
    },
  };
}
