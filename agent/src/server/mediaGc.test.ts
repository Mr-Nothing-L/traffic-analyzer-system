/**
 * 媒体 GC(删会话连带清理,见 mediaGc.ts / SessionManager.delete)测试:
 * 共享引用删其一保留、独占引用删除后消失、media 目录不存在静默通过、
 * 孤儿文件不动。
 */
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { mediaUrl, saveMediaFile } from './mediaStore';
import { SessionManager } from './session';
import type { TimelineEntry } from './storage';

let workspace: string;
let manager: SessionManager | undefined;

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-media-gc-test-'));
});

afterEach(() => {
  manager?.close();
  manager = undefined;
  rmSync(workspace, { recursive: true, force: true });
});

/** 1x1 png 的假字节(内容寻址只看字节本身,无需真实图片)。 */
const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x03, 0x04]);
const pngDataUrl = `data:image/png;base64,${PNG_BYTES.toString('base64')}`;

/** tool 条目:output 为 image_url part,URL 形态与 sanitizeToolOutputForTransport 产出一致。 */
function toolEntryWithImage(sessionId: string, name: string): TimelineEntry {
  return {
    kind: 'tool',
    toolCallId: 'c1',
    name: 'extract_frames',
    arguments: null,
    output: [{ type: 'image_url', imageUrl: { url: mediaUrl(sessionId, name) } }],
    isError: false,
    at: Date.now(),
  };
}

function createSession(): string {
  manager ??= new SessionManager();
  return manager.create({ workspaceDir: workspace, mode: 'yolo' }).id;
}

function mediaPath(name: string): string {
  return path.join(workspace, '.agent', 'media', name);
}

describe('媒体 GC(DELETE 会话连带清理)', () => {
  it('两个会话引用同一图片:删其一文件保留,删其二文件消失(引用也存在于消息体)', () => {
    const name = saveMediaFile(workspace, pngDataUrl);
    expect(name).toBeDefined();
    const a = createSession();
    const b = createSession();
    // a 经 entries 引用;b 经 messages 引用(覆盖两处扫描源)。
    manager?.appendEntries(a, [toolEntryWithImage(a, name!)]);
    manager?.appendMessages(b, [
      {
        role: 'user',
        content: [{ type: 'image_url', imageUrl: { url: mediaUrl(b, name!) } }],
        toolCalls: [],
      },
    ]);

    expect(manager?.delete(a)).toBe(true);
    expect(existsSync(mediaPath(name!))).toBe(true);

    expect(manager?.delete(b)).toBe(true);
    expect(existsSync(mediaPath(name!))).toBe(false);
  });

  it('独占引用:删除会话后文件消失;无引用的孤儿文件不动', () => {
    const name = saveMediaFile(workspace, pngDataUrl);
    expect(name).toBeDefined();
    // 孤儿文件:合法 media 文件名但任何会话都不引用(本次 GC 不动)。
    const orphan = `${'f'.repeat(64)}.jpg`;
    writeFileSync(mediaPath(orphan), 'orphan');

    const a = createSession();
    manager?.appendEntries(a, [toolEntryWithImage(a, name!)]);

    expect(manager?.delete(a)).toBe(true);
    expect(existsSync(mediaPath(name!))).toBe(false);
    expect(existsSync(mediaPath(orphan))).toBe(true);
  });

  it('.agent/media 不存在时静默通过(会话正常删除)', () => {
    const a = createSession();
    // 引用一个从未落盘的文件名:media 目录不存在,GC 应静默跳过。
    manager?.appendEntries(a, [toolEntryWithImage(a, `${'a'.repeat(64)}.png`)]);
    expect(existsSync(path.join(workspace, '.agent', 'media'))).toBe(false);

    expect(manager?.delete(a)).toBe(true);
    expect(manager?.get(a)).toBeUndefined();
  });
});
