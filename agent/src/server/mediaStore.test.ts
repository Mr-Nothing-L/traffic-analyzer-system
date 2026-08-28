/**
 * mediaStore 单元测试:内容寻址落盘(同字节同 hash 幂等)、URL 形态、
 * GET 端点文件名白名单校验。
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { mediaContentType, mediaUrl, saveMediaFile } from './mediaStore';

let workspace: string;

beforeEach(() => {
  workspace = mkdtempSync(path.join(tmpdir(), 'agent-media-test-'));
});

afterEach(() => {
  rmSync(workspace, { recursive: true, force: true });
});

/** 1x1 jpeg/png 的假字节(内容寻址只看字节本身,无需真实图片)。 */
const JPEG_BYTES = Buffer.from([0xff, 0xd8, 0xff, 0xdb, 0xab, 0xcd]);
const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x01, 0x02]);
const jpegDataUrl = `data:image/jpeg;base64,${JPEG_BYTES.toString('base64')}`;
const pngDataUrl = `data:image/png;base64,${PNG_BYTES.toString('base64')}`;

describe('saveMediaFile', () => {
  it('按 sha256 内容寻址写盘 .agent/media/<hash>.jpg;同字节重复写幂等', () => {
    const name1 = saveMediaFile(workspace, jpegDataUrl);
    expect(name1).toBe(
      `${createHash('sha256').update(JPEG_BYTES).digest('hex')}.jpg`,
    );
    const file = path.join(workspace, '.agent', 'media', name1!);
    expect(existsSync(file)).toBe(true);
    expect(readFileSync(file).equals(JPEG_BYTES)).toBe(true);

    // 同字节 → 同 hash → 同文件,不重复落盘(内容存在即跳过)。
    const name2 = saveMediaFile(workspace, jpegDataUrl);
    expect(name2).toBe(name1);
  });

  it('png 落 .png;jpeg 与 png 字节相同也因扩展名区分', () => {
    const jpg = saveMediaFile(workspace, jpegDataUrl);
    const png = saveMediaFile(workspace, pngDataUrl);
    expect(png).toBe(`${createHash('sha256').update(PNG_BYTES).digest('hex')}.png`);
    expect(jpg).not.toBe(png);
  });

  it('非 dataURL / 非白名单类型 → undefined,不写盘', () => {
    expect(saveMediaFile(workspace, 'https://example.com/a.jpg')).toBeUndefined();
    expect(saveMediaFile(workspace, 'data:image/webp;base64,AAAA')).toBeUndefined();
    expect(saveMediaFile(workspace, 'data:image/jpeg,AAAA')).toBeUndefined();
    expect(saveMediaFile(workspace, 'data:text/plain;base64,AAAA')).toBeUndefined();
    expect(existsSync(path.join(workspace, '.agent', 'media'))).toBe(false);
  });
});

describe('mediaUrl / mediaContentType', () => {
  it('URL 形态:/sessions/{id}/media/{name}', () => {
    expect(mediaUrl('s-1', 'abcd.jpg')).toBe('/sessions/s-1/media/abcd.jpg');
  });

  it('文件名白名单:64 位 hex + jpg/png 合法,其余拒绝', () => {
    const hash = 'a'.repeat(64);
    expect(mediaContentType(`${hash}.jpg`)).toBe('image/jpeg');
    expect(mediaContentType(`${hash}.png`)).toBe('image/png');
    expect(mediaContentType(`${hash}.webp`)).toBeUndefined();
    expect(mediaContentType('../secret.jpg')).toBeUndefined();
    expect(mediaContentType('..%2fsecret.jpg')).toBeUndefined();
    expect(mediaContentType(`${'A'.repeat(64)}.jpg`)).toBeUndefined();
    expect(mediaContentType(`${hash}jpg`)).toBeUndefined();
  });
});
