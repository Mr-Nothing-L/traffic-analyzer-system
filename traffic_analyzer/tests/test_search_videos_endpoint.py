"""Integration tests for POST /tools/search_videos (tmp 假库 + mock embedding)。

[文件说明]
作用:端点集成测试——FastAPI TestClient + tmp_path 种子库:
    text 模式契约(embedding 经 monkeypatch mock,不打真实服务)、
    related/adjacent/site 模式、库缺失 404 rag_index_missing(引导文案)、
    workspace 越界 403、/config/roots 热注册根内工作区可查、
    参数校验 422、目标不在库 404 rag_query_error。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/toolserver/server.py(被测端点);
    rag/query.py(检索逻辑,mock embed_texts 驱动)。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.rag import query as rag_query
from traffic_analyzer.rag.store import RagStore
from traffic_analyzer.toolserver import create_app


def _unit_vec(dim: int, idx: int) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[idx] = 1.0
    return v.tolist()


def _seed(workspace: Path) -> None:
    with RagStore(workspace) as s:
        s.upsert_record(
            "a.mp4",
            video_vec=_unit_vec(8, 0),
            ann_vec=_unit_vec(8, 2),
            events=[2, 8],
            has_annotation=1,
            human_edited=0,
            review_status="confirmed",
            stake="K18+470",
            direction="进京",
            site="G3京台高速|K18+470|进京|3",
            start_ts=1754288341.555,
            duration_s=6.43,
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
            site="G3京台高速|K18+470|进京|3",
            start_ts=1754288900.0,
            duration_s=6.0,
        )


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    _seed(tmp_path)
    return TestClient(create_app(tmp_path))


@pytest.fixture()
def mock_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rag_query, "embed_texts", lambda texts: [_unit_vec(8, 0) for _ in texts]
    )


def test_text_mode_contract(client: TestClient, mock_embed: None) -> None:
    resp = client.post("/tools/search_videos", json={"mode": "text", "query": "应急车道占用"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "text"
    assert body["elapsed_ms"] >= 0
    first = body["results"][0]
    assert first["video_path"] == "a.mp4"
    assert first["score"] == pytest.approx(0.6)
    assert first["events"] == [2, 8]
    assert first["site"] == "G3京台高速|K18+470|进京|3"
    assert first["start_ts"] == 1754288341.555
    assert first["duration_s"] == 6.43
    assert first["has_annotation"] is True
    assert first["human_edited"] is False
    assert first["review_status"] == "confirmed"
    assert set(first) == {
        "video_path", "score", "events", "site", "start_ts", "duration_s",
        "has_annotation", "human_edited", "review_status",
    }


def test_related_mode(client: TestClient) -> None:
    resp = client.post("/tools/search_videos", json={"mode": "related", "video": "a.mp4"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target"] == "a.mp4"
    assert [r["video_path"] for r in body["results"]] == ["b.mp4"]


def test_adjacent_mode(client: TestClient) -> None:
    resp = client.post("/tools/search_videos", json={"mode": "adjacent", "video": "a.mp4"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"].startswith("site=")
    assert [r["video_path"] for r in body["results"]] == ["b.mp4"]
    assert body["results"][0]["score"] is None


def test_site_mode(client: TestClient) -> None:
    resp = client.post(
        "/tools/search_videos",
        json={"mode": "site", "query": "K18", "direction": "进京", "after": 1754288400.0},
    )
    assert resp.status_code == 200, resp.text
    assert [r["video_path"] for r in resp.json()["results"]] == ["b.mp4"]


def test_index_missing_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    resp = client.post("/tools/search_videos", json={"mode": "site", "query": "K18"})
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "rag_index_missing"
    assert "build_rag_index" in error["message"]


def test_workspace_outside_roots_403(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/tools/search_videos",
        json={"mode": "site", "query": "K18", "workspace": str(tmp_path / ".." / ".." / "etc")},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "path_outside_workspace"


def test_workspace_in_registered_root(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "other_ws"
    other.mkdir()
    _seed(other)
    resp = client.post("/config/roots", json={"path": str(other)})
    assert resp.status_code == 200
    resp = client.post(
        "/tools/search_videos",
        json={"mode": "related", "video": "a.mp4", "workspace": str(other)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["target"] == "a.mp4"


def test_mode_param_validation_422(client: TestClient) -> None:
    resp = client.post("/tools/search_videos", json={"mode": "text"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
    resp = client.post("/tools/search_videos", json={"mode": "related"})
    assert resp.status_code == 422
    resp = client.post("/tools/search_videos", json={"mode": "bogus", "query": "x"})
    assert resp.status_code == 422


def test_video_not_indexed_404(client: TestClient) -> None:
    resp = client.post(
        "/tools/search_videos", json={"mode": "related", "video": "missing.mp4"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "rag_query_error"
