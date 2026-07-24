"""
Unit tests for vlm_cache.py (DiskCache corrupt-row self-healing).

[文件说明]
作用:测试 DiskCache 磁盘缓存,重点覆盖损坏缓存行的自愈(读取失败时清理)行为。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/core/vlm_cache.py(被测模块)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from traffic_analyzer.core.vlm_cache import DiskCache
from traffic_analyzer.models.schemas import LLMResponse


def _insert_raw_row(db_path: Path, cache_key: str, response_json: str) -> None:
    """Insert a cache row directly, bypassing DiskCache.set serialization."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT OR REPLACE INTO vlm_cache
           (cache_key, provider, model, response_json, created_at, access_count, last_accessed)
           VALUES (?, ?, ?, ?, 0.0, 1, 0.0)""",
        (cache_key, "aliyun", "qwen-vl-max", response_json),
    )
    conn.commit()
    conn.close()


def _row_exists(db_path: Path, cache_key: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM vlm_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    conn.close()
    return bool(row[0])


def test_disk_cache_roundtrip(tmp_path: Path) -> None:
    cache = DiskCache(str(tmp_path / "cache.db"))
    response = LLMResponse(success=True, raw_text="ok", parsed_data={"a": 1})
    cache.set("k1", "aliyun", "qwen-vl-max", response)

    cached = cache.get("k1", "aliyun", "qwen-vl-max")
    assert cached is not None
    assert cached.parsed_data == {"a": 1}
    # Rows are keyed by provider/model as well
    assert cache.get("k1", "anthropic", "qwen-vl-max") is None
    assert cache.get("k1", "aliyun", "other-model") is None


def test_disk_cache_get_invalid_json_returns_none_and_deletes(tmp_path: Path) -> None:
    """A corrupt response_json must be treated as a miss, not crash call()."""
    db_path = tmp_path / "cache.db"
    cache = DiskCache(str(db_path))
    _insert_raw_row(db_path, "bad", "{not valid json")

    assert cache.get("bad", "aliyun", "qwen-vl-max") is None
    # Self-healing: the corrupt row is removed so the key can be re-cached
    assert not _row_exists(db_path, "bad")


def test_disk_cache_get_schema_mismatch_returns_none_and_deletes(tmp_path: Path) -> None:
    """A stale-format row that fails LLMResponse validation is a miss."""
    db_path = tmp_path / "cache.db"
    cache = DiskCache(str(db_path))
    _insert_raw_row(db_path, "stale", json.dumps({"prompt_tokens": "not-an-int"}))

    assert cache.get("stale", "aliyun", "qwen-vl-max") is None
    assert not _row_exists(db_path, "stale")


def test_disk_cache_get_non_dict_json_returns_none_and_deletes(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    cache = DiskCache(str(db_path))
    _insert_raw_row(db_path, "list", json.dumps([1, 2, 3]))

    assert cache.get("list", "aliyun", "qwen-vl-max") is None
    assert not _row_exists(db_path, "list")
