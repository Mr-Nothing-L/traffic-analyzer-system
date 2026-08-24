/**
 * Unit tests for agent/src/tools/builtin. All toolserver traffic is mocked
 * (stubbed global fetch); file/script tools run against a tmp workspace.
 * No real model API or toolserver process is touched.
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ContentPart } from '../../kosong/message';
import type { WorkspaceConfig } from '../../sandbox/path-access';
import {
  isRunnableToolExecution,
  type ExecutableTool,
  type ExecutableToolErrorResult,
  type ExecutableToolResult,
  type RunnableToolExecution,
} from '../contract';
import { ToolRegistry } from '../registry';
import { ToolserverClient } from './httpToolserver';
import { registerBuiltinTools } from './index';
import { createSubmitDetectionTool, loadSubmitDetectionSchema } from './submitDetection';
import { createVideoTools } from './videoTools';
import { createFileTools } from './fileTools';

const FAKE_JPEG_BASE64 = Buffer.from('fake-jpeg-bytes').toString('base64');

let workspaceDir: string;
let workspace: WorkspaceConfig;
let fetchMock: ReturnType<typeof vi.fn>;
let client: ToolserverClient;

beforeEach(() => {
  workspaceDir = mkdtempSync(path.join(os.tmpdir(), 'builtin-tools-test-'));
  workspace = { workspaceDir, additionalDirs: [] };
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  client = new ToolserverClient({ baseUrl: 'http://127.0.0.1:8601' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  rmSync(workspaceDir, { recursive: true, force: true });
});

function mockToolserver(payload: unknown, status = 200): void {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(payload), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  );
}

function runnable(tool: ExecutableTool, input: unknown): RunnableToolExecution {
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
  if (!isRunnableToolExecution(execution as never)) {
    throw new Error(
      `expected runnable execution, got: ${JSON.stringify(execution)}`,
    );
  }
  return execution as RunnableToolExecution;
}

async function execute(tool: ExecutableTool, input: unknown): Promise<ExecutableToolResult> {
  const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
  if (!isRunnableToolExecution(execution as never)) {
    return execution as ExecutableToolErrorResult;
  }
  return (execution as RunnableToolExecution).execute({
    toolCallId: 'test-call',
    signal: new AbortController().signal,
  });
}

function videoTool(name: string): ExecutableTool {
  const tools = createVideoTools(client, workspace, (n) => `desc:${n}`);
  const tool = tools.find((t) => t.name === name);
  if (!tool) throw new Error(`tool ${name} not found`);
  return tool;
}

function fileTool(name: string): ExecutableTool {
  const tools = createFileTools(workspace, (n) => `desc:${n}`);
  const tool = tools.find((t) => t.name === name);
  if (!tool) throw new Error(`tool ${name} not found`);
  return tool;
}

describe('video_meta', () => {
  it('posts video_path and returns metadata JSON', async () => {
    mockToolserver({ duration_s: 12.5, fps: 25, width: 1920, height: 1080, frame_count: 312 });
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const result = await execute(videoTool('video_meta'), { video_path: videoPath });
    expect(result.isError).toBeFalsy();
    expect(JSON.parse(result.output as string)).toMatchObject({ fps: 25, frame_count: 312 });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8601/tools/video_meta');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ video_path: videoPath });
  });

  it('declares a file read access on the resolved video path', () => {
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const execution = runnable(videoTool('video_meta'), { video_path: videoPath });
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'read', path: videoPath, recursive: undefined },
    ]);
    expect(execution.approvalRule).toContain(videoPath);
  });

  it('maps a non-2xx toolserver error contract to an isError result', async () => {
    mockToolserver({ error: { code: 'video_not_found', message: 'Video not found: x.mp4' } }, 404);
    const result = await execute(videoTool('video_meta'), {
      video_path: path.join(workspaceDir, 'x.mp4'),
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('video_not_found');
    expect(result.output).toContain('Video not found');
  });

  it('maps network failure to toolserver_unreachable', async () => {
    fetchMock.mockRejectedValueOnce(new Error('connect ECONNREFUSED'));
    const result = await execute(videoTool('video_meta'), {
      video_path: path.join(workspaceDir, 'x.mp4'),
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('toolserver_unreachable');
  });

  it('hard-vetoes a relative path escaping the workspace', async () => {
    const result = await execute(videoTool('video_meta'), { video_path: '../outside.mp4' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('hard-vetoes an ABSOLUTE path outside the workspace', async () => {
    const result = await execute(videoTool('video_meta'), {
      video_path: path.join(os.tmpdir(), 'elsewhere.mp4'),
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('extract_frames', () => {
  it('sends clamped max_frames and converts jpeg_base64 frames to image parts', async () => {
    mockToolserver({
      frames: [
        { timestamp: 0, jpeg_base64: FAKE_JPEG_BASE64, width: 640, height: 360 },
        { timestamp: 5, jpeg_base64: FAKE_JPEG_BASE64, width: 640, height: 360 },
      ],
    });
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const result = await execute(videoTool('extract_frames'), {
      video_path: videoPath,
      timestamps: [0, 5],
      max_frames: 99, // clamped to 8
    });
    expect(result.isError).toBeFalsy();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: videoPath,
      timestamps: [0, 5],
      max_frames: 8,
    });

    const parts = result.output as ContentPart[];
    const imageParts = parts.filter((part) => part.type === 'image_url');
    expect(imageParts).toHaveLength(2);
    for (const part of imageParts) {
      if (part.type !== 'image_url') throw new Error('unreachable');
      expect(part.imageUrl.url).toBe(`data:image/jpeg;base64,${FAKE_JPEG_BASE64}`);
    }
    expect(parts.some((part) => part.type === 'text' && part.text.includes('@ 5s'))).toBe(true);
  });

  it('returns isError when the toolserver yields zero frames', async () => {
    mockToolserver({ frames: [] });
    const result = await execute(videoTool('extract_frames'), {
      video_path: path.join(workspaceDir, 'demo.mp4'),
    });
    expect(result.isError).toBe(true);
  });
});

describe('draw_boxes', () => {
  it('posts normalized boxes and returns the annotated image part', async () => {
    mockToolserver({ jpeg_base64: FAKE_JPEG_BASE64, width: 640, height: 360 });
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const boxes = [{ x1: 0.1, y1: 0.2, x2: 0.3, y2: 0.4, label: '疑似行人' }];
    const result = await execute(videoTool('draw_boxes'), {
      video_path: videoPath,
      timestamp: 3.2,
      boxes,
    });
    expect(result.isError).toBeFalsy();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: videoPath,
      timestamp: 3.2,
      boxes,
    });

    const parts = result.output as ContentPart[];
    const image = parts.find((part) => part.type === 'image_url');
    expect(image).toBeDefined();
    if (image?.type !== 'image_url') throw new Error('unreachable');
    expect(image.imageUrl.url).toBe(`data:image/jpeg;base64,${FAKE_JPEG_BASE64}`);
  });

  it('rejects out-of-range box coordinates', async () => {
    const result = await execute(videoTool('draw_boxes'), {
      video_path: path.join(workspaceDir, 'demo.mp4'),
      timestamp: 1,
      boxes: [{ x1: 0.1, y1: 0.2, x2: 1.5, y2: 0.4 }],
    });
    expect(result.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('read_file / write_file', () => {
  it('write_file writes inside the workspace and read_file reads it back', async () => {
    const write = await execute(fileTool('write_file'), {
      path: 'notes/a.txt',
      content: '你好,沙盒',
    });
    expect(write.isError).toBeFalsy();
    expect(JSON.parse(write.output as string)).toMatchObject({
      path: path.join(workspaceDir, 'notes/a.txt'),
    });
    expect(readFileSync(path.join(workspaceDir, 'notes/a.txt'), 'utf8')).toBe('你好,沙盒');

    const read = await execute(fileTool('read_file'), { path: 'notes/a.txt' });
    expect(read.isError).toBeFalsy();
    expect(JSON.parse(read.output as string).content).toBe('你好,沙盒');
  });

  it('write_file declares a file write access (permission chain trigger)', () => {
    const execution = runnable(fileTool('write_file'), { path: 'x.txt', content: 'y' });
    expect(execution.accesses).toEqual([
      {
        kind: 'file',
        operation: 'write',
        path: path.join(workspaceDir, 'x.txt'),
        recursive: undefined,
      },
    ]);
  });

  it('hard-vetoes writes escaping the workspace', async () => {
    const result = await execute(fileTool('write_file'), {
      path: '../evil.txt',
      content: 'x',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
  });

  it('hard-vetoes ABSOLUTE paths outside the workspace (read and write)', async () => {
    const outsideDir = mkdtempSync(path.join(os.tmpdir(), 'outside-ws-'));
    try {
      const outsideFile = path.join(outsideDir, 'abs.txt');
      writeFileSync(outsideFile, 'secret');

      const read = await execute(fileTool('read_file'), { path: outsideFile });
      expect(read.isError).toBe(true);
      expect(read.output).toContain('PATH_OUTSIDE_WORKSPACE');

      const write = await execute(fileTool('write_file'), {
        path: path.join(outsideDir, 'new.txt'),
        content: 'x',
      });
      expect(write.isError).toBe(true);
      expect(write.output).toContain('PATH_OUTSIDE_WORKSPACE');

      const script = await execute(fileTool('run_script'), { path: outsideFile });
      expect(script.isError).toBe(true);
      expect(script.output).toContain('PATH_OUTSIDE_WORKSPACE');
    } finally {
      rmSync(outsideDir, { recursive: true, force: true });
    }
  });

  it('allows absolute paths inside additionalDirs', async () => {
    const extraDir = mkdtempSync(path.join(os.tmpdir(), 'extra-dir-'));
    try {
      const extraFile = path.join(extraDir, 'note.txt');
      const extraWorkspace: WorkspaceConfig = {
        workspaceDir,
        additionalDirs: [extraDir],
      };
      const tools = createFileTools(extraWorkspace, (n) => `desc:${n}`);
      const write = tools.find((t) => t.name === 'write_file');
      const read = tools.find((t) => t.name === 'read_file');
      if (!write || !read) throw new Error('tools not found');

      const written = await execute(write, { path: extraFile, content: 'in extra dir' });
      expect(written.isError).toBeFalsy();
      expect(readFileSync(extraFile, 'utf8')).toBe('in extra dir');

      const readBack = await execute(read, { path: extraFile });
      expect(readBack.isError).toBeFalsy();
      expect(JSON.parse(readBack.output as string).content).toBe('in extra dir');
    } finally {
      rmSync(extraDir, { recursive: true, force: true });
    }
  });

  it('hard-vetoes sensitive files', async () => {
    const result = await execute(fileTool('read_file'), { path: '.env' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_SENSITIVE');
  });

  it('returns isError when reading a missing file', async () => {
    const result = await execute(fileTool('read_file'), { path: 'missing.txt' });
    expect(result.isError).toBe(true);
  });
});

describe('run_script', () => {
  it('runs a bash script inside the workspace and captures output', async () => {
    writeFileSync(path.join(workspaceDir, 'hello.sh'), 'echo "hello $1"\n');
    const result = await execute(fileTool('run_script'), {
      path: 'hello.sh',
      args: ['world'],
    });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.exit_code).toBe(0);
    expect(parsed.stdout).toContain('hello world');
  });

  it('runs with the workspace as cwd', async () => {
    writeFileSync(path.join(workspaceDir, 'pwd.sh'), 'pwd\n');
    const result = await execute(fileTool('run_script'), { path: 'pwd.sh' });
    expect(result.isError).toBeFalsy();
    expect(JSON.parse(result.output as string).stdout.trim()).toBe(workspaceDir);
  });

  it('returns the non-zero exit code with stderr (not isError)', async () => {
    writeFileSync(path.join(workspaceDir, 'fail.sh'), 'echo boom >&2\nexit 3\n');
    const result = await execute(fileTool('run_script'), { path: 'fail.sh' });
    expect(result.isError).toBeFalsy();
    const parsed = JSON.parse(result.output as string);
    expect(parsed.exit_code).toBe(3);
    expect(parsed.stderr).toContain('boom');
  });

  it('kills scripts exceeding the timeout', async () => {
    writeFileSync(path.join(workspaceDir, 'slow.sh'), 'sleep 30\n');
    const result = await execute(fileTool('run_script'), {
      path: 'slow.sh',
      timeout_sec: 1,
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('超时');
  }, 15000);

  it('rejects unsupported script extensions at resolve time', async () => {
    writeFileSync(path.join(workspaceDir, 'notes.txt'), 'echo hi\n');
    const result = await execute(fileTool('run_script'), { path: 'notes.txt' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('不支持的脚本类型');
  });

  it('hard-vetoes scripts outside the workspace', async () => {
    const result = await execute(fileTool('run_script'), { path: '../evil.sh' });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
  });

  it('declares a read access and an approvalRule containing the script path', () => {
    writeFileSync(path.join(workspaceDir, 'a.sh'), 'true\n');
    const execution = runnable(fileTool('run_script'), { path: 'a.sh' });
    const scriptPath = path.join(workspaceDir, 'a.sh');
    expect(execution.accesses).toEqual([
      { kind: 'file', operation: 'read', path: scriptPath, recursive: undefined },
    ]);
    expect(execution.approvalRule).toContain(scriptPath);
  });
});

describe('submit_detection', () => {
  const tool = (): ExecutableTool =>
    createSubmitDetectionTool('提交检测结果', loadSubmitDetectionSchema());

  function baseEvents(): Array<Record<string, unknown>> {
    return [1, 2, 3, 4, 5, 6, 7, 8, 10, 11].map((id) => ({
      event_id: id,
      detected: false,
      confidence: 0.1,
      instances: [],
      reasoning: '全片检查未见该事件',
      evidence_frames: [],
    }));
  }

  it('accepts a consistent all-zero (normal) submission with stopTurn', async () => {
    const result = await execute(tool(), {
      events: baseEvents(),
      binary_encoding: '0_0_0_0_0_0_0_0_0_0_0',
      normal: true,
      report_markdown: '# 检测报告\n未检出任何事件。',
    });
    expect(result.isError).toBeFalsy();
    expect(result.stopTurn).toBe(true);
    expect(result.output).toBe('检测结果已提交');
    const payload = JSON.parse(result.note as string);
    expect(payload.binary_encoding).toBe('0_0_0_0_0_0_0_0_0_0_0');
    expect(payload.events).toHaveLength(10);
  });

  it('accepts a consistent detection and carries the structured payload', async () => {
    const events = baseEvents();
    events[2] = {
      event_id: 3,
      detected: true,
      confidence: 0.9,
      instances: [
        { description: '白色小客车停靠应急车道', location: '画面右侧', start_sec: 2, end_sec: 8 },
      ],
      reasoning: '第 3-8 秒可见静止车辆',
      evidence_frames: [3.0],
    };
    const result = await execute(tool(), {
      events,
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 检测报告\n检出事件 3。',
    });
    expect(result.isError).toBeFalsy();
    expect(result.stopTurn).toBe(true);
    expect(JSON.parse(result.note as string).events[2].detected).toBe(true);
  });

  it('rejects when a set bit contradicts events.detected', async () => {
    const result = await execute(tool(), {
      events: baseEvents(),
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('位 3');
    expect(result.output).toContain('detected=false');
  });

  it('rejects when normal contradicts the encoding', async () => {
    const events = baseEvents();
    events[0] = { ...events[0], detected: true, evidence_frames: [1.0] };
    const result = await execute(tool(), {
      events,
      binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
      normal: true,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('normal=true');
  });

  it('rejects detected events without evidence frames', async () => {
    const events = baseEvents();
    events[0] = { ...events[0], detected: true, evidence_frames: [] };
    const result = await execute(tool(), {
      events,
      binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('evidence_frames 为空');
  });

  it('rejects string evidence frames (timestamps in seconds only)', async () => {
    const events = baseEvents();
    events[0] = { ...events[0], detected: true, evidence_frames: ['frame_3s.jpg'] };
    const result = await execute(tool(), {
      events,
      binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
  });

  it('rejects an encoding with bit 9 set (schema pattern)', async () => {
    const result = await execute(tool(), {
      events: baseEvents(),
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: false,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
  });

  it('inlines the submit_detection.schema.json as tool parameters', () => {
    const parameters = tool().parameters;
    expect(parameters['$ref']).toBeUndefined();
    expect(parameters['required']).toEqual(
      expect.arrayContaining(['events', 'binary_encoding', 'normal', 'report_markdown']),
    );
  });
});

describe('registerBuiltinTools', () => {
  it('registers all seven builtin tools', () => {
    const registry = new ToolRegistry();
    const tools = registerBuiltinTools(registry, { workspaceDir });
    expect(tools).toHaveLength(7);
    expect(registry.list().map((tool) => tool.name).sort()).toEqual(
      [
        'draw_boxes',
        'extract_frames',
        'read_file',
        'run_script',
        'submit_detection',
        'video_meta',
        'write_file',
      ].sort(),
    );
  });

  it('takes model-facing descriptions from toolset.json', () => {
    const registry = new ToolRegistry();
    registerBuiltinTools(registry, { workspaceDir });
    expect(registry.resolve('video_meta')?.description).toContain('元信息');
    expect(registry.resolve('submit_detection')?.description).toContain('提交');
  });
});
