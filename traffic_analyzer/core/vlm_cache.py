"""
Disk cache and cache-key helpers for the VLM inference engine.

Provides a SQLite-backed persistent cache and deterministic cache-key
generation for VLM requests.
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
            data = json.loads(row[0])
            return LLMResponse(**data)
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
        except sqlite3.Error as exc:
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

def _compute_cache_key(system_prompt: str, user_prompt: str, images: List[Any]) -> str:
    """Compute a deterministic cache key for a VLM call.

    The key is a SHA-256 hex digest of the prompt text combined with
    the raw image data.  This allows identical calls (same prompt +
    same images) to hit the cache even if the caller passes different
    Python object identities.

    Args:
        system_prompt: Rendered system prompt.
        user_prompt: Rendered user prompt.
        images: List of images (PIL Image, bytes, or file paths).

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

    return hasher.hexdigest()
