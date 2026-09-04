"""
Unit tests for rag/store.py (RagStore upsert / search / 过滤 / 混合分排序).

[文件说明]
作用:测试 RagStore 的写入、按字段检索、event/review 过滤、hybrid alpha 加权排序、
get_vec / stats / meta 读写;全部使用 tmp_path 假向量,无网络。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/rag/store.py(被测模块)。
"""

from __future__ import annotations

import numpy as np
import pytest

from traffic_analyzer.rag.store import RagStore


def _unit_vec(dim: int, idx: int) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[idx] = 1.0
    return v.tolist()


@pytest.fixture()
def store(tmp_path):
    with RagStore(tmp_path) as s:
        yield s


def _seed(store: RagStore) -> None:
    store.upsert_record(
        "a.mp4",
        video_vec=_unit_vec(8, 0),
        ann_vec=_unit_vec(8, 2),
        events=[2, 3],
        has_annotation=1,
        human_edited=1,
        review_status="confirmed",
        site="G3-K18-进京-3",
        start_ts=1751869790.0,
    )
    store.upsert_record(
        "b.mp4",
        video_vec=_unit_vec(8, 1),
        ann_vec=None,
        events=[5],
        has_annotation=0,
        human_edited=0,
        review_status="unconfirmed",
        site="G3-K18-进京-3",
        start_ts=1751869890.0,
    )
    store.upsert_record(
        "c.mp4",
        video_vec=_unit_vec(8, 0),  # 与 a 相同的 video 向量
        ann_vec=_unit_vec(8, 3),
        events=[2],
        has_annotation=1,
        human_edited=0,
        review_status="needs_review",
        site=None,
        start_ts=None,
    )


def test_db_created_under_agent_rag(tmp_path):
    with RagStore(tmp_path) as s:
        assert s.db_path == tmp_path / ".agent" / "rag" / "vectors.db"
        assert s.db_path.is_file()


def test_upsert_records_and_existing_paths(store):
    _seed(store)
    assert store.existing_paths() == {"a.mp4", "b.mp4", "c.mp4"}
    records = {r["video_path"]: r for r in store.records()}
    assert records["a.mp4"]["events"] == [2, 3]
    assert records["a.mp4"]["human_edited"] == 1
    assert records["b.mp4"]["has_annotation"] == 0
    assert records["c.mp4"]["site"] is None


def test_upsert_replaces_existing(store):
    _seed(store)
    store.upsert_record("a.mp4", video_vec=_unit_vec(8, 1), review_status="needs_review")
    assert len(store.records()) == 3
    rec = {r["video_path"]: r for r in store.records()}["a.mp4"]
    assert rec["review_status"] == "needs_review"
    vec = store.get_vec("a.mp4", "video")
    np.testing.assert_allclose(vec, np.array(_unit_vec(8, 1), dtype=np.float32))


def test_search_video_field(store):
    _seed(store)
    results = store.search(_unit_vec(8, 0), "video", 10)
    paths = [p for p, _ in results]
    assert paths[:2] == ["a.mp4", "c.mp4"]  # 与 query 同向,分数 1.0
    assert results[0][1] == pytest.approx(1.0)
    assert paths[-1] == "b.mp4"
    assert results[-1][1] == pytest.approx(0.0)


def test_search_annotation_field_skips_missing_ann(store):
    _seed(store)
    results = store.search(_unit_vec(8, 2), "annotation", 10)
    assert [p for p, _ in results] == ["a.mp4", "c.mp4"]  # b.mp4 无 ann_vec
    assert results[0][1] == pytest.approx(1.0)


def test_search_hybrid_alpha(store):
    _seed(store)
    q = _unit_vec(8, 0)
    # alpha=1.0 → 纯 video 分数;a 与 c 的 video 向量相同,并列 1.0
    results = store.search(q, "hybrid", 10, alpha=1.0)
    assert results[0][1] == pytest.approx(1.0)
    assert {p for p, s in results if s == pytest.approx(1.0)} == {"a.mp4", "c.mp4"}
    # alpha=0.0 → 纯 annotation 分数;a/c 的 ann 与 q 正交(0.0),b 无 ann 回退 video 分数
    results = store.search(q, "hybrid", 10, alpha=0.0)
    by_path = dict(results)
    assert set(by_path) == {"a.mp4", "b.mp4", "c.mp4"}  # b 回退后仍在结果中
    assert by_path["a.mp4"] == pytest.approx(0.0)
    assert by_path["b.mp4"] == pytest.approx(0.0)  # b 的 video 向量也与 q 正交
    # 用与 a 的 ann 同向的 query 验证 alpha=0 排序
    results = store.search(_unit_vec(8, 2), "hybrid", 10, alpha=0.0)
    assert results[0][0] == "a.mp4"
    assert results[0][1] == pytest.approx(1.0)
    # alpha=0.5 → a: 0.5*1 + 0.5*0 = 0.5;b: 无 ann 回退 video 分数 1.0 排最前
    results = store.search(q, "hybrid", 10, alpha=0.5)
    by_path = dict(results)
    assert by_path["a.mp4"] == pytest.approx(0.5)
    assert by_path["b.mp4"] == pytest.approx(0.0)


def test_search_event_filter(store):
    _seed(store)
    results = store.search(_unit_vec(8, 0), "video", 10, event_filter=[3])
    assert [p for p, _ in results] == ["a.mp4"]
    results = store.search(_unit_vec(8, 0), "video", 10, event_filter=[2])
    assert {p for p, _ in results} == {"a.mp4", "c.mp4"}


def test_search_review_filter(store):
    _seed(store)
    results = store.search(_unit_vec(8, 0), "video", 10, review_filter="confirmed")
    assert [p for p, _ in results] == ["a.mp4"]
    results = store.search(
        _unit_vec(8, 0), "video", 10, review_filter=["confirmed", "needs_review"]
    )
    assert {p for p, _ in results} == {"a.mp4", "c.mp4"}


def test_search_top_k(store):
    _seed(store)
    results = store.search(_unit_vec(8, 0), "video", 2)
    assert len(results) == 2


def test_get_vec_and_unknown_field(store):
    _seed(store)
    vec = store.get_vec("a.mp4", "annotation")
    np.testing.assert_allclose(vec, np.array(_unit_vec(8, 2), dtype=np.float32))
    assert store.get_vec("b.mp4", "annotation") is None
    assert store.get_vec("missing.mp4", "video") is None
    with pytest.raises(ValueError):
        store.get_vec("a.mp4", "hybrid")
    with pytest.raises(ValueError):
        store.search(_unit_vec(8, 0), "bogus", 10)


def test_stats_and_meta(store):
    _seed(store)
    store.set_meta("model", "wemm-embedding-9b")
    store.set_meta("dim", 4096)
    assert store.get_meta("model") == "wemm-embedding-9b"
    stats = store.stats()
    assert stats["total"] == 3
    assert stats["with_video_vec"] == 3
    assert stats["with_ann_vec"] == 2
    assert stats["has_annotation"] == 2
    assert stats["human_edited"] == 1
    assert stats["review_status"] == {
        "confirmed": 1,
        "unconfirmed": 1,
        "needs_review": 1,
    }
    assert stats["meta"]["dim"] == "4096"
