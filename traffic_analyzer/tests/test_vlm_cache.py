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

from traffic_analyzer.core.vlm_cache import DiskCache, _compute_cache_key
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


# ---------------------------------------------------------------------------
# _compute_cache_key: enable_thinking 必须参与键
# ---------------------------------------------------------------------------


def test_cache_key_distinguishes_enable_thinking() -> None:
    """enable_thinking 三个取值必须得到三个不同 key,否则互相串缓存
    (关 thinking 的请求吃到开 thinking 的旧响应,反之亦然)。"""
    args = ("sys prompt", "user prompt", [b"fake-jpeg-bytes"])
    keys = {
        _compute_cache_key(*args),
        _compute_cache_key(*args, enable_thinking=False),
        _compute_cache_key(*args, enable_thinking=True),
    }
    assert len(keys) == 3


def test_cache_key_none_thinking_matches_legacy_key() -> None:
    """不传参(None)不追加字段 → 与历史键一致,旧缓存条目继续命中。"""
    legacy = _compute_cache_key("sys", "user", [])
    assert legacy == _compute_cache_key("sys", "user", [], enable_thinking=None)


def test_cache_key_distinguishes_reasoning_effort() -> None:
    """reasoning_effort 不同取值必须隔离,防止 xhigh 旧缓存污染 medium 结果。"""
    args = ("sys prompt", "user prompt", [b"fake-jpeg-bytes"])
    keys = {
        _compute_cache_key(*args),
        _compute_cache_key(*args, reasoning_effort="low"),
        _compute_cache_key(*args, reasoning_effort="medium"),
        _compute_cache_key(*args, reasoning_effort="xhigh"),
    }
    assert len(keys) == 4


def test_cache_key_distinguishes_thinking_budget() -> None:
    args = ("sys prompt", "user prompt", [b"fake-jpeg-bytes"])
    keys = {
        _compute_cache_key(*args),
        _compute_cache_key(*args, thinking_budget=512),
        _compute_cache_key(*args, thinking_budget=1024),
    }
    assert len(keys) == 3


def test_cache_key_combines_thinking_params() -> None:
    args = ("sys prompt", "user prompt", [b"fake-jpeg-bytes"])
    base = _compute_cache_key(*args)
    with_effort = _compute_cache_key(*args, reasoning_effort="medium")
    with_budget = _compute_cache_key(*args, thinking_budget=1024)
    with_both = _compute_cache_key(
        *args, reasoning_effort="medium", thinking_budget=1024
    )
    assert len({base, with_effort, with_budget, with_both}) == 4
