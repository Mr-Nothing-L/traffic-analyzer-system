"""
Unit tests for rag/query.py(四种检索的结构化契约与过滤逻辑)。

[文件说明]
作用:测试 run_search/search_text/search_related/search_adjacent/search_site——
    结果契约字段(video_path/score 4 位小数/events null/site/start_ts/
    duration_s/has_annotation/human_edited/review_status)、only_confirmed
    与 human_edited 过滤、related 排除自身、adjacent 时间窗与 site 缺失回退、
    site 桩号/方向/时间窗过滤、库缺失 RagIndexNotFound、参数错误 RagQueryError;
    全部使用 tmp_path 假向量,embedding 经 embed_fn 注入,无网络。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/rag/query.py(被测模块)。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from traffic_analyzer.rag.query import (
    RagIndexNotFound,
    RagQueryError,
    parse_time,
    run_search,
)
from traffic_analyzer.rag.store import RagStore

RESULT_KEYS = {
    "video_path",
    "score",
    "events",
    "site",
    "start_ts",
    "duration_s",
    "has_annotation",
    "human_edited",
    "review_status",
}


def _unit_vec(dim: int, idx: int) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[idx] = 1.0
    return v.tolist()


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [_unit_vec(8, 0) for _ in texts]


def _seed(workspace: Path) -> None:
    with RagStore(workspace) as s:
        s.upsert_record(
            "a.mp4",
            video_vec=_unit_vec(8, 0),
            ann_vec=_unit_vec(8, 2),
            events=[2, 3],
            has_annotation=1,
            human_edited=1,
            review_status="confirmed",
            stake="K18+470",
            direction="进京",
            site="G3-K18-进京-3",
            start_ts=1000.0,
            duration_s=10.0,
        )
        s.upsert_record(
            "b.mp4",
            video_vec=_unit_vec(8, 1),
            ann_vec=None,
            events=[],
            has_annotation=0,
            human_edited=0,
            review_status="unconfirmed",
            stake="K18+470",
            direction="进京",
            site="G3-K18-进京-3",
            start_ts=1600.0,
            duration_s=6.0,
        )
        s.upsert_record(
            "c.mp4",
            video_vec=_unit_vec(8, 0),  # 与 a 相同的 video 向量
            ann_vec=_unit_vec(8, 3),
            events=[2],
            has_annotation=1,
            human_edited=0,
            review_status="needs_review",
            stake="K20+000",
            direction="出京",
            site=None,
            start_ts=None,
            duration_s=None,
        )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    _seed(tmp_path)
    return tmp_path


def _by_path(results: list[dict]) -> dict[str, dict]:
    return {r["video_path"]: r for r in results}


def test_parse_time() -> None:
    assert parse_time(None) is None
    assert parse_time(1000) == 1000.0
    assert parse_time("1000.5") == 1000.5
    ts = parse_time("2099-01-01T00:00:00")
    assert ts is not None and ts > 4e9  # ISO 字符串可解析(本地时区)


def test_index_missing_raises(workspace: Path, tmp_path: Path) -> None:
    del workspace
    with pytest.raises(RagIndexNotFound, match="build_rag_index"):
        run_search(tmp_path / "empty", "text", query="x", embed_fn=_fake_embed)


def test_unknown_mode(workspace: Path) -> None:
    with pytest.raises(RagQueryError, match="unknown mode"):
        run_search(workspace, "bogus", query="x", embed_fn=_fake_embed)


def test_missing_required_params(workspace: Path) -> None:
    with pytest.raises(RagQueryError):
        run_search(workspace, "text", embed_fn=_fake_embed)
    with pytest.raises(RagQueryError):
        run_search(workspace, "related")
    with pytest.raises(RagQueryError):
        run_search(workspace, "adjacent")
    with pytest.raises(RagQueryError):
        run_search(workspace, "site")


def test_text_mode_contract(workspace: Path) -> None:
    resp = run_search(workspace, "text", query="应急车道", embed_fn=_fake_embed)
    assert resp["mode"] == "text"
    assert isinstance(resp["elapsed_ms"], float) and resp["elapsed_ms"] >= 0
    assert len(resp["results"]) == 3
    items = _by_path(resp["results"])
    first = items["a.mp4"]
    assert set(first) == RESULT_KEYS
    # hybrid:0.6 * cos_video(1.0) + 0.4 * cos_ann(0.0)
    assert first["score"] == pytest.approx(0.6)
    assert first["score"] == round(first["score"], 4)
    assert first == {
        "video_path": "a.mp4",
        "score": 0.6,
        "events": [2, 3],
        "site": "G3-K18-进京-3",
        "start_ts": 1000.0,
        "duration_s": 10.0,
        "has_annotation": True,
        "human_edited": True,
        "review_status": "confirmed",
    }
    scores = [r["score"] for r in resp["results"]]
    assert scores == sorted(scores, reverse=True)


def test_text_mode_null_fields(workspace: Path) -> None:
    resp = run_search(workspace, "text", query="x", embed_fn=_fake_embed)
    items = _by_path(resp["results"])
    # b:无标注 → events/events 衍生字段给 null
    assert items["b.mp4"]["events"] is None
    assert items["b.mp4"]["has_annotation"] is False
    assert items["b.mp4"]["score"] == pytest.approx(0.0)
    # c:site/start_ts/duration_s 缺失 → null
    assert items["c.mp4"]["site"] is None
    assert items["c.mp4"]["start_ts"] is None
    assert items["c.mp4"]["duration_s"] is None


def test_text_mode_filters(workspace: Path) -> None:
    resp = run_search(
        workspace, "text", query="x", only_confirmed=True, embed_fn=_fake_embed
    )
    assert [r["video_path"] for r in resp["results"]] == ["a.mp4"]
    resp = run_search(workspace, "text", query="x", human_edited=True, embed_fn=_fake_embed)
    assert [r["video_path"] for r in resp["results"]] == ["a.mp4"]
    resp = run_search(workspace, "text", query="x", k=1, embed_fn=_fake_embed)
    assert len(resp["results"]) == 1


def test_text_mode_embedding_failure(workspace: Path) -> None:
    def _boom(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("provider down")

    with pytest.raises(RagQueryError) as exc_info:
        run_search(workspace, "text", query="x", embed_fn=_boom)
    assert exc_info.value.status_code == 502


def test_related_mode(workspace: Path) -> None:
    resp = run_search(workspace, "related", video="a.mp4")
    assert resp["mode"] == "related"
    assert resp["target"] == "a.mp4"
    paths = [r["video_path"] for r in resp["results"]]
    assert "a.mp4" not in paths  # 排除自身
    assert paths[0] == "c.mp4"  # 与 a 同向量,分数 1.0
    assert resp["results"][0]["score"] == pytest.approx(1.0)


def test_related_mode_stem_match_and_k(workspace: Path) -> None:
    resp = run_search(workspace, "related", video="a", k=1)  # stem 匹配
    assert resp["target"] == "a.mp4"
    assert len(resp["results"]) == 1


def test_related_mode_errors(workspace: Path) -> None:
    with pytest.raises(RagQueryError) as exc_info:
        run_search(workspace, "related", video="missing.mp4")
    assert exc_info.value.status_code == 404
    with RagStore(workspace) as s:
        s.upsert_record("d.mp4")  # 无 video_vec
    with pytest.raises(RagQueryError, match="no video_vec"):
        run_search(workspace, "related", video="d.mp4")


def test_adjacent_mode_same_site(workspace: Path) -> None:
    resp = run_search(workspace, "adjacent", video="a.mp4")
    assert resp["mode"] == "adjacent"
    assert resp["target"] == "a.mp4"
    assert resp["source"] == "site=G3-K18-进京-3"
    # a_end=1010,b_start=1600 → 间隔 590 ≤ 600 命中;c 不同 site 不在候选
    assert [r["video_path"] for r in resp["results"]] == ["b.mp4"]
    # 无相似分模式 score 为 null
    assert resp["results"][0]["score"] is None
    resp = run_search(workspace, "adjacent", video="a.mp4", gap_s=500.0)
    assert resp["results"] == []


def test_adjacent_mode_site_missing_fallback(workspace: Path) -> None:
    resp = run_search(workspace, "adjacent", video="c.mp4")
    assert resp["source"] == "site missing, fallback video_vec top-50"
    # c 无 start_ts → 时间邻近全部不成立
    assert resp["results"] == []


def test_site_mode_filters(workspace: Path) -> None:
    resp = run_search(workspace, "site", query="K18")
    assert [r["video_path"] for r in resp["results"]] == ["a.mp4", "b.mp4"]
    resp = run_search(workspace, "site", query="K18", direction="出京")
    assert resp["results"] == []
    resp = run_search(workspace, "site", query="K20", direction="出京")
    assert [r["video_path"] for r in resp["results"]] == ["c.mp4"]
    resp = run_search(workspace, "site", query="K18", after=1500.0)
    assert [r["video_path"] for r in resp["results"]] == ["b.mp4"]
    resp = run_search(workspace, "site", query="K18", before=1500.0)
    assert [r["video_path"] for r in resp["results"]] == ["a.mp4"]
    resp = run_search(workspace, "site", query="K18", before="2099-01-01T00:00:00")
    assert len(resp["results"]) == 2
