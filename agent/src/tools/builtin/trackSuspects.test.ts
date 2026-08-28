/**
 * Unit tests for track_suspects. The toolserver is mocked (stubbed global
 * fetch) per the /tools/track_suspects contract; no real model API or
 * toolserver process is touched.
 */
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ContentPart } from '../../llm/kosong';
import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  isRunnableToolExecution,
  type ExecutableToolErrorResult,
  type ExecutableToolResult,
  type RunnableToolExecution,
} from '../contract';
import { ToolserverClient } from './httpToolserver';
import {
  TRACK_SUSPECTS_TIMEOUT_MS,
  createTrackSuspectsTool,
} from './trackSuspects';

const JPEG_OVERLAY = Buffer.from('overlay-annotated-jpeg').toString('base64');
const JPEG_CROP_A = Buffer.from('track1-crop-a-jpeg').toString('base64');
const JPEG_CROP_B = Buffer.from('track1-crop-b-jpeg').toString('base64');

let workspaceDir: string;
let workspace: WorkspaceConfig;
let fetchMock: ReturnType<typeof vi.fn>;
let client: ToolserverClient;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'track-suspects-test-'));
  workspace = { workspaceDir, additionalDirs: [] };
  fetchMock = vi.fn();
  client = new ToolserverClient({ baseUrl: 'http://127.0.0.1:8601', fetchImpl: fetchMock });
});

afterEach(() => {
  rmSync(workspaceDir, { recursive: true, force: true });
});

function mockTrack(payload: unknown, status = 200): void {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(payload), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  );
}

function tool() {
  return createTrackSuspectsTool(client, workspace, {
    description: 'desc:track_suspects',
    parameters: { type: 'object', properties: {} },
  });
}

async function execute(input: unknown): Promise<ExecutableToolResult> {
  const execution = tool().resolveExecution(input);
  if (!isRunnableToolExecution(execution)) {
    return execution as ExecutableToolErrorResult;
  }
  return (execution as RunnableToolExecution).execute({
    toolCallId: 'test-call',
    signal: new AbortController().signal,
  });
}

function runnable(input: unknown): RunnableToolExecution {
  const execution = tool().resolveExecution(input);
  if (!isRunnableToolExecution(execution)) throw new Error('expected runnable execution');
  return execution;
}

const ANCHORS = [
  {
    box: { x1: 0.1, y1: 0.2, x2: 0.3, y2: 0.4 },
    timestamp: 3.5,
    description: '白色小客车,疑似长时间静止',
  },
];

function successPayload() {
  const dir = path.join(workspaceDir, '.agent', 'tracking', 'run-1');
  return {
    tracks: [
      {
        id: 1,
        description: '白色小客车',
        profile: '静止 12.4s;平均速度 0.02km/h',
        side_hint: '右侧第 3 车道附近',
        direction_verdict: '全程停驻,未发生位移',
        best_frames: [
          { timestamp: 2, jpeg_base64: JPEG_CROP_A },
          { timestamp: 9, jpeg_base64: JPEG_CROP_B },
        ],
      },
      {
        id: 2,
        description: '黑色轿车',
        profile: '位移 145m;平均速度 61km/h(逆向)',
        side_hint: '对向车道一侧',
        direction_verdict: '逆行(航向角约 175°)',
        best_frames: [],
      },
    ],
    annotated_image: JPEG_OVERLAY,
    artifacts: {
      dir,
      clip: path.join(dir, 'track_1.mp4'),
      csv: path.join(dir, 'tracks.csv'),
    },
    failed: false,
    failure_reason: null,
  };
}

describe('track_suspects', () => {
  it('posts the resolved video_path with suspects/time_range to /tools/track_suspects', async () => {
    mockTrack(successPayload());
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const result = await execute({
      video_path: videoPath,
      suspects: ANCHORS,
      time_range: [0, 15],
    });
    expect(result.isError).toBeFalsy();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8601/tools/track_suspects');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: videoPath,
      suspects: ANCHORS,
      time_range: [0, 15],
    });
  });

  it('omits an absent time_range (default period lives in the toolserver)', async () => {
    mockTrack(successPayload());
    await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: ANCHORS,
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // 未提供的 time_range 不发送:默认时段的单一权威在 toolserver。
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: ANCHORS,
    });
  });

  it('renders summary text + overlay image + per-track best-frame crops + artifacts paths', async () => {
    mockTrack(successPayload());
    const result = await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: ANCHORS,
    });
    expect(result.isError).toBeFalsy();
    const parts = result.output as ContentPart[];
    const joinedText = parts
      .filter((part) => part.type === 'text')
      .map((part) => (part.type === 'text' ? part.text : ''))
      .join('\n');
    const imageParts = parts.filter((part) => part.type === 'image_url');
    expect(imageParts).toHaveLength(3); // 叠加图 + 轨迹 1 的 2 张关键帧

    // 数值档案摘要文本:含各轨迹的数值字段,best_frames 只留时间戳。
    expect(joinedText).toContain('2 条目标轨迹');
    expect(joinedText).toContain('静止 12.4s');
    expect(joinedText).toContain('逆行(航向角约 175°)');
    expect(joinedText).not.toContain(JPEG_OVERLAY);

    // 图片 part 均为 dataURL,顺序:叠加图 → 关键帧裁剪图。
    const urls = imageParts.map((part) =>
      part.type === 'image_url' ? part.imageUrl.url : '',
    );
    expect(urls[0]).toBe(`data:image/jpeg;base64,${JPEG_OVERLAY}`);
    expect(urls.slice(1)).toEqual([
      `data:image/jpeg;base64,${JPEG_CROP_A}`,
      `data:image/jpeg;base64,${JPEG_CROP_B}`,
    ]);
    expect(joinedText).toContain('轨迹 1 关键帧 @ 9s:');

    // artifacts 路径以文本返回。
    expect(joinedText).toContain(path.join(workspaceDir, '.agent', 'tracking', 'run-1'));
    expect(joinedText).toContain('tracks.csv');
  });

  it('declares timeoutMs=900000 with a read access and an approvalRule on the resolved video', () => {
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const execution = runnable({ video_path: videoPath, suspects: ANCHORS });
    expect(execution.timeoutMs).toBe(TRACK_SUSPECTS_TIMEOUT_MS);
    expect(execution.timeoutMs).toBe(900_000);
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'read', path: videoPath, recursive: undefined },
    ]);
    expect(execution.approvalRule).toBe(`track_suspects(${videoPath})`);
  });

  it('reports business failure (failed=true) as a NON-error result telling the model to fall back', async () => {
    mockTrack({
      tracks: [],
      failed: true,
      failure_reason: '锚点时刻前后均未检出目标,无法建轨迹',
    });
    const result = await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: ANCHORS,
    });
    // 业务失败不是工具错误,isError=false 由输出文本表达退回指引。
    expect(result.isError).toBeFalsy();
    expect(typeof result.output).toBe('string');
    expect(result.output).toContain('跟踪失败');
    expect(result.output).toContain('锚点时刻前后均未检出目标,无法建轨迹');
    expect(result.output).toContain('退回纯视觉判断');
  });

  it('falls back to the unknown-reason placeholder when failure_reason is missing', async () => {
    mockTrack({ tracks: [], failed: true });
    const result = await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: ANCHORS,
    });
    expect(result.isError).toBeFalsy();
    expect(result.output).toContain('跟踪失败:未知原因');
  });

  it('hard-vetoes a relative path escaping the workspace without calling the toolserver', async () => {
    const result = await execute({ video_path: '../outside.mp4', suspects: ANCHORS });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('hard-vetoes an ABSOLUTE path outside the workspace', async () => {
    const result = await execute({
      video_path: path.join(os.tmpdir(), 'elsewhere.mp4'),
      suspects: ANCHORS,
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects more than five suspects (zod max)', async () => {
    const six = Array.from({ length: 6 }, (_, i) => ({
      box: { x1: 0.1, y1: 0.1, x2: 0.2, y2: 0.2 },
      timestamp: i,
      description: `suspect-${i}`,
    }));
    const result = await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: six,
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('track_suspects 参数不合法');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects empty suspects and out-of-range box coordinates', async () => {
    const empty = await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: [],
    });
    expect(empty.isError).toBe(true);

    const badBox = await execute({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      suspects: [{ ...ANCHORS[0], box: { x1: 0.1, y1: 0.1, x2: 1.5, y2: 0.2 } }],
    });
    expect(badBox.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('maps a non-2xx toolserver error contract to an isError result', async () => {
    mockTrack(
      { error: { code: 'video_not_found', message: 'Video not found: x.mp4' } },
      404,
    );
    const result = await execute({
      video_path: path.join(workspaceDir, 'x.mp4'),
      suspects: ANCHORS,
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('video_not_found');
  });

  it('maps network failure to toolserver_unreachable', async () => {
    fetchMock.mockRejectedValueOnce(new Error('connect ECONNREFUSED'));
    const result = await execute({
      video_path: path.join(workspaceDir, 'x.mp4'),
      suspects: ANCHORS,
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('toolserver_unreachable');
  });
});
