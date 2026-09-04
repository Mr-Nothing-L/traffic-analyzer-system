"""RAG 建库路由(POST /api/rag/build、GET status、POST cancel)端点测试。

[文件说明]
作用:web 层建库端点——build_index 经 monkeypatch mock(gate 控制结束时机):
启动 → running/total、重复启动 409、cancel → partial;空闲时 status 也返回
library 概况(exists/count/built_at,库不存在 exists=False);未选工作区 400;
全部使用 tmp_path 假工作区,无网络、无真实 embedding。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/web/rag.py(被测路由)。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.rag.store import RagStore
from traffic_analyzer.web import rag as web_rag
from traffic_analyzer.web.app import create_app


@pytest.fixture(autouse=True)
def _reset_build_state() -> None:
    """模块级建库状态跨用例隔离:前置等遗留线程退出并清零,后置确保取消。"""
    web_rag._build_cancel.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        with web_rag._build_lock:
            if not web_rag._build_state["running"]:
                break
        time.sleep(0.02)
    with web_rag._build_lock:
        web_rag._build_state.update(
            running=False,
            done=0,
            total=0,
            failed=0,
            started_at=None,
            finished_at=None,
            last_error=None,
            partial=False,
        )
    web_rag._build_cancel.clear()
    yield
    web_rag._build_cancel.set()


def _make_workspace(workspace) -> None:
    (workspace / "a.mp4").write_bytes(b"a")
    (workspace / "b.mp4").write_bytes(b"b")


def _blocking_build(workspace, **kwargs):
    """假 build_index:报一次进度后阻塞,直到 cancel_flag 置位,返回 partial。"""
    progress_cb = kwargs.get("progress_cb")
    cancel_flag = kwargs["cancel_flag"]
    if progress_cb:
        progress_cb(1, 2, 0)
    deadline = time.time() + 10
    while time.time() < deadline:
        if cancel_flag():
            if progress_cb:
                progress_cb(2, 2, 1)
            return {
                "workspace": str(workspace),
                "elapsed_s": 0.1,
                "success": ["a.mp4"],
                "failed": [{"video": "b.mp4", "error": "RuntimeError: boom"}],
                "stats": {},
                "total": 2,
                "partial": True,
            }
        time.sleep(0.01)
    raise AssertionError("build was not cancelled in time")


class TestRagBuildEndpoints:
    def test_start_status_409_cancel(self, tmp_path, monkeypatch) -> None:
        _make_workspace(tmp_path)
        monkeypatch.setattr(web_rag, "build_index", _blocking_build)
        client = TestClient(create_app(workspace=str(tmp_path)))

        resp = client.post("/api/rag/build", json={"concurrency": 4})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"started": True, "total": 2}

        status = client.get("/api/rag/build/status").json()
        assert status["running"] is True
        assert status["done"] == 1
        assert status["total"] == 2
        assert status["failed"] == 0
        assert status["started_at"] is not None
        assert status["finished_at"] is None
        assert status["last_error"] is None
        assert status["partial"] is False
        assert "library" in status

        resp = client.post("/api/rag/build")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "build already running"

        resp = client.post("/api/rag/build/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"cancelling": True}

        deadline = time.time() + 10
        while time.time() < deadline:
            status = client.get("/api/rag/build/status").json()
            if not status["running"]:
                break
            time.sleep(0.02)
        assert status["running"] is False
        assert status["partial"] is True
        assert status["failed"] == 1
        assert status["finished_at"] is not None
        assert status["last_error"] is None

    def test_build_exception_goes_to_last_error(self, tmp_path, monkeypatch) -> None:
        _make_workspace(tmp_path)

        def _boom(workspace, **kwargs):
            raise RuntimeError("store exploded")

        monkeypatch.setattr(web_rag, "build_index", _boom)
        client = TestClient(create_app(workspace=str(tmp_path)))
        assert client.post("/api/rag/build").status_code == 200
        deadline = time.time() + 10
        while time.time() < deadline:
            status = client.get("/api/rag/build/status").json()
            if not status["running"]:
                break
            time.sleep(0.02)
        assert "RuntimeError: store exploded" in status["last_error"]
        assert status["finished_at"] is not None

    def test_status_idle_library(self, tmp_path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        status = client.get("/api/rag/build/status").json()
        assert status["running"] is False
        assert status["library"] == {"exists": False, "count": 0, "built_at": None}

        with RagStore(tmp_path) as store:
            store.upsert_record("a.mp4", video_vec=[1.0, 0.0])
            store.set_meta("built_at", 1756000000.0)
        status = client.get("/api/rag/build/status").json()
        assert status["library"]["exists"] is True
        assert status["library"]["count"] == 1
        assert status["library"]["built_at"] == pytest.approx(1756000000.0)

    def test_no_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.post("/api/rag/build").status_code == 400
        assert client.get("/api/rag/build/status").status_code == 400
