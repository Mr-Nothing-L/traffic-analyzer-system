/**
 * Unit tests for agent/src/tools/builtin. All toolserver traffic is mocked
 * (stubbed global fetch); file/script tools run against a tmp workspace.
 * No real model API or toolserver process is touched.
 */
import { mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ContentPart } from '../../llm/kosong';
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
import { registerBuiltinTools, expandToolsetParameters } from './index';
import { createSubmitDetectionTool, loadSubmitDetectionSchema, type DetectionPayload } from './submitDetection';
import { loadEventContract, type ToolsetEntrySpec } from './eventContract';
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
  client = new ToolserverClient({ baseUrl: 'http://127.0.0.1:8601', fetchImpl: fetchMock });
});

afterEach(() => {
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

/** 直连工厂用的最小 toolset spec(description/parameters 占位,真实值由 toolset.json 提供)。 */
function spec(name: string): ToolsetEntrySpec {
  return { description: `desc:${name}`, parameters: { type: 'object', properties: {} } };
}

function videoTool(name: string): ExecutableTool {
  const tools = createVideoTools(client, workspace, (n) => spec(n));
  const tool = tools.find((t) => t.name === name);
  if (!tool) throw new Error(`tool ${name} not found`);
  return tool;
}

function fileTool(name: string): ExecutableTool {
  const tools = createFileTools(workspace, (n) => spec(n));
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
  it('passes timestamps and max_frames through verbatim (toolserver clamps)', async () => {
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
      max_frames: 99, // TS 不再 clamp,toolserver 按模式上限截断
    });
    expect(result.isError).toBeFalsy();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: videoPath,
      timestamps: [0, 5],
      max_frames: 99,
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

  it('passes fps and max_frames through without clamping (toolserver owns the cap)', async () => {
    mockToolserver({
      frames: [
        { timestamp: 0, jpeg_base64: FAKE_JPEG_BASE64, width: 640, height: 360 },
      ],
    });
    const videoPath = path.join(workspaceDir, 'demo.mp4');
    const result = await execute(videoTool('extract_frames'), {
      video_path: videoPath,
      fps: 1,
      max_frames: 999, // toolserver 侧截到 fps 模式上限 120
    });
    expect(result.isError).toBeFalsy();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: videoPath,
      fps: 1,
      max_frames: 999,
    });
  });

  it('omits absent params; forwards provided mode params as-is (no default injection)', async () => {
    mockToolserver({ frames: [] });
    await execute(videoTool('extract_frames'), {
      video_path: path.join(workspaceDir, 'demo.mp4'),
      timestamps: [1, 2],
      fps: 1,
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // 未提供的 max_frames/count 不发送:模式默认值由 toolserver 决定
    expect(JSON.parse(init.body as string)).toEqual({
      video_path: path.join(workspaceDir, 'demo.mp4'),
      timestamps: [1, 2],
      fps: 1,
    });
  });

  it('rejects an out-of-range fps', async () => {
    const result = await execute(videoTool('extract_frames'), {
      video_path: path.join(workspaceDir, 'demo.mp4'),
      fps: 10,
    });
    expect(result.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('prepends a truncation note when the toolserver truncates', async () => {
    mockToolserver({
      frames: [
        { timestamp: 0, jpeg_base64: FAKE_JPEG_BASE64, width: 640, height: 360 },
      ],
      truncated: true,
    });
    const result = await execute(videoTool('extract_frames'), {
      video_path: path.join(workspaceDir, 'demo.mp4'),
      fps: 1,
    });
    expect(result.isError).toBeFalsy();
    const parts = result.output as ContentPart[];
    const first = parts[0];
    expect(first?.type).toBe('text');
    if (first?.type !== 'text') throw new Error('unreachable');
    expect(first.text).toContain('截断');
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

  it('hard-vetoes a workspace symlink whose target is outside the workspace', async () => {
    const outsideDir = mkdtempSync(path.join(os.tmpdir(), 'symlink-target-'));
    try {
      const secretFile = path.join(outsideDir, 'secret.txt');
      writeFileSync(secretFile, 'secret');
      symlinkSync(secretFile, path.join(workspaceDir, 'leak.txt'));

      const read = await execute(fileTool('read_file'), { path: 'leak.txt' });
      expect(read.isError).toBe(true);
      expect(read.output).toContain('PATH_OUTSIDE_WORKSPACE');
      expect(read.output).toContain('resolves through symlinks');

      const evilScript = path.join(outsideDir, 'evil.sh');
      writeFileSync(evilScript, 'echo pwned\n');
      symlinkSync(evilScript, path.join(workspaceDir, 'evil.sh'));
      const script = await execute(fileTool('run_script'), { path: 'evil.sh' });
      expect(script.isError).toBe(true);
      expect(script.output).toContain('PATH_OUTSIDE_WORKSPACE');
    } finally {
      rmSync(outsideDir, { recursive: true, force: true });
    }
  });

  it('allows a symlink whose physical target stays inside additionalDirs', async () => {
    const extraDir = mkdtempSync(path.join(os.tmpdir(), 'extra-symlink-'));
    try {
      const target = path.join(extraDir, 'note.txt');
      writeFileSync(target, 'via symlink');
      symlinkSync(target, path.join(workspaceDir, 'alias.txt'));

      const extraWorkspace: WorkspaceConfig = { workspaceDir, additionalDirs: [extraDir] };
      const read = createFileTools(extraWorkspace, (n) => `desc:${n}`).find(
        (t) => t.name === 'read_file',
      );
      if (!read) throw new Error('read_file not found');

      const result = await execute(read, { path: 'alias.txt' });
      expect(result.isError).toBeFalsy();
      expect(JSON.parse(result.output as string).content).toBe('via symlink');
    } finally {
      rmSync(extraDir, { recursive: true, force: true });
    }
  });

  it('keeps working when the workspace directory itself is behind a symlink', async () => {
    const realDir = mkdtempSync(path.join(os.tmpdir(), 'real-ws-'));
    const linkDir = path.join(os.tmpdir(), `link-ws-${process.pid}-${Date.now()}`);
    symlinkSync(realDir, linkDir);
    try {
      writeFileSync(path.join(realDir, 'a.txt'), 'ok');
      const linkedWorkspace: WorkspaceConfig = { workspaceDir: linkDir, additionalDirs: [] };
      const read = createFileTools(linkedWorkspace, (n) => `desc:${n}`).find(
        (t) => t.name === 'read_file',
      );
      if (!read) throw new Error('read_file not found');

      const result = await execute(read, { path: 'a.txt' });
      expect(result.isError).toBeFalsy();
      expect(JSON.parse(result.output as string).content).toBe('ok');
    } finally {
      rmSync(realDir, { recursive: true, force: true });
      rmSync(linkDir, { force: true });
    }
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

  it('declares an execute-level (all) access and an approvalRule containing the script path', () => {
    writeFileSync(path.join(workspaceDir, 'a.sh'), 'true\n');
    const execution = runnable(fileTool('run_script'), { path: 'a.sh' });
    const scriptPath = path.join(workspaceDir, 'a.sh');
    expect(execution.accesses).toEqual([{ kind: 'all' }]);
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

  it('accepts a normal submission (bit 9=1, normal=true) with stopTurn', async () => {
    const result = await execute(tool(), {
      video_path: 'demo.mp4',
      events: baseEvents(),
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: true,
      report_markdown: '# 检测报告\n未检出任何事件。',
    });
    expect(result.isError).toBeFalsy();
    expect(result.stopTurn).toBe(true);
    expect(result.output).toBe('检测结果已提交');
    // 结构化载荷走 payload 一等字段,不再是 note JSON 字符串。
    expect(result.note).toBeUndefined();
    const payload = result.payload as DetectionPayload;
    expect(payload.binary_encoding).toBe('0_0_0_0_0_0_0_0_1_0_0');
    expect(payload.normal).toBe(true);
    expect(payload.events).toHaveLength(10);
    expect(payload.events.every((event) => typeof event.event_id === 'number')).toBe(true);
    // 无检出事件时不产生 meta。
    expect(payload.meta).toBeUndefined();
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
      video_path: 'demo.mp4',
      events,
      binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 检测报告\n检出事件 3。',
    });
    expect(result.isError).toBeFalsy();
    expect(result.stopTurn).toBe(true);
    expect((result.payload as DetectionPayload).events[2]?.detected).toBe(true);
  });

  it('tolerates events passed as a JSON string (qwen3_xml quirk)', async () => {
    const events = baseEvents();
    events[0] = {
      event_id: 1,
      detected: true,
      confidence: 0.9,
      instances: [
        { description: '大型货车停靠应急车道', location: '画面右侧', start_sec: 0, end_sec: 19 },
      ],
      reasoning: '全片可见静止货车',
      evidence_frames: [2, 7, 12],
    };
    // 模型把数组包成字符串(带前导换行),容错反序列化后应正常通过。
    const result = await execute(tool(), {
      video_path: 'demo.mp4',
      events: '\n' + JSON.stringify(events),
      binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 检测报告\n检出事件 1。',
    });
    expect(result.isError).toBeFalsy();
    expect(result.stopTurn).toBe(true);
    const payload = result.payload as DetectionPayload;
    expect(payload.events[0]?.detected).toBe(true);
  });

  it('tolerates the whole input passed as a JSON string', async () => {
    const input = {
      video_path: 'demo.mp4',
      events: baseEvents(),
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: true,
      report_markdown: '# 报告',
    };
    const result = await execute(tool(), JSON.stringify(input) as unknown as Record<string, unknown>);
    expect(result.isError).toBeFalsy();
    expect(result.stopTurn).toBe(true);
  });

  it('rejects when a set bit contradicts events.detected', async () => {
    const result = await execute(tool(), {
      video_path: 'demo.mp4',
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
      video_path: 'demo.mp4',
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
      video_path: 'demo.mp4',
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
      video_path: 'demo.mp4',
      events,
      binary_encoding: '1_0_0_0_0_0_0_0_0_0_0',
      normal: false,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
  });

  it('rejects when bit 9 disagrees with normal (bit 9=1 but normal=false)', async () => {
    const result = await execute(tool(), {
      video_path: 'demo.mp4',
      events: baseEvents(),
      binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
      normal: false,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('位 9');
    expect(result.output).toContain('normal=false');
  });

  it('rejects an all-zero encoding claimed as normal (bit 9 must be 1)', async () => {
    const result = await execute(tool(), {
      video_path: 'demo.mp4',
      events: baseEvents(),
      binary_encoding: '0_0_0_0_0_0_0_0_0_0_0',
      normal: true,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('位 9 应为 1');
  });

  it('rejects bit 9=1 when an event is detected', async () => {
    const events = baseEvents();
    events[0] = { ...events[0], detected: true, evidence_frames: [1.0] };
    const result = await execute(tool(), {
      video_path: 'demo.mp4',
      events,
      binary_encoding: '1_0_0_0_0_0_0_0_1_0_0',
      normal: true,
      report_markdown: '# 报告',
    });
    expect(result.isError).toBe(true);
    expect(result.output).toContain('位 9 应为 0');
  });

  it('inlines the submit_detection.schema.json as tool parameters', () => {
    const parameters = tool().parameters;
    expect(parameters['$ref']).toBeUndefined();
    expect(parameters['required']).toEqual(
      expect.arrayContaining(['video_path', 'events', 'binary_encoding', 'normal', 'report_markdown']),
    );
  });

  describe('per-event annotated images', () => {
    const annotatingTool = (): ExecutableTool =>
      createSubmitDetectionTool('提交检测结果', loadSubmitDetectionSchema(), {
        client,
        workspace,
      });

    function eventsWithDetected(extra: Record<string, unknown>): Array<Record<string, unknown>> {
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
        ...extra,
      };
      return events;
    }

    it('annotates a detected event with boxes via toolserver draw_boxes', async () => {
      mockToolserver({ jpeg_base64: FAKE_JPEG_BASE64, width: 640, height: 360 });
      const videoPath = path.join(workspaceDir, 'demo.mp4');
      const boxes = [{ x1: 0.5, y1: 0.5, x2: 0.7, y2: 0.8, label: '白色小客车' }];
      const result = await execute(annotatingTool(), {
        video_path: videoPath,
        // boxes 包成 JSON 字符串也应被容错反序列化(qwen3_xml 怪癖)。
        events: eventsWithDetected({ boxes: JSON.stringify(boxes), box_frame: 3.0 }),
        binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 检测报告\n检出事件 3。',
      });
      expect(result.isError).toBeFalsy();
      expect(result.stopTurn).toBe(true);

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe('http://127.0.0.1:8601/tools/draw_boxes');
      expect(JSON.parse(init.body as string)).toEqual({
        video_path: videoPath,
        timestamp: 3.0,
        boxes,
      });

      const payload = result.payload as DetectionPayload;
      expect(payload.events[2]?.annotated_image).toBe(
        `data:image/jpeg;base64,${FAKE_JPEG_BASE64}`,
      );
      expect(payload.meta).toBeUndefined();
    });

    it('declares a read access on the sandbox-resolved video path', () => {
      const videoPath = path.join(workspaceDir, 'demo.mp4');
      const execution = runnable(annotatingTool(), {
        video_path: videoPath,
        events: eventsWithDetected({
          boxes: [{ x1: 0.1, y1: 0.1, x2: 0.2, y2: 0.2 }],
          box_frame: 3.0,
        }),
        binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 报告',
      });
      expect(execution.accesses).toEqual([
        { kind: 'file', operation: 'read', path: videoPath, recursive: undefined },
      ]);
    });

    it('hard-vetoes a video_path outside the workspace', async () => {
      const result = await execute(annotatingTool(), {
        video_path: '../outside.mp4',
        events: eventsWithDetected({
          boxes: [{ x1: 0.1, y1: 0.1, x2: 0.2, y2: 0.2 }],
          box_frame: 3.0,
        }),
        binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 报告',
      });
      expect(result.isError).toBe(true);
      expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it('hard-vetoes an ABSOLUTE video_path outside the workspace (previously allowed)', async () => {
      const outsideDir = mkdtempSync(path.join(os.tmpdir(), 'submit-outside-'));
      try {
        const result = await execute(annotatingTool(), {
          video_path: path.join(outsideDir, 'elsewhere.mp4'),
          events: eventsWithDetected({
            boxes: [{ x1: 0.1, y1: 0.1, x2: 0.2, y2: 0.2 }],
            box_frame: 3.0,
          }),
          binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
          normal: false,
          report_markdown: '# 报告',
        });
        expect(result.isError).toBe(true);
        expect(result.output).toContain('PATH_OUTSIDE_WORKSPACE');
        expect(fetchMock).not.toHaveBeenCalled();
      } finally {
        rmSync(outsideDir, { recursive: true, force: true });
      }
    });

    it('degrades when draw_boxes fails: submission stands, meta.annotation_failed records the event', async () => {
      mockToolserver({ error: { code: 'frame_unavailable', message: 'no frame at 3s' } }, 404);
      const result = await execute(annotatingTool(), {
        video_path: path.join(workspaceDir, 'demo.mp4'),
        events: eventsWithDetected({
          boxes: [{ x1: 0.5, y1: 0.5, x2: 0.7, y2: 0.8 }],
          box_frame: 3.0,
        }),
        binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 报告',
      });
      expect(result.isError).toBeFalsy();
      expect(result.stopTurn).toBe(true);
      const payload = result.payload as DetectionPayload;
      expect(payload.events[2]?.annotated_image).toBeUndefined();
      expect(payload.meta?.annotation_failed).toEqual([3]);
    });

    it('degrades when the toolserver is unreachable', async () => {
      fetchMock.mockRejectedValueOnce(new Error('connect ECONNREFUSED'));
      const result = await execute(annotatingTool(), {
        video_path: path.join(workspaceDir, 'demo.mp4'),
        events: eventsWithDetected({
          boxes: [{ x1: 0.5, y1: 0.5, x2: 0.7, y2: 0.8 }],
          box_frame: 3.0,
        }),
        binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 报告',
      });
      expect(result.isError).toBeFalsy();
      const payload = result.payload as DetectionPayload;
      expect(payload.meta?.annotation_failed).toEqual([3]);
    });

    it('soft-records detected events without boxes/box_frame under meta.missing_boxes (no draw call, no rejection)', async () => {
      const result = await execute(annotatingTool(), {
        video_path: path.join(workspaceDir, 'demo.mp4'),
        events: eventsWithDetected({}),
        binary_encoding: '0_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 报告',
      });
      expect(result.isError).toBeFalsy();
      expect(result.stopTurn).toBe(true);
      expect(fetchMock).not.toHaveBeenCalled();
      const payload = result.payload as DetectionPayload;
      expect(payload.events[2]?.annotated_image).toBeUndefined();
      expect(payload.meta?.missing_boxes).toEqual([3]);
    });

    it('lists both levels side by side: missing_boxes for the bare event, annotation_failed for the failed draw', async () => {
      // 事件 1 检出但完全没给框(missing_boxes),事件 3 给了框但 draw 失败(annotation_failed)。
      mockToolserver({ error: { code: 'frame_unavailable', message: 'no frame at 3s' } }, 404);
      const events = baseEvents();
      events[0] = { ...events[0], detected: true, evidence_frames: [1.5] };
      events[2] = {
        ...events[0],
        event_id: 3,
        confidence: 0.9,
        reasoning: '第 3-8 秒可见静止车辆',
        evidence_frames: [3.0],
        boxes: [{ x1: 0.1, y1: 0.1, x2: 0.4, y2: 0.4 }],
        box_frame: 3.0,
      };
      const result = await execute(annotatingTool(), {
        video_path: path.join(workspaceDir, 'demo.mp4'),
        events,
        binary_encoding: '1_0_1_0_0_0_0_0_0_0_0',
        normal: false,
        report_markdown: '# 报告',
      });
      expect(result.isError).toBeFalsy();
      expect(result.stopTurn).toBe(true);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const payload = result.payload as DetectionPayload;
      expect(payload.events[0]?.annotated_image).toBeUndefined();
      expect(payload.events[2]?.annotated_image).toBeUndefined();
      // 两级元信息在同一份 payload 并存,逐事件列出 id 便于定位缺框原因。
      expect(payload.meta).toEqual({
        missing_boxes: [1],
        annotation_failed: [3],
      });
    });
  });

  describe('resolver convergence (single strict boundary)', () => {
    interface Verdict {
      readonly runnable: boolean;
      readonly code?: string;
    }

    function verdict(tool: ExecutableTool, input: unknown): Verdict {
      const execution = (tool.resolveExecution as (i: unknown) => unknown)(input);
      if (isRunnableToolExecution(execution as never)) return { runnable: true };
      const output = (execution as ExecutableToolErrorResult).output;
      const match = typeof output === 'string' ? /\[(PATH_[A-Z_]+)\]/.exec(output) : undefined;
      return { runnable: false, code: match?.[1] };
    }

    function submitInput(videoPath: string): Record<string, unknown> {
      return {
        video_path: videoPath,
        events: baseEvents(),
        binary_encoding: '0_0_0_0_0_0_0_0_1_0_0',
        normal: true,
        report_markdown: '# 报告',
      };
    }

    it('the same path yields the same verdict across read_file / video_meta / submit_detection', () => {
      const submitTool = createSubmitDetectionTool('提交检测结果', loadSubmitDetectionSchema(), {
        client,
        workspace,
      });
      const cases: ReadonlyArray<{ path: string; expected: Verdict }> = [
        { path: path.join(workspaceDir, 'demo.mp4'), expected: { runnable: true } },
        {
          path: path.join(os.tmpdir(), 'elsewhere', 'x.mp4'),
          expected: { runnable: false, code: 'PATH_OUTSIDE_WORKSPACE' },
        },
        {
          path: '../outside.mp4',
          expected: { runnable: false, code: 'PATH_OUTSIDE_WORKSPACE' },
        },
        { path: '.env', expected: { runnable: false, code: 'PATH_SENSITIVE' } },
        { path: '', expected: { runnable: false, code: 'PATH_INVALID' } },
      ];

      for (const { path: rawPath, expected } of cases) {
        expect(verdict(fileTool('read_file'), { path: rawPath })).toEqual(expected);
        expect(verdict(videoTool('video_meta'), { video_path: rawPath })).toEqual(expected);
        expect(verdict(submitTool, submitInput(rawPath))).toEqual(expected);
      }
    });
  });
});

describe('registerBuiltinTools', () => {
  it('registers all eighteen builtin tools', () => {
    const registry = new ToolRegistry();
    const tools = registerBuiltinTools(registry, { workspaceDir });
    expect(tools).toHaveLength(18);
    expect(registry.list().map((tool) => tool.name).sort()).toEqual(
      [
        'draw_boxes',
        'edit_file',
        'extract_frames',
        'glob_files',
        'grep_files',
        'job_kill',
        'job_list',
        'job_output',
        'load_video',
        'read_file',
        'run_command',
        'run_script',
        'submit_detection',
        'todo_write',
        'track_suspects',
        'video_meta',
        'web_fetch',
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

  it('feeds toolset.json parameters as the model-visible schema (verbatim)', () => {
    const registry = new ToolRegistry();
    registerBuiltinTools(registry, { workspaceDir });
    const toolset = JSON.parse(
      readFileSync(
        fileURLToPath(new URL('../../../config/toolset.json', import.meta.url)),
        'utf8',
      ),
    ) as { tools: Array<{ name: string; description: string; parameters: Record<string, unknown> }> };
    for (const entry of toolset.tools) {
      if (entry.name === 'subagent_list' || entry.name === 'subagent_report') {
        // 这两个工具由 server/app.ts 按 session 注入,registerBuiltinTools 不注册,
        // 但 toolset.json 仍保留它们的描述/参数契约,保证 app.ts 加载时可用。
        continue;
      }
      const tool = registry.resolve(entry.name);
      expect(tool, entry.name).toBeDefined();
      expect(tool?.description).toBe(entry.description);
      if (entry.name === 'submit_detection') continue; // enum/pattern 由 event_contract 注入,单独断言
      expect(tool?.parameters).toEqual(entry.parameters);
    }
  });

  it('derives submit_detection event enum and encoding pattern from event_contract.json', () => {
    const registry = new ToolRegistry();
    registerBuiltinTools(registry, { workspaceDir });
    const parameters = registry.resolve('submit_detection')?.parameters as Record<string, any>;
    const contract = loadEventContract();
    const eventId = parameters.properties.events.items.properties.event_id;
    expect(eventId.enum).toEqual([...contract.active_event_ids]);
    expect(parameters.properties.binary_encoding.pattern).toBe(
      `^[01]${'_[01]'.repeat(contract.encoding_length - 1)}$`,
    );
    expect(parameters.properties.events.description).toContain(
      `${contract.active_event_ids.length} 个活跃事件编号`,
    );
  });
});

describe('toolset / event contract drift guards', () => {
  it('submit_detection.schema.json 静态枚举/位宽与 event_contract.json 一致(生成物派生,文件值防漂移)', () => {
    const raw = JSON.parse(
      readFileSync(
        fileURLToPath(new URL('../../../config/submit_detection.schema.json', import.meta.url)),
        'utf8',
      ),
    ) as Record<string, any>;
    const contract = loadEventContract();
    expect(raw.properties.events.items.properties.event_id.enum).toEqual([
      ...contract.active_event_ids,
    ]);
    expect(raw.properties.binary_encoding.pattern).toBe(
      `^[01]${'_[01]'.repeat(contract.encoding_length - 1)}$`,
    );
  });

  it('expandToolsetParameters 解析任意 ./xxx.json 相对引用(相对 agent/config/)', () => {
    const entry = {
      name: 'submit_detection',
      parameters: { $ref: './submit_detection.schema.json' },
    };
    const expanded = expandToolsetParameters(entry);
    expect(expanded['$ref']).toBeUndefined();
    expect(expanded['required']).toEqual(
      expect.arrayContaining(['video_path', 'events', 'binary_encoding', 'normal', 'report_markdown']),
    );
  });

  it('expandToolsetParameters 拒绝 config 目录之外的 $ref 形式', () => {
    for (const ref of ['../secrets.json', '/etc/passwd', 'http://evil/x.json']) {
      expect(() =>
        expandToolsetParameters({ name: 'x', parameters: { $ref: ref } }),
      ).toThrow(/\$ref/);
    }
  });

  it('expandToolsetParameters 对缺失 parameters 的条目 fail-fast', () => {
    expect(() => expandToolsetParameters({ name: 'broken_tool' })).toThrow(/parameters/);
  });
});
