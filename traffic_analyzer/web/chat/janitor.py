"""Periodic cleanup of stale chat upload/generated files.

[文件说明]
作用:快速对话上传目录清理。sweep_uploads 删除 output/chat_uploads/ 下
mtime 距今超过 24h 的文件(目录保留);run_janitor 为 asyncio 后台循环
(启动时先立即扫一次,之后每 3600s 一次),由 web/app.py 的 lifespan
创建并随退出 cancel。
上游:web/app.py(lifespan 后台任务)。
下游:web/chat/paths.py(目录常量);仅文件系统。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, Union

from traffic_analyzer.web.chat import paths

logger = logging.getLogger(__name__)

MAX_AGE_SEC = 24 * 3600
INTERVAL_SEC = 3600


def sweep_uploads(
    root: Optional[Union[str, Path]] = None,
    now: Optional[float] = None,
    max_age_sec: float = MAX_AGE_SEC,
) -> int:
    """Unlink files older than ``max_age_sec`` under ``root``; dirs are kept."""
    root_path = Path(root) if root is not None else paths.UPLOAD_DIR
    if not root_path.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in root_path.rglob("*"):
        if not entry.is_file():
            continue
        try:
            if now - entry.stat().st_mtime > max_age_sec:
                entry.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("[chat-janitor] cannot remove %s: %s", entry, exc)
    if removed:
        logger.info("[chat-janitor] removed %d stale files under %s", removed, root_path)
    return removed


async def run_janitor(interval_sec: float = INTERVAL_SEC) -> None:
    """Async loop: sweep immediately, then every ``interval_sec`` seconds."""
    while True:
        try:
            sweep_uploads()
        except Exception as exc:
            logger.warning("[chat-janitor] sweep failed: %s", exc)
        await asyncio.sleep(interval_sec)
