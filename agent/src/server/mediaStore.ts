/**
 * 媒体内容寻址存储:把工具输出/检测载荷里的图片 dataURL 写盘为
 * <workspaceDir>/.agent/media/<sha256>.<ext>(目录自动创建),并给出
 * 服务端可寻址 URL(/sessions/{sessionId}/media/{name})。
 *
 * 用途:SSE 事件与时间线条目不再内联 dataURL(单图可达数 MB,一次
 * extract_frames/track_suspects 即把 history 撑到几 MB),替换为 URL 引用,
 * 前端按需经 GET /sessions/{id}/media/{name} 加载。同图片字节 → 同 hash →
 * 同文件,重复落盘幂等;写入失败由调用方回退保留原始 dataURL。
 *
 * 注意:只处理传输/落盘副本,loop 内 messages 不经过这里——模型仍收到
 * 原始 dataURL。文件名是 64 位 hex hash + 白名单扩展名,GET 端点据此
 * 校验防路径穿越。
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

/** media 相对 workspace 的固定目录(与 sessions.db 同在 .agent 下)。 */
export const MEDIA_DIR_SEGMENTS = ['.agent', 'media'];

/** 扩展名 ↔ Content-Type:支持集即白名单(工具链图片只有 jpeg/png)。 */
const CONTENT_TYPES: Record<string, string> = {
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
};

interface DataUrlImage {
  readonly ext: string;
  readonly bytes: Buffer;
}

/** 解析 dataURL 为图片字节;非 dataURL 或类型不在白名单时返回 undefined。 */
function parseDataUrlImage(dataUrl: string): DataUrlImage | undefined {
  const match = /^data:(image\/(jpeg|png));base64,([A-Za-z0-9+/=]*)$/.exec(dataUrl);
  if (match === null) return undefined;
  const mime = match[1]!;
  return {
    ext: mime === 'image/png' ? '.png' : '.jpg',
    bytes: Buffer.from(match[3]!, 'base64'),
  };
}

/**
 * dataURL → 内容寻址文件:写入 <workspaceDir>/.agent/media/<sha256><ext>
 * (已存在则跳过,幂等),返回落盘文件名;解析失败返回 undefined。
 */
export function saveMediaFile(workspaceDir: string, dataUrl: string): string | undefined {
  const image = parseDataUrlImage(dataUrl);
  if (image === undefined) return undefined;
  const hash = createHash('sha256').update(image.bytes).digest('hex');
  const name = `${hash}${image.ext}`;
  const file = path.join(workspaceDir, ...MEDIA_DIR_SEGMENTS, name);
  if (!existsSync(file)) {
    mkdirSync(path.join(workspaceDir, ...MEDIA_DIR_SEGMENTS), { recursive: true });
    writeFileSync(file, image.bytes);
  }
  return name;
}

/** media 引用 URL(GET /sessions/{id}/media/{name})。 */
export function mediaUrl(sessionId: string, name: string): string {
  return `/sessions/${sessionId}/media/${name}`;
}

/** GET 端点的文件名校验:<sha256 hex>.<白名单扩展名>;合法返回 Content-Type。 */
export function mediaContentType(name: string): string | undefined {
  const match = /^([0-9a-f]{64})(\.(jpg|png))$/.exec(name);
  return match === null ? undefined : CONTENT_TYPES[match[2]!];
}
