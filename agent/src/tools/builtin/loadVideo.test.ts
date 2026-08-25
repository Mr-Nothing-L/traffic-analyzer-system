/**
 * Unit tests for load_video. The toolserver is mocked (stubbed global fetch);
 * the "prepared" mp4 is a tmp file holding only a fake mp4 header (no real
 * decoding). No real model API or toolserver process is touched.
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ContentPart, VideoURLPart } from '../../kosong/message';
import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  isRunnableToolExecution,
  type ExecutableToolResult,
  type ExecutableToolErrorResult,
  type RunnableToolExecution,
} from '../contract';
import { ToolserverClient } from './httpToolserver';
import { createLoadVideoTool } from './loadVideo';

// Minimal fake mp4 bytes: ftyp box header only; never actually decoded.
const FAKE_MP4 = Buffer.from([
  0x00, 0x00, 0x00, 0x20, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d,
]);

let workspaceDir: string;
let workspace: WorkspaceConfig;
let fetchMock: ReturnType<typeof vi.fn>;
let client: ToolserverClient;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'load-video-test-'));
  workspace = { workspaceDir, additionalDirs: [] };
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  client = new ToolserverClient({ baseUrl: 'http://127.0.0.1:8601' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  rmSync(workspaceDir, { recursive: true, force: true });
});

function mockPrepareVideo(payload: unknown, status = 200): void {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(payload), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  );
}

function tool() {
  return createLoadVideoTool(client, workspace, 'desc:load_video');
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

function preparedPayload(overrides: Record<string, unknown> = {}) {
  return {
    path: path.join(workspaceDir, 'prepared.mp4'),
    size_bytes: FAKE_MP4.byteLength,
    fps: 25,
    duration_s: 20,
    transcoded: false,
    ...overrides,
  };
}

describe('load_video', () => {
  it('posts video_path with the default max_mb and returns text + video parts', async () => {
    const preparedPath = path.join(workspaceDir, 'prepared.mp4');
    writeFileSync(preparedPath, FAKE_MP4);
    mockPrepareVideo(preparedPayload());

    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const result = await execute({ video_path: videoPath });
    expect(result.isError).toBeFalsy();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8601/tools/prepare_video');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ video_path: videoPath, max_mb: 40 });

    const parts = result.output as ContentPart[];
    expect(parts).toHaveLength(2);
    const text = parts[0];
    expect(text?.type).toBe('text');
    if (text?.type !== 'text') throw new Error('unreachable');
    expect(text.text).toContain('时长 20s');
    expect(text.text).toContain('fps 25');
    expect(text.text).toContain('未转码');

    const video = parts[1] as VideoURLPart;
    expect(video.type).toBe('video_url');
    expect(video.videoUrl.url).toBe(`data:video/mp4;base64,${FAKE_MP4.toString('base64')}`);
  });

  it('passes a caller-supplied max_mb through to prepare_video', async () => {
    writeFileSync(path.join(workspaceDir, 'prepared.mp4'), FAKE_MP4);
    mockPrepareVideo(preparedPayload());
    await execute({ video_path: path.join(workspaceDir, 'demo.mp4'), max_mb: 12.5 });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toMatchObject({ max_mb: 12.5 });
  });

  it('reads the transcoded file the toolserver points at and says so in the text', async () => {
    // prepare_video returns a different (transcoded) path; the tool must read
    // that file, not the original video.
    const transcodedPath = path.join(workspaceDir, 'prepared_8fps.mp4');
    writeFileSync(transcodedPath, FAKE_MP4);
    mockPrepareVideo(preparedPayload({ path: transcodedPath, fps: 8, transcoded: true }));

    const result = await execute({ video_path: path.join(workspaceDir, 'demo.mp4') });
    expect(result.isError).toBeFalsy();

    const parts = result.output as ContentPart[];
    const text = parts[0];
    if (text?.type !== 'text') throw new Error('unreachable');
    expect(text.text).toContain('已降帧/转码');
    expect(text.text).toContain('fps 8');

    const video = parts[1] as VideoURLPart;
    expect(video.videoUrl.url).toBe(`data:video/mp4;base64,${FAKE_MP4.toString('base64')}`);
  });

  it('declares a file read access on the resolved video path', () => {
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const execution = tool().resolveExecution({ video_path: videoPath });
    if (!isRunnableToolExecution(execution)) throw new Error('expected runnable');
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'read', path: videoPath, recursive: undefined },
    ]);
    expect(execution.approvalRule).toContain(videoPath);
  });

  it('fails over to draw_boxes when the prepared file is still > 50MB', async () => {
    mockPrepareVideo(preparedPayload({ size_bytes: 60 * 1024 * 1024, transcoded: true }));
    const result = await execute({ video_path: path.join(workspaceDir, 'demo.mp4') });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('draw_boxes');
    expect(result.output).toContain('50');
  });

  it('maps a non-2xx toolserver error contract to an isError result', async () => {
    mockPrepareVideo({ error: { code: 'video_not_found', message: 'Video not found: x.mp4' } }, 404);
    const result = await execute({ video_path: path.join(workspaceDir, 'x.mp4') });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('video_not_found');
  });

  it('maps network failure to toolserver_unreachable', async () => {
    fetchMock.mockRejectedValueOnce(new Error('connect ECONNREFUSED'));
    const result = await execute({ video_path: path.join(workspaceDir, 'x.mp4') });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('toolserver_unreachable');
  });

  it('returns isError when the prepared file cannot be read', async () => {
    mockPrepareVideo(preparedPayload({ path: path.join(workspaceDir, 'gone.mp4') }));
    const result = await execute({ video_path: path.join(workspaceDir, 'demo.mp4') });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('读取预处理后的视频文件失败');
  });

  it('hard-vetoes a path escaping the workspace without calling the toolserver', async () => {
    const result = await execute({ video_path: '../outside.mp4' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects invalid input (non-positive max_mb)', async () => {
    const result = await execute({ video_path: path.join(workspaceDir, 'demo.mp4'), max_mb: -1 });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('load_video 参数不合法');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
