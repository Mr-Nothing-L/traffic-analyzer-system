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
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { z } from 'zod';

import {
  ToolAccesses,
  type ExecutableTool,
  type ExecutableToolResult,
} from '../contract';
import { invalidInputResult } from './utils';

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
  evidence_frames: z.array(z.string()),
});

const submitDetectionInputSchema = z.strictObject({
  events: z.array(eventSchema).min(1),
  binary_encoding: z
    .string()
    .regex(BINARY_ENCODING_PATTERN, '必须是 11 位 0/1,以下划线连接,且位 9 恒为 0'),
  normal: z.boolean(),
  report_markdown: z.string().min(1),
});

type SubmitDetectionInput = z.infer<typeof submitDetectionInputSchema>;

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

export function createSubmitDetectionTool(
  description: string,
  parameters: Record<string, unknown>,
): ExecutableTool {
  return {
    name: 'submit_detection',
    description,
    parameters,
    resolveExecution(rawInput: unknown) {
      const parsed = submitDetectionInputSchema.safeParse(rawInput);
      if (!parsed.success) return invalidInputResult('submit_detection', parsed.error);
      const input = parsed.data;
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
        accesses: ToolAccesses.none(),
        approvalRule: 'submit_detection',
        stopBatchAfterThis: true,
        execute: async (): Promise<ExecutableToolResult> => ({
          output: '检测结果已提交',
          stopTurn: true,
          // The contract has no structured-attachment field; downstream
          // consumers parse the detection payload from this JSON string.
          note: JSON.stringify(input),
        }),
      };
    },
  };
}
