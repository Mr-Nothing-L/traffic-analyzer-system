"""
Disk cache and cache-key helpers for the VLM inference engine.

Provides a SQLite-backed persistent cache and deterministic cache-key
generation for VLM requests.

[文件说明]
作用:VLM 响应的磁盘缓存层。DiskCache 基于 SQLite(WAL 模式、线程本地连接)
  实现跨进程持久缓存,支持 provider/model 匹配命中与超容量 LRU 清理;
  _compute_cache_key 对 prompt 文本与图像内容做 SHA-256 生成确定性缓存键。
上游:core/vlm_engine.py(VLMInferenceEngine 在 call 中查/写磁盘缓存,
  并用 _compute_cache_key 同时服务内存缓存)。
下游:SQLite 数据库文件(磁盘缓存库,路径来自配置的 disk_cache_path);
  models/schemas.py 的 LLMResponse(序列化/反序列化缓存内容)。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from traffic_analyzer.models.schemas import LLMResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Disk Cache (cross-process persistent cache)
# ---------------------------------------------------------------------------

class DiskCache:
    """SQLite-backed persistent cache for LLM responses.

    Enables cache hits across subprocess boundaries (e.g. batch_infer)
    where each video runs in a separate process.
    """

    def __init__(self, db_path: str, max_entries: int = 2000) -> None:
        # Resolve relative paths to absolute (subprocess cwd may differ)
        self.db_path = str(Path(db_path).expanduser().resolve())
        self.max_entries = max_entries
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> "sqlite3.Connection":
        """Return a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            import sqlite3
            self._local.conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                check_same_thread=False,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        import sqlite3
        import os
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vlm_cache (
                cache_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at REAL,
                access_count INTEGER DEFAULT 1,
                last_accessed REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vlm_cache_last_accessed
            ON vlm_cache(last_accessed)
        """)
        conn.commit()
        conn.close()

    def get(self, cache_key: str, provider: str, model: str) -> Optional[LLMResponse]:
        """Retrieve a cached response if it exists and matches provider/model."""
        import sqlite3
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT response_json FROM vlm_cache WHERE cache_key = ? AND provider = ? AND model = ?",
                (cache_key, provider, model),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            # Update access stats
            now = time.time()
            conn.execute(
                "UPDATE vlm_cache SET access_count = access_count + 1, last_accessed = ? WHERE cache_key = ?",
                (now, cache_key),
            )
            conn.commit()
            try:
                data = json.loads(row[0])
                return LLMResponse(**data)
            except (ValueError, TypeError, ValidationError) as exc:
                # Corrupt or stale-format row: treat as a miss and delete it so
                # the same cache key does not stay a permanent false negative.
                logger.debug("[DiskCache] GET dropping corrupt row: %s", exc)
                try:
                    conn.execute(
                        "DELETE FROM vlm_cache WHERE cache_key = ?",
                        (cache_key,),
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass
                return None
        except sqlite3.Error as exc:
            logger.debug("[DiskCache] GET error: %s", exc)
            return None

    def set(self, cache_key: str, provider: str, model: str, response: LLMResponse) -> None:
        """Store a response in the disk cache."""
        import sqlite3
        try:
            conn = self._get_conn()
            now = time.time()
            response_json = json.dumps(response.model_dump(), default=str)
            conn.execute(
                """INSERT OR REPLACE INTO vlm_cache
                   (cache_key, provider, model, response_json, created_at, access_count, last_accessed)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (cache_key, provider, model, response_json, now, now),
            )
            conn.commit()
            # Prune if over max_entries
            self._prune(conn)
        except (sqlite3.Error, ValueError, TypeError, ValidationError) as exc:
            logger.debug("[DiskCache] SET error: %s", exc)

    def _prune(self, conn: "sqlite3.Connection") -> None:
        """Remove oldest entries if over max_entries."""
        import sqlite3
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM vlm_cache")
            count = cursor.fetchone()[0]
            if count > self.max_entries:
                to_delete = count - self.max_entries
                conn.execute(
                    "DELETE FROM vlm_cache WHERE cache_key IN ("
                    "SELECT cache_key FROM vlm_cache ORDER BY last_accessed ASC LIMIT ?"
                    ")",
                    (to_delete,),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def get_stats(self) -> Dict[str, Any]:
        """Return disk cache statistics."""
        import sqlite3
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*), SUM(access_count) FROM vlm_cache")
            row = cursor.fetchone()
            return {
                "disk_cache_enabled": True,
                "disk_cache_path": self.db_path,
                "disk_cache_entries": row[0] or 0,
                "disk_cache_total_hits": row[1] or 0,
            }
        except sqlite3.Error as exc:
            return {
                "disk_cache_enabled": True,
                "disk_cache_path": self.db_path,
                "disk_cache_error": str(exc),
            }


# ---------------------------------------------------------------------------
# Cache key helper
# ---------------------------------------------------------------------------

def _compute_cache_key(
    system_prompt: str,
    user_prompt: str,
    images: List[Any],
    enable_thinking: Optional[bool] = None,
    thinking_budget: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
) -> str:
    """Compute a deterministic cache key for a VLM call.

    The key is a SHA-256 hex digest of the prompt text combined with
    the raw image data.  This allows identical calls (same prompt +
    same images) to hit the cache even if the caller passes different
    Python object identities.

    Args:
        system_prompt: Rendered system prompt.
        user_prompt: Rendered user prompt.
        images: List of images (PIL Image, bytes, or file paths).
        enable_thinking: Thinking 开关取值参与键(不同取值必须得到不同
            key,否则关 thinking 的请求会命中开 thinking 的旧缓存,反之
            亦然);None 不追加字段,与历史键保持一致。
        thinking_budget: 思考软预算参与键;None 不追加字段。
        reasoning_effort: 思考档位(low/medium/xhigh)参与键;None 不追加
            字段。防止 xhigh 旧缓存污染 medium 新结果。

    Returns:
        Hex digest string suitable as a cache key.
    """
    hasher = hashlib.sha256()
    hasher.update((system_prompt or "").encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update((user_prompt or "").encode("utf-8"))

    for img in images:
        hasher.update(b"\x00")
        if isinstance(img, bytes):
            hasher.update(img)
        elif isinstance(img, str):
            try:
                with open(img, "rb") as fh:
                    hasher.update(fh.read())
            except OSError:
                hasher.update(img.encode("utf-8"))
        else:
            # PIL Image or other – convert to PNG bytes
            try:
                from PIL import Image as PILImage
                if isinstance(img, PILImage.Image):
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    hasher.update(buf.getvalue())
                else:
                    hasher.update(str(img).encode("utf-8"))
            except Exception:
                hasher.update(str(img).encode("utf-8"))

    if enable_thinking is not None:
        hasher.update(b"\x00")
        hasher.update(f"enable_thinking={enable_thinking}".encode("utf-8"))
    if thinking_budget is not None:
        hasher.update(b"\x00")
        hasher.update(f"thinking_budget={thinking_budget}".encode("utf-8"))
    if reasoning_effort is not None:
        hasher.update(b"\x00")
        hasher.update(f"reasoning_effort={reasoning_effort}".encode("utf-8"))

    return hasher.hexdigest()
