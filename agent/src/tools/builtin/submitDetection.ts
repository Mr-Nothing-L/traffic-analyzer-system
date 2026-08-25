/**
 * submit_detection: the structured output contract for the detection agent.
 *
 * The model-facing parameter schema is agent/config/submit_detection.schema.json
 * (loaded and inlined by index.ts where toolset.json references it via $ref).
 * On top of the schema, this tool enforces the runtime cross-checks documented
 * in toolset.json `limits`:
 *   - binary_encoding bit i (1..11, 9 reserved) must match events[event_id=i].detected
 *   - normal === true iff binary_encoding is all zeros
 *   - detected === true requires a non-empty evidence_frames
 * Violations are returned as isError results describing each inconsistency so
 * the model can fix and retry. A valid submission returns
 * `{output: '检测结果已提交', stopTurn: true}` with the structured detection
 * payload carried as a JSON string in `note` (the tool contract has no
 * dedicated field for structured attachments).
 *
 * Per-event annotated images: during execution (after validation passes), each
 * detected event carrying both `boxes` and `box_frame` is annotated via the
 * toolserver POST /tools/draw_boxes ({video_path, timestamp: box_frame,
 * boxes}); the returned JPEG is embedded as a data URL in that event's
 * `annotated_image` field. Annotation failures degrade gracefully — the event
 * keeps no `annotated_image` and its id is listed in note meta
 * `annotation_missing`; detected events lacking boxes/box_frame are likewise
 * soft-recorded in meta `annotation_not_provided`. Neither blocks submission.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { z } from 'zod';

import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolResult,
} from '../contract';
import { ToolserverClient } from './httpToolserver';
import { invalidInputResult, resolveSandboxPath } from './utils';

const ACTIVE_EVENT_IDS = new Set([1, 2, 3, 4, 5, 6, 7, 8, 10, 11]);
const RESERVED_BIT_INDEX = 9;
const ENCODING_LENGTH = 11;
const BINARY_ENCODING_PATTERN = /^[01]_[01]_[01]_[01]_[01]_[01]_[01]_[01]_0_[01]_[01]$/;

const instanceSchema = z.strictObject({
  description: z.string(),
  location: z.string(),
  start_sec: z.number().min(0),
  end_sec: z.number().min(0),
});

const boxSchema = z.strictObject({
  x1: z.number().min(0).max(1),
  y1: z.number().min(0).max(1),
  x2: z.number().min(0).max(1),
  y2: z.number().min(0).max(1),
  label: z.string().optional(),
});

const eventSchema = z.strictObject({
  event_id: z
    .number()
    .int()
    .refine((id) => ACTIVE_EVENT_IDS.has(id), {
      message: 'event_id 必须是活跃事件编号之一:1-8、10、11(9 为保留位)',
    }),
  detected: z.boolean(),
  confidence: z.number().min(0).max(1),
  instances: z.array(instanceSchema),
  reasoning: z.string(),
  // Frame timestamps (seconds); frames are never written to disk.
  evidence_frames: z.array(z.number()),
  // Optional localization boxes (normalized xyxy) + the frame they belong to;
  // used to render an annotated image for the user at submission time.
  boxes: z.array(boxSchema).min(1).optional(),
  box_frame: z.number().min(0).optional(),
});

const submitDetectionInputSchema = z.strictObject({
  video_path: z.string(),
  events: z.array(eventSchema).min(1),
  binary_encoding: z
    .string()
    .regex(BINARY_ENCODING_PATTERN, '必须是 11 位 0/1,以下划线连接,且位 9 恒为 0'),
  normal: z.boolean(),
  report_markdown: z.string().min(1),
});

type SubmitDetectionInput = z.infer<typeof submitDetectionInputSchema>;

interface DrawBoxesResponse {
  jpeg_base64: string;
  width: number;
  height: number;
}

/**
 * Optional dependencies for the annotation step. When `workspace` is given,
 * `video_path` is sandbox-validated (read) at resolve time and a violation is
 * a hard veto; without it the toolserver's own path allowlist is the only
 * enforcement. When `client` is omitted, a default ToolserverClient
 * (TOOLSERVER_URL env or http://127.0.0.1:8601) is created lazily on first use.
 */
export interface SubmitDetectionDeps {
  readonly client?: ToolserverClient;
  readonly workspace?: WorkspaceConfig;
}

/**
 * qwen3_xml 序列化的已知怪癖:模型有时把数组/对象参数包成 JSON 字符串
 * (例如 events: "\n[{...}]"),且收到 "expected array, received string" 后
 * 也无法自我修正(实测死循环 18 次)。这里做容错反序列化:字符串字段在
 * trim 后若能 JSON.parse 成目标形态,先还原再交给 zod 严格校验。
 */
function tryParseJsonString(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed.startsWith('[') && !trimmed.startsWith('{') && !trimmed.startsWith('"')) {
    return value;
  }
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return value;
  }
}

export function normalizeSubmitInput(rawInput: unknown): unknown {
  const top = tryParseJsonString(rawInput);
  if (typeof top !== 'object' || top === null || Array.isArray(top)) return top;
  const obj = { ...(top as Record<string, unknown>) };
  obj['events'] = tryParseJsonString(obj['events']);
  if (Array.isArray(obj['events'])) {
    obj['events'] = obj['events'].map((event: unknown) => {
      if (typeof event !== 'object' || event === null || Array.isArray(event)) return event;
      const ev = { ...(event as Record<string, unknown>) };
      ev['instances'] = tryParseJsonString(ev['instances']);
      ev['evidence_frames'] = tryParseJsonString(ev['evidence_frames']);
      ev['boxes'] = tryParseJsonString(ev['boxes']);
      return ev;
    });
  }
  return obj;
}

/** Load the model-facing parameter schema (agent/config/submit_detection.schema.json). */
export function loadSubmitDetectionSchema(): Record<string, unknown> {
  const schemaUrl = new URL('../../../config/submit_detection.schema.json', import.meta.url);
  return JSON.parse(readFileSync(fileURLToPath(schemaUrl), 'utf8')) as Record<string, unknown>;
}

/** Runtime cross-checks beyond the JSON schema. Returns a list of violations. */
export function crossValidateDetection(input: SubmitDetectionInput): string[] {
  const violations: string[] = [];
  const bits = input.binary_encoding.split('_');
  const detectedById = new Map(input.events.map((event) => [event.event_id, event.detected]));

  for (let position = 1; position <= ENCODING_LENGTH; position++) {
    if (position === RESERVED_BIT_INDEX) continue;
    const bit = bits[position - 1];
    const bitSet = bit === '1';
    const detected = detectedById.get(position);
    if (detected === undefined) {
      if (bitSet) {
        violations.push(
          `binary_encoding 位 ${position} 为 1,但 events 中缺少 event_id=${position} 的判定`,
        );
      }
      continue;
    }
    if (bitSet !== detected) {
      violations.push(
        `binary_encoding 位 ${position} 为 ${bit ?? '?'},但 events 中 event_id=${position} 的 detected=${detected}`,
      );
    }
  }

  const allZero = bits.every((bit) => bit === '0');
  if (input.normal !== allZero) {
    violations.push(
      `normal=${input.normal},但 binary_encoding ${allZero ? '为全零(应为 normal=true)' : '含置位(应为 normal=false)'}`,
    );
  }

  for (const event of input.events) {
    if (event.detected && event.evidence_frames.length === 0) {
      violations.push(
        `event_id=${event.event_id} detected=true,但 evidence_frames 为空(锚定核验的硬性要求)`,
      );
    }
  }

  return violations;
}

/**
 * Soft check (never rejects): detected events lacking boxes and/or box_frame.
 * Their ids are recorded in note meta `annotation_not_provided` so the
 * frontend can degrade to a no-image presentation.
 */
export function findEventsWithoutBoxes(input: SubmitDetectionInput): number[] {
  return input.events
    .filter(
      (event) =>
        event.detected &&
        (event.boxes === undefined || event.boxes.length === 0 || event.box_frame === undefined),
    )
    .map((event) => event.event_id);
}

export function createSubmitDetectionTool(
  description: string,
  parameters: Record<string, unknown>,
  deps: SubmitDetectionDeps = {},
): ExecutableTool {
  let lazyClient: ToolserverClient | undefined;
  const client = (): ToolserverClient => {
    if (deps.client !== undefined) return deps.client;
    lazyClient ??= new ToolserverClient();
    return lazyClient;
  };

  return {
    name: 'submit_detection',
    description,
    parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = submitDetectionInputSchema.safeParse(normalizeSubmitInput(rawInput));
      if (!parsed.success) return invalidInputResult('submit_detection', parsed.error);
      const input = parsed.data;

      // Sandbox read-check the video used for annotation (hard veto) when a
      // workspace is available; otherwise the toolserver enforces its roots.
      let videoPath = input.video_path;
      if (deps.workspace !== undefined) {
        const resolved = resolveSandboxPath(input.video_path, deps.workspace, 'read');
        if (!resolved.ok) return resolved.result;
        videoPath = resolved.path;
      }

      const violations = crossValidateDetection(input);
      if (violations.length > 0) {
        return {
          output:
            '提交被拒绝,存在以下不一致:\n' +
            violations.map((violation) => `- ${violation}`).join('\n') +
            '\n请修正后重新调用 submit_detection。',
          isError: true,
        };
      }

      const annotationNotProvided = findEventsWithoutBoxes(input);
      return {
        accesses:
          deps.workspace !== undefined
            ? ToolAccesses.readFile(videoPath)
            : ToolAccesses.none(),
        approvalRule: 'submit_detection',
        stopBatchAfterThis: true,
        execute: async (): Promise<ExecutableToolResult> => {
          const annotationMissing: number[] = [];
          const events = input.events.map(async (event) => {
            const annotated: Record<string, unknown> = { ...event };
            const canAnnotate =
              event.detected &&
              event.boxes !== undefined &&
              event.boxes.length > 0 &&
              event.box_frame !== undefined;
            if (!canAnnotate) return annotated;
            const result = await client().post<DrawBoxesResponse>('/tools/draw_boxes', {
              video_path: videoPath,
              timestamp: event.box_frame,
              boxes: event.boxes,
            });
            if (result.ok) {
              annotated['annotated_image'] = `data:image/jpeg;base64,${result.data.jpeg_base64}`;
            } else {
              annotationMissing.push(event.event_id);
            }
            return annotated;
          });
          const payload: Record<string, unknown> = {
            ...input,
            events: await Promise.all(events),
          };
          const meta: Record<string, unknown> = {};
          if (annotationMissing.length > 0) meta['annotation_missing'] = annotationMissing;
          if (annotationNotProvided.length > 0) {
            meta['annotation_not_provided'] = annotationNotProvided;
          }
          if (Object.keys(meta).length > 0) payload['meta'] = meta;
          return {
            output: '检测结果已提交',
            stopTurn: true,
            // The contract has no structured-attachment field; downstream
            // consumers parse the detection payload from this JSON string.
            note: JSON.stringify(payload),
          };
        },
      };
    },
  };
}
