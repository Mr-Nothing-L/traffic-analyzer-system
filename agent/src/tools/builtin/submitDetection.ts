/**
 * submit_detection: the structured output contract for the detection agent.
 *
 * The model-facing parameter schema is agent/config/submit_detection.schema.json
 * (loaded and inlined by index.ts where toolset.json references it via $ref);
 * 其中的 event_id 枚举与编码位宽在加载时由 applyEventContractToSubmitSchema
 * 从 agent/config/event_contract.json(生成自 event_categories.yaml)注入。
 * 运行时(zod)的活跃事件集合同样从 event_contract.json 派生(fail-fast)。
 * On top of the schema, this tool enforces the runtime cross-checks documented
 * in toolset.json `limits` (ADR-0001 semantics):
 *   - binary_encoding bit i (i in 1..8, 10, 11) must match events[event_id=i].detected
 *   - bit 9 is the normal indicator: 1 iff no event is detected
 *   - the `normal` flag must equal bit 9
 *   - detected === true requires a non-empty evidence_frames
 *   - (防跳跟踪闸门,可选)events 1/2/8(违停/应急车道/逆行)任一 detected=true
 *     时,payload.video_path 必须已发起过 track_suspects(经 TrackAttemptRecorder
 *     按 session 记录,失败也算);未发起则拒绝提交并指引先取证,禁止目测
 *     静止时长/速度。recorder 缺省时不启用该闸门。
 * Violations are returned as isError results describing each inconsistency so
 * the model can fix and retry. A valid submission returns
 * `{output: '检测结果已提交', stopTurn: true, payload}` with the structured
 * detection payload (DetectionPayload) carried in the result's `payload` field
 * — the first-class data channel consumed by the server (detection SSE 事件
 * 与落盘条目)and spawn_subagent, no string encoding involved.
 *
 * Per-event annotated images: during execution (after validation passes), each
 * detected event carrying both `boxes` and `box_frame` is annotated via the
 * toolserver POST /tools/draw_boxes ({video_path, timestamp: box_frame,
 * boxes}); the returned JPEG is embedded as a data URL in that event's
 * `annotated_image` field. A failed draw is immediately retried once with the
 * same parameters (no backoff). Annotation degradation is two-level (never
 * blocks submission) and recorded per event_id in payload meta:
 *   - `missing_boxes`: detected event lacking boxes/box_frame (模型侧没给)
 *   - `annotation_failed`: boxes given, but the server-side draw failed twice
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
import {
  applyEventContractToSubmitSchema,
  binaryEncodingPattern,
  formatEventIdList,
  loadEventContract,
} from './eventContract';
import { resolveWorkspacePath } from './fileTools';
import { invalidInputResult } from './utils';
import type { TrackAttemptRecorder } from './trackAttemptRecorder';

/** 活跃事件编号:从 event_contract.json 派生(权威源 event_categories.yaml)。 */
const eventContract = loadEventContract();
const ACTIVE_EVENT_IDS = new Set(eventContract.active_event_ids);
/** Bit 9 carries no event category; it is the normal indicator (ADR-0001). */
const NORMAL_BIT_INDEX = eventContract.normal_bit_index;
const ENCODING_LENGTH = eventContract.encoding_length;
const BINARY_ENCODING_PATTERN = binaryEncodingPattern(ENCODING_LENGTH);
/** 防跳跟踪闸门覆盖的动态事件:1 违停 / 2 应急车道 / 8 逆行倒车。 */
const TRACK_GATED_EVENT_IDS = new Set([1, 2, 8]);

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
      message: `event_id 必须是活跃事件编号之一:${formatEventIdList(eventContract.active_event_ids)}(9 为正常指示位,不对应事件)`,
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
    .regex(BINARY_ENCODING_PATTERN, `必须是 ${ENCODING_LENGTH} 位 0/1,以下划线连接`),
  normal: z.boolean(),
  report_markdown: z.string().min(1),
});

type SubmitDetectionInput = z.infer<typeof submitDetectionInputSchema>;

/** 逐事件条目:zod 校验后的输入事件 + 标注成功时服务端补充的 annotated_image。 */
export interface DetectionPayloadEvent {
  event_id: number;
  detected: boolean;
  confidence: number;
  instances: Array<{
    description: string;
    location: string;
    start_sec: number;
    end_sec: number;
  }>;
  reasoning: string;
  /** 证据帧时间点(秒)。 */
  evidence_frames: number[];
  boxes?: Array<{ x1: number; y1: number; x2: number; y2: number; label?: string }>;
  box_frame?: number;
  /** 逐事件标注图(jpeg dataURL);无框/画框失败时缺省。 */
  annotated_image?: string;
}

/**
 * submit_detection 提交成功后的结构化检测载荷:随工具结果 payload 字段
 * 全链路传输(server detection 事件/落盘条目、前端检测卡渲染)。
 */
export interface DetectionPayload {
  video_path: string;
  events: DetectionPayloadEvent[];
  binary_encoding: string;
  normal: boolean;
  report_markdown: string;
  /** 标注降级元信息(两级,逐事件列出 event_id);无降级时缺省。
   * missing_boxes=检出事件未提供 boxes/box_frame(模型侧没给);
   * annotation_failed=提供了 boxes/box_frame 但服务端 draw_boxes 画框失败
   * (立即重试一次后仍失败)。 */
  meta?: {
    missing_boxes?: number[];
    annotation_failed?: number[];
  };
}

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
 * `trackRecorder` enables the防跳跟踪 soft gate (see module doc); when omitted
 * the gate is disabled.
 */
export interface SubmitDetectionDeps {
  readonly client?: ToolserverClient;
  readonly workspace?: WorkspaceConfig;
  readonly trackRecorder?: TrackAttemptRecorder;
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

/**
 * Load the model-facing parameter schema (agent/config/submit_detection.schema.json)
 * and inject the event contract (event_id 枚举、编码位宽及相关描述)——模型可见
 * 的活跃事件集合永远与 event_contract.json 一致,schema 文件中的静态枚举仅为
 * 文档参考(漂移由测试守护)。
 */
export function loadSubmitDetectionSchema(): Record<string, unknown> {
  const schemaUrl = new URL('../../../config/submit_detection.schema.json', import.meta.url);
  const raw = JSON.parse(readFileSync(fileURLToPath(schemaUrl), 'utf8')) as Record<string, unknown>;
  return applyEventContractToSubmitSchema(raw);
}

/** Runtime cross-checks beyond the JSON schema. Returns a list of violations. */
export function crossValidateDetection(input: SubmitDetectionInput): string[] {
  const violations: string[] = [];
  const bits = input.binary_encoding.split('_');
  const detectedById = new Map(input.events.map((event) => [event.event_id, event.detected]));

  for (let position = 1; position <= ENCODING_LENGTH; position++) {
    if (position === NORMAL_BIT_INDEX) continue;
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

  // ADR-0001:位 9 是正常指示位——为 1 当且仅当无任何事件检出。
  const bit9 = bits[NORMAL_BIT_INDEX - 1];
  const bit9Set = bit9 === '1';
  const anyDetected = input.events.some((event) => event.detected);
  if (bit9Set !== !anyDetected) {
    violations.push(
      `binary_encoding 位 9 为 ${bit9 ?? '?'},但 events 中${
        anyDetected ? '存在 detected=true 的事件,位 9 应为 0' : '所有事件均未检出,位 9 应为 1(正常指示位)'
      }`,
    );
  }
  if (input.normal !== bit9Set) {
    violations.push(
      `normal=${input.normal},与 binary_encoding 位 9(${bit9 ?? '?'})不一致:位 9 为 1 时 normal 必须为 true,位 9 为 0 时必须为 false`,
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
 * Their ids are recorded in payload meta `missing_boxes`, so debug can tell a
 * model-side gap (`missing_boxes`) apart from a server-side drawing failure
 * (`annotation_failed`). Neither blocks submission.
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
      // workspace is available — same strict boundary as every other tool, so
      // an absolute path outside the workspace is also rejected; without a
      // workspace the toolserver's own path allowlist is the only enforcement.
      let videoPath = input.video_path;
      if (deps.workspace !== undefined) {
        const resolved = resolveWorkspacePath(input.video_path, deps.workspace, 'read');
        if (!resolved.ok) return resolved.result;
        videoPath = resolved.path;
      }

      // 防跳跟踪软闸门:事件 1/2/8 任一 detected=true 前必须对本视频发起过
      // track_suspects(发起即记,失败也算——可凭回退结论提交)。未发起则
      // 拒绝并指引取证;recorder 缺省时不启用(直接构造工厂的用例行为不变)。
      if (deps.trackRecorder !== undefined) {
        const gatedDetected = input.events
          .filter((event) => TRACK_GATED_EVENT_IDS.has(event.event_id) && event.detected)
          .map((event) => event.event_id);
        if (gatedDetected.length > 0 && !deps.trackRecorder.hasAttempted(videoPath)) {
          return {
            output:
              `提交被拒绝:事件 ${gatedDetected.join('/')} 判定 detected=true 前,` +
              '必须先调用 track_suspects 取证(即使跟踪失败,也可凭回退结论再提交),' +
              '禁止目测静止时长/速度。\n' +
              '请先对本视频调用 track_suspects,再重新提交。',
            isError: true,
          };
        }
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

      return {
        accesses:
          deps.workspace !== undefined
            ? ToolAccesses.readFile(videoPath)
            : ToolAccesses.none(),
        // 正式检测提交按 video_path 区分规则:不同视频独立确认,
        // 相同视频在 session 内可复用(避免重复审批)。
        approvalRule: `submit_detection(${videoPath})`,
        execute: async (): Promise<ExecutableToolResult> => {
          const annotationFailed: number[] = [];
          const events = input.events.map(async (event): Promise<DetectionPayloadEvent> => {
            const annotated: DetectionPayloadEvent = { ...event };
            const canAnnotate =
              event.detected &&
              event.boxes !== undefined &&
              event.boxes.length > 0 &&
              event.box_frame !== undefined;
            if (!canAnnotate) return annotated;
            const drawBody = {
              video_path: videoPath,
              timestamp: event.box_frame,
              boxes: event.boxes,
            };
            // 瞬时失败(如 toolserver 重启瞬间)同参数立即重试一次,仍失败才计降级。
            let result = await client().post<DrawBoxesResponse>('/tools/draw_boxes', drawBody);
            if (!result.ok) {
              result = await client().post<DrawBoxesResponse>('/tools/draw_boxes', drawBody);
            }
            if (result.ok) {
              annotated.annotated_image = `data:image/jpeg;base64,${result.data.jpeg_base64}`;
            } else {
              annotationFailed.push(event.event_id);
            }
            return annotated;
          });
          const payload: DetectionPayload = { ...input, events: await Promise.all(events) };
          const meta: NonNullable<DetectionPayload['meta']> = {};
          if (annotationFailed.length > 0) meta.annotation_failed = annotationFailed;
          const missingBoxes = findEventsWithoutBoxes(input);
          if (missingBoxes.length > 0) meta.missing_boxes = missingBoxes;
          if (Object.keys(meta).length > 0) payload.meta = meta;
          return {
            output: '检测结果已提交',
            stopTurn: true,
            payload,
          };
        },
      };
    },
  };
}
