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
 *
 * 取证发起记录:传入可选 recorder(TrackAttemptRecorder)时,输入合法且
 * video_path 经 resolver 规范化通过后立即记录(调用开始即记,不管后续
 * 工具调用成败),供 submit_detection 的防跳跟踪闸门核查。
 */
import { z } from 'zod';

import type { ContentPart } from '../../llm/kosong';
import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolResult,
} from '../contract';
import type { TrackAttemptRecorder } from './trackAttemptRecorder';
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
  // toolserver 实际返回数值档案对象(可含 covered_s/coverage);字符串按旧形态透传。
  profile: unknown;
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
  side: z.enum(['coming', 'going', 'unknown']).optional(),
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

/** profile 为对象时取覆盖字段;字符串/字段缺失/类型不符时返回空(优雅降级不展示)。 */
function coverageOf(profile: unknown): { coveredS?: number; coverage?: number } {
  if (typeof profile !== 'object' || profile === null || Array.isArray(profile)) return {};
  const record = profile as Record<string, unknown>;
  const coveredS = record['covered_s'];
  const coverage = record['coverage'];
  return {
    ...(typeof coveredS === 'number' ? { coveredS } : {}),
    ...(typeof coverage === 'number' ? { coverage } : {}),
  };
}

/** 单条轨迹的摘要文本:direction_verdict 显著行置顶,数值档案随后(模型先看裁决再看数值)。 */
function renderTrackSummary(track: SuspectTrack): string {
  const lines = [
    `【轨迹 ${track.id}】direction_verdict:${track.direction_verdict}`,
    `描述:${track.description} | 方位:${track.side_hint}`,
    `数值档案:${
      typeof track.profile === 'string' ? track.profile : JSON.stringify(track.profile ?? {})
    }`,
  ];
  const { coveredS, coverage } = coverageOf(track.profile);
  const coverageParts: string[] = [];
  if (coveredS !== undefined) coverageParts.push(`covered_s=${coveredS}s`);
  if (coverage !== undefined) coverageParts.push(`coverage=${Math.round(coverage * 100)}%`);
  if (coverageParts.length > 0) lines.push(`轨迹覆盖:${coverageParts.join(',')}`);
  if (coverage !== undefined && coverage < 0.5) {
    lines.push(`注意:轨迹仅覆盖时段的 ${Math.round(coverage * 100)}%,结论证据不足`);
  }
  const timestamps = track.best_frames.map((frame) => frame.timestamp);
  if (timestamps.length > 0) {
    lines.push(`关键帧时刻:${timestamps.map((t) => `${t}s`).join('、')}`);
  }
  return lines.join('\n');
}

/** 矛盾裁决指引(存在任一 verdict 时追加):防模型仅凭 speed≈0 推翻跟踪器裁决。 */
const VERDICT_CONTRADICTION_GUIDANCE =
  '提示:若 direction_verdict 与速度/位移数值看似矛盾,以 direction_verdict 与画面证据为准;禁止仅凭 speed≈0 推翻 verdict。';

export function createTrackSuspectsTool(
  client: ToolserverClient,
  workspace: WorkspaceConfig,
  spec: ToolsetEntrySpec,
  recorder?: TrackAttemptRecorder,
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
      // 调用开始即记(不管成败):submit_detection 防跳跟踪闸门的核查依据。
      recorder?.record(videoPath);
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

          // 逐轨迹摘要:direction_verdict 显著行置顶,数值档案随后——数值问题
          // (停多久/方向角/速度)以「数值档案」为唯一引用来源;存在 verdict 时
          // 末尾追加矛盾裁决指引,防止模型仅凭 speed≈0 推翻跟踪器裁决。
          const trackCount = tracks.length;
          const summaryBlocks = tracks.map(renderTrackSummary);
          const hasVerdict = tracks.some((track) => track.direction_verdict.trim() !== '');
          if (hasVerdict) summaryBlocks.push(VERDICT_CONTRADICTION_GUIDANCE);
          parts.push({
            type: 'text',
            text:
              `已跟踪 ${trackCount} 条目标轨迹` +
              '(数值问题以各轨迹「数值档案」为唯一引用来源):\n\n' +
              summaryBlocks.join('\n\n'),
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
