"""RAG 检索路由(POST /api/rag/search)端点测试。

[文件说明]
作用:web 层 RAG 检索端点——进程内直调 rag.query(embedding 经 monkeypatch
    mock):text/related/site 契约、库缺失 404 + build_rag_index 引导文案、
    未选工作区 400、参数校验 422;全部使用 tmp_path 假库,无网络。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/web/rag.py(被测路由)、rag/query.py(检索逻辑)。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.rag import query as rag_query
from traffic_analyzer.rag.store import RagStore
from traffic_analyzer.web.app import create_app


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
            events=[],
            has_annotation=0,
            review_status="unconfirmed",
            stake="K18+470",
            direction="进京",
            site="G3京台高速|K18+470|进京|3",
            start_ts=1754288900.0,
            duration_s=6.0,
        )


@pytest.fixture()
def mock_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rag_query, "embed_texts", lambda texts: [_unit_vec(8, 0) for _ in texts]
    )


class TestRagSearch:
    def test_text_contract(self, tmp_path: Path, mock_embed: None) -> None:
        _seed(tmp_path)
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post("/api/rag/search", json={"mode": "text", "query": "应急车道"})
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

    def test_related_and_site(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post("/api/rag/search", json={"mode": "related", "video": "a.mp4"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["target"] == "a.mp4"
        resp = client.post(
            "/api/rag/search",
            json={"mode": "site", "query": "K18", "after": 1754288400.0},
        )
        assert resp.status_code == 200, resp.text
        assert [r["video_path"] for r in resp.json()["results"]] == ["b.mp4"]

    def test_index_missing_404(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post("/api/rag/search", json={"mode": "site", "query": "K18"})
        assert resp.status_code == 404
        assert "build_rag_index" in resp.json()["detail"]

    def test_no_workspace_400(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/rag/search", json={"mode": "site", "query": "K18"})
        assert resp.status_code == 400

    def test_validation_422(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post("/api/rag/search", json={"mode": "text"})
        assert resp.status_code == 422
        resp = client.post("/api/rag/search", json={"mode": "bogus", "query": "x"})
        assert resp.status_code == 422

    def test_video_not_indexed_404(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post(
            "/api/rag/search", json={"mode": "related", "video": "missing.mp4"}
        )
        assert resp.status_code == 404
