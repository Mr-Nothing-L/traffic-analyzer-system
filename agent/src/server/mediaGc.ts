/**
 * 删除会话时的引用计数式媒体 GC(挂接在 SessionManager.delete,见 session.ts)。
 *
 * media 文件内容寻址、跨会话共享(<workspace>/.agent/media/<sha256>.<ext>,
 * 见 mediaStore.ts),删会话时只清「该会话 entries/messages 引用过 且 剩余
 * 会话不再引用」的文件;盘上其他文件(如历史遗留孤儿)本次不动,仅在结果
 * 里计数供调用方记日志。调用顺序:删库前先采集被删会话的引用集,删库后再
 * 调 gcMediaAfterSessionDelete(此时全库扫描已不含被删会话,逻辑最简)。
 *
 * 容错:.agent/media 不存在静默返回;单文件删除失败(EBUSY/EPERM,如
 * sessions.db 同被其他 agent server/web 进程持有)跳过并计数,不抛出——
 * GC 失败不阻断删除主流程(调用方再兜一层 try/catch 记日志)。
 */
import { readdirSync, rmSync } from 'node:fs';
import path from 'node:path';

import { MEDIA_DIR_SEGMENTS } from './mediaStore';
import type { SessionStorage } from './storage';

export interface MediaGcResult {
  /** 实际删除的文件数。 */
  readonly deleted: number;
  /** 删除失败(容错跳过)的文件数。 */
  readonly failed: number;
  /** 盘上存在但任何会话都不引用的文件数(本次不动,仅提示)。 */
  readonly orphaned: number;
}

/**
 * 删库后的媒体 GC:deletedRefs 为被删会话 entries/messages 引用过的文件名
 * (删库前经 SessionStorage.collectMediaNames(id) 采集)。扫描剩余会话仍
 * 引用的集合,删除差集对应的文件。
 */
export function gcMediaAfterSessionDelete(
  workspaceDir: string,
  storage: SessionStorage,
  deletedRefs: ReadonlySet<string>,
): MediaGcResult {
  const mediaDir = path.join(workspaceDir, ...MEDIA_DIR_SEGMENTS);
  let files: string[];
  try {
    files = readdirSync(mediaDir);
  } catch {
    // media 目录不存在(从未落盘过图片):静默通过。
    return { deleted: 0, failed: 0, orphaned: 0 };
  }
  if (deletedRefs.size === 0) {
    return { deleted: 0, failed: 0, orphaned: files.length };
  }
  const stillReferenced = storage.collectMediaNames();
  let deleted = 0;
  let failed = 0;
  for (const name of deletedRefs) {
    if (stillReferenced.has(name)) continue;
    try {
      // force: 容忍 ENOENT(并发进程已删);EBUSY/EPERM 等抛出错下面计数跳过。
      rmSync(path.join(mediaDir, name), { force: true });
      deleted += 1;
    } catch {
      failed += 1;
    }
  }
  const referenced = new Set([...deletedRefs, ...stillReferenced]);
  const orphaned = files.filter((file) => !referenced.has(file)).length;
  return { deleted, failed, orphaned };
}
