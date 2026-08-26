"""Tests for the /api/agent reverse proxy (traffic_analyzer.web.agentproxy).

下游(TS agent server / toolserver)用 httpx.MockTransport 替身:
monkeypatch agentproxy.routes.AsyncClient 注入 transport,不起真实子进程、
不打真实端口。覆盖:路由转发、SSE 逐行透传不缓冲、workspaceDir 注入、
下游不可用/未启动时的降级响应,以及 AgentRuntimeManager 的启停语义。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web import app as app_mod
from traffic_analyzer.web.agentproxy import routes as routes_mod
from traffic_analyzer.web.agentproxy import runtime as runtime_mod
from traffic_analyzer.web.agentproxy.runtime import AgentRuntimeManager

# autouse 的 root_posts fixture 会替换 _post_workspace_root;需要验证其
# 真实实现(URL/body)的用例先把它还原回来。
_REAL_POST_WORKSPACE_ROOT = runtime_mod._post_workspace_root
_REAL_POST_WORKSPACE_RESTORE = runtime_mod._post_workspace_restore


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _patch_downstream(monkeypatch: pytest.MonkeyPatch, handler: Callable) -> None:
    """让代理路由的 httpx.AsyncClient 走 MockTransport(handler)。"""

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(*args, **kwargs)

    monkeypatch.setattr(routes_mod, "AsyncClient", factory)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """最小健康下游:所有端点返回成功。"""
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/sessions":
        return httpx.Response(200, json={"sessionId": "s-1"})
    if request.url.path == "/approval":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "?"}})


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "v1.mp4").write_bytes(b"")
    return ws


@pytest.fixture()
def proxy_app(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """完整 create_app(不进入 lifespan,不 spawn 真实子进程),手动挂上 runtime。"""
    monkeypatch.delenv(runtime_mod.ENABLE_ENV_VAR, raising=False)
    monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
    app = app_mod.create_app(workspace=str(workspace))
    app.state.agent_runtime = AgentRuntimeManager(enabled=True)
    return app


@pytest.fixture(autouse=True)
def root_posts(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """拦截 runtime → toolserver 的 /config/roots 注册(不打真实 8601)。

    autouse:既给注册语义用例提供断言载体,也防止既有 start() 用例
    (fake spawn)误触本机正在运行的 8601 实例。
    """
    posts: List[Any] = []
    monkeypatch.setattr(
        runtime_mod,
        "_post_workspace_root",
        lambda url, path: posts.append((url, str(path))),
    )
    return posts


@pytest.fixture(autouse=True)
def restore_posts(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """拦截 runtime → agent server 的 /workspaces/restore 恢复(不打真实 8602)。

    autouse:既给恢复语义用例提供断言载体,也防止既有 start() 用例
    (fake spawn)误触本机正在运行的 8602 实例。
    """
    posts: List[Any] = []
    monkeypatch.setattr(
        runtime_mod,
        "_post_workspace_restore",
        lambda url, path: posts.append((url, str(path))),
    )
    return posts


@pytest.fixture(autouse=True)
def registry_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把登记表重定向到 tmp config 目录(不触碰真实 config/agent_workspaces.json)。

    autouse:add_workspace_root/restore_workspace 现在都会写登记表,既有用例
    (start、工作区切换等)也得隔离。
    """
    path = tmp_path / "config" / "agent_workspaces.json"
    monkeypatch.setattr(runtime_mod, "REGISTRY_PATH", path)
    return path


# ---------------------------------------------------------------------------
# GET /api/agent/health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_ok_when_both_downstreams_healthy(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_downstream(monkeypatch, _ok_handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["agent"]["healthy"] is True
        assert body["toolserver"]["healthy"] is True
        assert body["runtime"]["enabled"] is True

    def test_unavailable_when_agent_down(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.port == 8602:  # agent 下游连接失败
                raise httpx.ConnectError("connection refused", request=request)
            return _ok_handler(request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        body = client.get("/api/agent/health").json()
        assert body["status"] == "unavailable"
        assert body["agent"]["healthy"] is False
        assert body["toolserver"]["healthy"] is True

    def test_unavailable_when_toolserver_unhealthy(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.port == 8601:
                return httpx.Response(500, json={"error": {"code": "x", "message": "y"}})
            return _ok_handler(request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        body = client.get("/api/agent/health").json()
        assert body["status"] == "unavailable"
        assert body["toolserver"]["healthy"] is False

    def test_unavailable_when_runtime_missing(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app(workspace=str(workspace))
        _patch_downstream(monkeypatch, _ok_handler)
        client = TestClient(app)
        body = client.get("/api/agent/health").json()
        assert body["status"] == "unavailable"

    def test_unavailable_when_runtime_disabled(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_app.state.agent_runtime = AgentRuntimeManager(enabled=False)
        _patch_downstream(monkeypatch, _ok_handler)
        client = TestClient(proxy_app)
        body = client.get("/api/agent/health").json()
        assert body["status"] == "unavailable"
        assert body["runtime"]["enabled"] is False


# ---------------------------------------------------------------------------
# POST /api/agent/sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def test_injects_current_workspace(
        self, proxy_app: Any, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"sessionId": "s-1"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions", json={})
        assert resp.status_code == 200
        assert resp.json() == {"sessionId": "s-1"}
        assert captured[0]["workspaceDir"] == str(workspace)

    def test_explicit_workspace_dir_preserved(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"sessionId": "s-2"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post(
            "/api/agent/sessions",
            json={"workspaceDir": "/elsewhere", "mode": "yolo"},
        )
        assert resp.status_code == 200
        assert captured[0]["workspaceDir"] == "/elsewhere"
        assert captured[0]["mode"] == "yolo"

    def test_no_workspace_selected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app()
        app.state.agent_runtime = AgentRuntimeManager(enabled=True)
        called = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            called.set()
            return httpx.Response(200, json={"sessionId": "s"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(app)
        resp = client.post("/api/agent/sessions", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_workspace"
        assert not called.is_set()  # 未选工作区时不应触达下游

    def test_downstream_error_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"code": "workspace_invalid", "message": "not a dir"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions", json={})
        assert resp.status_code == 400
        assert resp.json() == {
            "error": {"code": "workspace_invalid", "message": "not a dir"}
        }

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions", json={})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"

    def test_runtime_not_started(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app(workspace=str(workspace))
        called = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            called.set()
            return httpx.Response(200, json={})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(app)
        resp = client.post("/api/agent/sessions", json={})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"
        assert not called.is_set()


# ---------------------------------------------------------------------------
# GET /api/agent/sessions(列表)
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/sessions"
            return httpx.Response(
                200,
                json={"sessions": [{"id": "s-1", "title": "检测演示视频"}]},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/sessions")
        assert resp.status_code == 200
        assert resp.json() == {"sessions": [{"id": "s-1", "title": "检测演示视频"}]}

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/sessions")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# GET /api/agent/sessions/{id}/history
# ---------------------------------------------------------------------------


class TestSessionHistory:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = [
            {"kind": "user", "text": "检测这个视频", "images": [], "at": 1},
            {"kind": "tool", "name": "video_meta", "toolCallId": "c1",
             "arguments": None, "output": {}, "isError": False, "at": 2},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/sessions/s-1/history"
            return httpx.Response(200, json={"entries": entries})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/sessions/s-1/history")
        assert resp.status_code == 200
        assert resp.json() == {"entries": entries}

    def test_not_found_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "session_not_found",
                                "message": "unknown session: s-x"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/sessions/s-x/history")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "session_not_found"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/sessions/s-1/history")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# DELETE /api/agent/sessions/{id}
# ---------------------------------------------------------------------------


class TestDeleteSession:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            assert request.url.path == "/sessions/s-1"
            return httpx.Response(200, json={"status": "ok"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.delete("/api/agent/sessions/s-1")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_not_found_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "session_not_found",
                                "message": "unknown session: s-x"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.delete("/api/agent/sessions/s-x")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "session_not_found"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.delete("/api/agent/sessions/s-1")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# POST /api/agent/sessions/{id}/compact
# ---------------------------------------------------------------------------


class TestCompactSession:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/sessions/s-1/compact"
            return httpx.Response(
                200,
                json={"status": "ok", "compacted": True,
                      "beforeTokens": 900, "afterTokens": 120},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/compact")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "ok", "compacted": True,
            "beforeTokens": 900, "afterTokens": 120,
        }

    def test_conflict_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={"error": {"code": "chat_in_progress",
                                "message": "turn in progress"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/compact")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "chat_in_progress"

    def test_not_found_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "session_not_found",
                                "message": "unknown session: s-x"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-x/compact")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "session_not_found"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/compact")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# POST /api/agent/sessions/{id}/recall
# ---------------------------------------------------------------------------


class TestRecallSession:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/sessions/s-1/recall"
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/recall", json={"entryIndex": 3})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert captured == [{"entryIndex": 3}]

    def test_conflict_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={"error": {"code": "chat_in_progress",
                                "message": "turn in progress"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/recall", json={"entryIndex": 0})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "chat_in_progress"

    def test_bad_entry_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"code": "invalid_entry",
                                "message": "not a user entry"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/recall", json={"entryIndex": 99})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_entry"

    def test_not_found_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "session_not_found",
                                "message": "unknown session: s-x"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-x/recall", json={"entryIndex": 0})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "session_not_found"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/recall", json={"entryIndex": 0})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# POST /api/agent/sessions/{id}/mode
# ---------------------------------------------------------------------------


class TestSessionMode:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/sessions/s-1/mode"
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "mode": "auto"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/mode", json={"mode": "auto"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "mode": "auto"}
        assert captured == [{"mode": "auto"}]

    def test_downstream_error_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"code": "invalid_request",
                                "message": "mode must be 'manual' | 'auto' | 'yolo'"}},
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/mode", json={"mode": "paranoid"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/mode", json={"mode": "yolo"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# GET /sessions/{id}/events · POST /sessions/{id}/cancel · /steer
# ---------------------------------------------------------------------------


class TestSessionEventsCancelSteer:
    def test_events_passthrough_with_query(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/sessions/s-1/events"
            assert request.url.params["fromSeq"] == "7"
            return httpx.Response(200, json={"events": [], "inProgress": True})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.get("/api/agent/sessions/s-1/events?fromSeq=7")
        assert resp.status_code == 200
        assert resp.json() == {"events": [], "inProgress": True}

    def test_cancel_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/sessions/s-1/cancel"
            return httpx.Response(200, json={"status": "ok"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_cancel_no_active_turn_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409, json={"error": {"code": "no_active_turn", "message": "no turn"}}
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/cancel")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "no_active_turn"

    def test_steer_passthrough_body(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/sessions/s-1/steer"
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "queued": True})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/steer", json={"input": "补充说明"})
        assert resp.status_code == 200
        assert captured == [{"input": "补充说明"}]

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/sessions/s-1/steer", json={"input": "x"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# POST /api/agent/approval
# ---------------------------------------------------------------------------


class TestApproval:
    def test_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/approval"
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post(
            "/api/agent/approval",
            json={"requestId": "r1", "decision": "approved", "scope": "session"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert captured[0]["decision"] == "approved"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post(
            "/api/agent/approval", json={"requestId": "r1", "decision": "approved"}
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# POST /api/agent/chat — SSE 透传
# ---------------------------------------------------------------------------


# TestClient 会缓冲流式响应(starlette 1.3 + httpx),SSE 测试改为直接驱动
# ASGI 应用(与 test_realtime.py 的 _read_sse 同一模式)。


def _chat_scope(app: Any) -> Dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/agent/chat",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "app": app,
    }


class _SseHarness:
    """驱动 ASGI 应用的 /api/agent/chat 请求,逐块收集响应 body。"""

    def __init__(self, app: Any, payload: Dict[str, Any]) -> None:
        self.chunks: List[bytes] = []
        self.first_received = asyncio.Event()
        self.status: Optional[int] = None
        self._sent = False
        self._body = json.dumps(payload).encode()
        self.task = asyncio.create_task(
            app(_chat_scope(app), self._receive, self._send)
        )

    async def _receive(self) -> Dict[str, Any]:
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await asyncio.sleep(3600)  # 不主动 disconnect;由 cancel 收尾
        return {"type": "http.disconnect"}

    async def _send(self, message: Dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                self.chunks.append(chunk)
                self.first_received.set()

    def body(self) -> bytes:
        return b"".join(self.chunks)


class TestChat:
    def test_sse_passthrough_not_buffered(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()
        downstream_done = threading.Event()

        async def sse_body() -> Any:
            yield b'data: {"type":"text_delta","text":"hello"}\n\n'
            # 若代理整体缓冲响应,首块在 release 之前不会到达客户端。
            while not release.is_set():
                await asyncio.sleep(0.005)
            yield b'data: {"type":"done","reason":"stop"}\n\n'
            downstream_done.set()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/chat"
            assert json.loads(request.content)["input"] == "检测这个视频"
            return httpx.Response(
                200,
                content=sse_body(),
                headers={"Content-Type": "text/event-stream"},
            )

        _patch_downstream(monkeypatch, handler)

        async def main() -> Any:
            harness = _SseHarness(
                proxy_app, {"sessionId": "s-1", "input": "检测这个视频"}
            )
            # 首块必须在下游尚未发完(release 未 set)时到达 → 逐块转发,不缓冲。
            await asyncio.wait_for(harness.first_received.wait(), timeout=5)
            assert not downstream_done.is_set()
            release.set()
            await asyncio.wait_for(harness.task, timeout=5)
            return harness

        harness = asyncio.run(main())
        assert harness.status == 200
        # 字节级透传:与下游产出完全一致。
        assert harness.body() == (
            b'data: {"type":"text_delta","text":"hello"}\n\n'
            b'data: {"type":"done","reason":"stop"}\n\n'
        )
        assert downstream_done.is_set()

    def test_client_disconnect_cancels_downstream(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """客户端断连(cancel ASGI 任务)必须取消下游请求。"""
        downstream_closed = threading.Event()

        async def sse_body() -> Any:
            try:
                yield b'data: {"type":"text_delta","text":"chunk"}\n\n'
                while True:
                    await asyncio.sleep(0.01)
            except (asyncio.CancelledError, GeneratorExit):
                downstream_closed.set()
                raise

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=sse_body(),
                headers={"Content-Type": "text/event-stream"},
            )

        _patch_downstream(monkeypatch, handler)

        async def main() -> None:
            harness = _SseHarness(proxy_app, {"sessionId": "s", "input": "hi"})
            await asyncio.wait_for(harness.first_received.wait(), timeout=5)
            harness.task.cancel()  # 客户端断连
            try:
                await harness.task
            except asyncio.CancelledError:
                pass
            # 代理 generator 的 finally 应 aclose 下游响应 → 下游流被取消。
            await asyncio.wait_for(
                asyncio.to_thread(downstream_closed.wait, 5), timeout=6
            )

        asyncio.run(main())
        assert downstream_closed.is_set()

    def test_downstream_error_status_passthrough(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404, json={"error": {"code": "session_not_found", "message": "?"}}
            )

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/chat", json={"sessionId": "x", "input": "hi"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "session_not_found"

    def test_downstream_unreachable(
        self, proxy_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_downstream(monkeypatch, handler)
        client = TestClient(proxy_app)
        resp = client.post("/api/agent/chat", json={"sessionId": "x", "input": "hi"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# AgentRuntimeManager 生命周期(假 Popen,不起真实子进程)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, argv: List[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.stdout = io.StringIO("")
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        # 模拟 SIGTERM 后退出:首次 wait 即置返回码。
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


class _StubbornProc(_FakeProc):
    """SIGTERM 不退出、SIGKILL 才退出的子进程(wait 按 returncode 驱动)。"""

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode


class TestRuntimeManager:
    def test_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(runtime_mod.ENABLE_ENV_VAR, "false")
        spawned: List[Any] = []
        mgr = AgentRuntimeManager(spawn=lambda *a, **k: spawned.append(a))
        mgr.start()
        assert spawned == []
        snap = mgr.snapshot()
        assert snap["enabled"] is False
        assert snap["agent"]["state"] == "disabled"

    def test_enabled_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(runtime_mod.ENABLE_ENV_VAR, raising=False)
        assert runtime_mod.runtime_enabled() is True

    def test_ports_overridable_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(runtime_mod.AGENT_PORT_ENV_VAR, "8711")
        monkeypatch.setenv(runtime_mod.TOOLSERVER_PORT_ENV_VAR, "8611")
        mgr = AgentRuntimeManager(enabled=False)
        assert mgr.agent_url == "http://127.0.0.1:8711"
        assert mgr.toolserver_url == "http://127.0.0.1:8611"
        snap = mgr.snapshot()
        assert snap["agent"]["port"] == 8711
        assert snap["toolserver"]["port"] == 8611

    def test_invalid_port_env_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(runtime_mod.AGENT_PORT_ENV_VAR, "not-a-port")
        monkeypatch.delenv(runtime_mod.TOOLSERVER_PORT_ENV_VAR, raising=False)
        mgr = AgentRuntimeManager(enabled=False)
        assert mgr.agent_url == "http://127.0.0.1:8602"
        assert mgr.toolserver_url == "http://127.0.0.1:8601"

    def test_spawn_commands_and_env(self, tmp_path: Path) -> None:
        procs: Dict[str, _FakeProc] = {}

        def fake_spawn(argv: List[str], **kwargs: Any) -> _FakeProc:
            proc = _FakeProc(argv, **kwargs)
            procs[argv[1] if argv[0] != "npx" else "agent"] = proc
            return proc

        ws = tmp_path / "ws"
        ws.mkdir()
        mgr = AgentRuntimeManager(
            workspace=ws,
            enabled=True,
            spawn=fake_spawn,
            port_probe=lambda port: False,
        )
        mgr.start()

        ts = procs["-m"]  # [python, -m, traffic_analyzer.toolserver, ...]
        assert "traffic_analyzer.toolserver" in ts.argv
        assert ts.argv[ts.argv.index("--workspace") + 1] == str(ws)
        assert ts.argv[ts.argv.index("--port") + 1] == "8601"

        agent = procs["agent"]
        assert agent.argv == ["npx", "tsx", "src/server/main.ts"]
        assert agent.kwargs["cwd"].endswith("agent")
        assert agent.kwargs["env"]["AGENT_PORT"] == "8602"
        assert agent.kwargs["env"]["TOOLSERVER_URL"] == "http://127.0.0.1:8601"
        # 自己 spawn 的 agent 由 env 在启动时自行恢复磁盘历史会话
        # (避免「spawn 后立即 HTTP restore 撞上子进程尚未 listen」的竞态)
        assert agent.kwargs["env"]["AGENT_RESTORE_WORKSPACES"] == str(ws)

        snap = mgr.snapshot()
        assert snap["toolserver"]["state"] == "running"
        assert snap["agent"]["state"] == "running"

    def test_workspace_defaults_to_repo_root(self, tmp_path: Path) -> None:
        spawned: List[List[str]] = []
        mgr = AgentRuntimeManager(
            workspace=None,
            enabled=True,
            repo_root=tmp_path,
            spawn=lambda argv, **k: (spawned.append(argv), _FakeProc(argv))[1],
            port_probe=lambda port: False,
        )
        mgr.start()
        ts_argv = spawned[0]
        assert ts_argv[ts_argv.index("--workspace") + 1] == str(tmp_path)

    def test_port_occupied_degrades(self) -> None:
        spawned: List[Any] = []
        mgr = AgentRuntimeManager(
            enabled=True,
            spawn=lambda *a, **k: spawned.append(a),
            port_probe=lambda port: True,
        )
        mgr.start()  # 不抛异常
        assert spawned == []
        snap = mgr.snapshot()
        assert snap["toolserver"]["state"] == "port_occupied"
        assert snap["agent"]["state"] == "port_occupied"

    def test_spawn_failure_degrades(self) -> None:
        def bad_spawn(*a: Any, **k: Any) -> Any:
            raise OSError("npx not found")

        mgr = AgentRuntimeManager(
            enabled=True, spawn=bad_spawn, port_probe=lambda port: False
        )
        mgr.start()  # 不抛异常
        snap = mgr.snapshot()
        assert snap["toolserver"]["state"] == "failed"
        assert snap["agent"]["state"] == "failed"

    def test_stop_sigterm_process_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stop() 对每个子进程的进程组发 SIGTERM;正常退出不升级 SIGKILL。"""
        killpg_calls: List[Any] = []
        monkeypatch.setattr(runtime_mod.os, "getpgid", lambda pid: pid + 100)
        monkeypatch.setattr(
            runtime_mod.os, "killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        procs: List[_FakeProc] = []

        def fake_spawn(argv: List[str], **kwargs: Any) -> _FakeProc:
            proc = _FakeProc(argv, **kwargs)
            proc.pid = 1000 + len(procs)
            procs.append(proc)
            return proc

        mgr = AgentRuntimeManager(
            enabled=True, spawn=fake_spawn, port_probe=lambda port: False
        )
        mgr.start()
        assert len(procs) == 2
        mgr.stop()
        assert killpg_calls == [
            (1100, signal.SIGTERM),
            (1101, signal.SIGTERM),
        ]
        assert all(p.returncode == -15 for p in procs)
        mgr.stop()  # 幂等:进程已退出,不再发信号
        assert len(killpg_calls) == 2

    def test_stop_escalates_to_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIGTERM 超时不退出 → 对进程组升级 SIGKILL。"""
        killpg_calls: List[Any] = []

        def fake_killpg(pgid: int, sig: int) -> None:
            killpg_calls.append((pgid, sig))
            if sig == signal.SIGKILL:
                for p in procs:
                    if p.pid + 100 == pgid:
                        p.returncode = -9

        monkeypatch.setattr(runtime_mod.os, "getpgid", lambda pid: pid + 100)
        monkeypatch.setattr(runtime_mod.os, "killpg", fake_killpg)
        procs: List[_StubbornProc] = []

        def fake_spawn(argv: List[str], **kwargs: Any) -> _StubbornProc:
            proc = _StubbornProc(argv, **kwargs)
            proc.pid = 2000 + len(procs)
            procs.append(proc)
            return proc

        mgr = AgentRuntimeManager(
            enabled=True, spawn=fake_spawn, port_probe=lambda port: False
        )
        mgr.start()
        mgr.stop()
        assert killpg_calls == [
            (2100, signal.SIGTERM), (2100, signal.SIGKILL),
            (2101, signal.SIGTERM), (2101, signal.SIGKILL),
        ]
        assert all(p.returncode == -9 for p in procs)

    def test_stop_process_group_already_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """竞态:poll 后进程刚好退出,getpgid 抛 ProcessLookupError → 不抛异常。"""
        def bad_getpgid(pid: int) -> int:
            raise ProcessLookupError(pid)

        monkeypatch.setattr(runtime_mod.os, "getpgid", bad_getpgid)
        procs: List[_FakeProc] = []
        mgr = AgentRuntimeManager(
            enabled=True,
            spawn=lambda argv, **k: (procs.append(_FakeProc(argv)), procs[-1])[1],
            port_probe=lambda port: False,
        )
        mgr.start()
        mgr.stop()  # 不抛异常


# ---------------------------------------------------------------------------
# add_workspace_root:工作区热注册进 toolserver 允许根(免重启)
# ---------------------------------------------------------------------------


class TestAddWorkspaceRoot:
    def test_post_workspace_root_url_and_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_post_workspace_root 本身:URL 路径 /config/roots,body {"path": ...}。"""
        calls: List[Any] = []
        monkeypatch.setattr(
            runtime_mod,
            "httpx",
            SimpleNamespace(post=lambda *a, **k: calls.append((a, k))),
        )
        monkeypatch.setattr(
            runtime_mod, "_post_workspace_root", _REAL_POST_WORKSPACE_ROOT
        )
        monkeypatch.delenv(runtime_mod.ADMIN_TOKEN_ENV_VAR, raising=False)
        runtime_mod._post_workspace_root("http://127.0.0.1:8601", tmp_path)
        assert calls == [
            (
                ("http://127.0.0.1:8601/config/roots",),
                {"json": {"path": str(tmp_path)},
                 "headers": None,
                 "timeout": runtime_mod._REGISTER_ROOT_TIMEOUT},
            )
        ]

    def test_post_workspace_root_sends_token_when_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """配置 TOOLSERVER_ADMIN_TOKEN 时,_post_workspace_root 带 X-Token 头。"""
        calls: List[Any] = []
        monkeypatch.setattr(
            runtime_mod,
            "httpx",
            SimpleNamespace(post=lambda *a, **k: calls.append((a, k))),
        )
        monkeypatch.setattr(
            runtime_mod, "_post_workspace_root", _REAL_POST_WORKSPACE_ROOT
        )
        monkeypatch.setenv(runtime_mod.ADMIN_TOKEN_ENV_VAR, "admin-token")
        runtime_mod._post_workspace_root("http://127.0.0.1:8601", tmp_path)
        assert calls == [
            (
                ("http://127.0.0.1:8601/config/roots",),
                {
                    "json": {"path": str(tmp_path)},
                    "headers": {"X-Token": "admin-token"},
                    "timeout": runtime_mod._REGISTER_ROOT_TIMEOUT,
                },
            )
        ]

    def test_posts_config_roots(
        self, root_posts: List[Any], tmp_path: Path
    ) -> None:
        mgr = AgentRuntimeManager(enabled=True)
        mgr.add_workspace_root(tmp_path)
        assert root_posts == [
            ("http://127.0.0.1:8601", str(tmp_path))
        ]

    def test_failure_only_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: Any, tmp_path: Path
    ) -> None:
        def boom(url: str, path: Path) -> None:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(runtime_mod, "_post_workspace_root", boom)
        mgr = AgentRuntimeManager(enabled=True)
        with caplog.at_level(logging.WARNING, logger="traffic_analyzer.web.agentproxy.runtime"):
            mgr.add_workspace_root(tmp_path)  # 不抛异常
        assert "failed to register workspace root" in caplog.text

    def test_disabled_runtime_skips(
        self, root_posts: List[Any], tmp_path: Path
    ) -> None:
        mgr = AgentRuntimeManager(enabled=False)
        mgr.add_workspace_root(tmp_path)
        assert root_posts == []

    def test_start_registers_current_workspace(
        self, root_posts: List[Any], tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        mgr = AgentRuntimeManager(
            workspace=ws,
            enabled=True,
            spawn=lambda argv, **k: _FakeProc(argv),
            port_probe=lambda port: False,
        )
        mgr.start()
        assert root_posts == [("http://127.0.0.1:8601", str(ws))]


# ---------------------------------------------------------------------------
# POST /api/workspace:切换工作区后热注册进 toolserver
# ---------------------------------------------------------------------------


class TestWorkspaceSwitchRegistersRoot:
    def test_set_workspace_registers_root(
        self,
        root_posts: List[Any],
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app(workspace=str(workspace))
        app.state.agent_runtime = AgentRuntimeManager(enabled=True)
        new_ws = tmp_path / "ws2"
        new_ws.mkdir()
        client = TestClient(app)
        resp = client.post("/api/workspace", json={"path": str(new_ws)})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"path": str(new_ws.resolve())}
        assert root_posts == [
            ("http://127.0.0.1:8601", str(new_ws.resolve()))
        ]

    def test_set_workspace_without_runtime_keeps_semantics(
        self,
        root_posts: List[Any],
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app(workspace=str(workspace))
        new_ws = tmp_path / "ws2"
        new_ws.mkdir()
        client = TestClient(app)
        resp = client.post("/api/workspace", json={"path": str(new_ws)})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"path": str(new_ws.resolve())}
        assert root_posts == []  # 无 runtime:不注册,也不报错

    def test_set_workspace_triggers_agent_restore(
        self,
        root_posts: List[Any],
        restore_posts: List[Any],
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """切换工作区同时触发两个副作用:toolserver 注册允许根 + agent 恢复会话。"""
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app(workspace=str(workspace))
        app.state.agent_runtime = AgentRuntimeManager(enabled=True)
        new_ws = tmp_path / "ws2"
        new_ws.mkdir()
        client = TestClient(app)
        resp = client.post("/api/workspace", json={"path": str(new_ws)})
        assert resp.status_code == 200, resp.text
        resolved = str(new_ws.resolve())
        assert root_posts == [("http://127.0.0.1:8601", resolved)]
        assert restore_posts == [("http://127.0.0.1:8602", resolved)]

    def test_set_workspace_without_runtime_skips_restore(
        self,
        restore_posts: List[Any],
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app(workspace=str(workspace))
        new_ws = tmp_path / "ws2"
        new_ws.mkdir()
        client = TestClient(app)
        resp = client.post("/api/workspace", json={"path": str(new_ws)})
        assert resp.status_code == 200, resp.text
        assert restore_posts == []  # 无 runtime:不恢复,也不报错


# ---------------------------------------------------------------------------
# restore_workspace:让 agent server 恢复工作区磁盘历史会话(免重启)
# ---------------------------------------------------------------------------


class TestRestoreWorkspace:
    def test_post_workspace_restore_url_and_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_post_workspace_restore 本身:URL /workspaces/restore,body {"workspaceDir": ...}。"""
        calls: List[Any] = []
        monkeypatch.setattr(
            runtime_mod,
            "httpx",
            SimpleNamespace(post=lambda *a, **k: calls.append((a, k))),
        )
        monkeypatch.setattr(
            runtime_mod, "_post_workspace_restore", _REAL_POST_WORKSPACE_RESTORE
        )
        runtime_mod._post_workspace_restore("http://127.0.0.1:8602", tmp_path)
        assert calls == [
            (
                ("http://127.0.0.1:8602/workspaces/restore",),
                {"json": {"workspaceDir": str(tmp_path)},
                 "timeout": runtime_mod._REGISTER_ROOT_TIMEOUT},
            )
        ]

    def test_posts_workspaces_restore(
        self, restore_posts: List[Any], tmp_path: Path
    ) -> None:
        mgr = AgentRuntimeManager(enabled=True)
        mgr.restore_workspace(tmp_path)
        assert restore_posts == [
            ("http://127.0.0.1:8602", str(tmp_path))
        ]

    def test_failure_only_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: Any, tmp_path: Path
    ) -> None:
        def boom(url: str, path: Path) -> None:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(runtime_mod, "_post_workspace_restore", boom)
        mgr = AgentRuntimeManager(enabled=True)
        with caplog.at_level(logging.WARNING, logger="traffic_analyzer.web.agentproxy.runtime"):
            mgr.restore_workspace(tmp_path)  # 不抛异常
        assert "failed to restore workspace" in caplog.text

    def test_disabled_runtime_skips(
        self, restore_posts: List[Any], tmp_path: Path
    ) -> None:
        mgr = AgentRuntimeManager(enabled=False)
        mgr.restore_workspace(tmp_path)
        assert restore_posts == []

    def test_restore_removes_missing_workspace_from_registry(
        self,
        restore_posts: List[Any],
        registry_file: Path,
        caplog: Any,
        tmp_path: Path,
    ) -> None:
        """restore_workspace 发现目录已不存在时,从登记表清理并跳过 HTTP。"""
        ws = (tmp_path / "ws").resolve()
        ws.mkdir()
        mgr = AgentRuntimeManager(enabled=True)
        mgr.add_workspace_root(ws)
        assert runtime_mod.registered_workspaces() == [str(ws)]

        shutil.rmtree(ws)
        with caplog.at_level(
            logging.WARNING, logger="traffic_analyzer.web.agentproxy.runtime"
        ):
            mgr.restore_workspace(ws)
        assert restore_posts == []  # 目录已删,不发 HTTP
        assert runtime_mod.registered_workspaces() == []
        assert "no longer exists" in caplog.text

    def test_start_spawns_agent_with_restore_env(
        self, restore_posts: List[Any], tmp_path: Path
    ) -> None:
        """自己 spawn 的 agent 走 AGENT_RESTORE_WORKSPACES 启动自恢复,
        不再发 HTTP restore(spawn 后立即调用会撞上子进程尚未 listen 的竞态)。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        mgr = AgentRuntimeManager(
            workspace=ws,
            enabled=True,
            spawn=lambda argv, **k: _FakeProc(argv),
            port_probe=lambda port: False,
        )
        mgr.start()
        assert restore_posts == []

    def test_start_restores_external_agent_over_http(
        self, restore_posts: List[Any], tmp_path: Path
    ) -> None:
        """端口被外部已运行 agent 占用时(未 spawn,env 不覆盖),经 HTTP
        补一次 restore(实例在听,无启动竞态)。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        mgr = AgentRuntimeManager(
            workspace=ws,
            enabled=True,
            spawn=lambda argv, **k: _FakeProc(argv),
            port_probe=lambda port: True,
        )
        mgr.start()
        assert restore_posts == [("http://127.0.0.1:8602", str(ws))]


# ---------------------------------------------------------------------------
# 工作区登记表:add_workspace_root/restore_workspace 追加登记,GET /sessions 聚合
# ---------------------------------------------------------------------------


class TestWorkspaceRegistry:
    def test_missing_file_returns_empty(self, registry_file: Path) -> None:
        assert runtime_mod.registered_workspaces() == []

    def test_append_and_dedup(
        self,
        registry_file: Path,
        root_posts: List[Any],
        restore_posts: List[Any],
        tmp_path: Path,
    ) -> None:
        ws_a = (tmp_path / "ws-a").resolve()
        ws_b = (tmp_path / "ws-b").resolve()
        ws_a.mkdir()
        ws_b.mkdir()
        mgr = AgentRuntimeManager(enabled=True)
        mgr.add_workspace_root(ws_a)
        mgr.restore_workspace(ws_b)
        # 重复登记(含不同写法的同一路径):去重,不追加。
        mgr.add_workspace_root(ws_a)
        mgr.restore_workspace(ws_a / ".." / "ws-a")
        assert runtime_mod.registered_workspaces() == [str(ws_a), str(ws_b)]
        # 落盘内容:JSON 数组,追加序。
        assert json.loads(registry_file.read_text(encoding="utf-8")) == [
            str(ws_a),
            str(ws_b),
        ]

    def test_disabled_runtime_still_records(
        self, registry_file: Path, tmp_path: Path
    ) -> None:
        """登记表与下游 HTTP 解耦:runtime 禁用时不发请求,但仍登记。"""
        mgr = AgentRuntimeManager(enabled=False)
        mgr.restore_workspace(tmp_path)
        assert runtime_mod.registered_workspaces() == [str(tmp_path.resolve())]

    def test_write_failure_only_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: Any,
        root_posts: List[Any],
        tmp_path: Path,
    ) -> None:
        # REGISTRY_PATH 指向一个目录:read_text/write_text 均 OSError。
        monkeypatch.setattr(runtime_mod, "REGISTRY_PATH", tmp_path)
        mgr = AgentRuntimeManager(enabled=True)
        with caplog.at_level(
            logging.WARNING, logger="traffic_analyzer.web.agentproxy.runtime"
        ):
            mgr.add_workspace_root(tmp_path / "ws")  # 不抛异常
        assert "failed to record workspace" in caplog.text

    def test_corrupt_file_returns_empty(
        self, registry_file: Path, caplog: Any
    ) -> None:
        registry_file.parent.mkdir(parents=True, exist_ok=True)
        registry_file.write_text("{not json", encoding="utf-8")
        with caplog.at_level(
            logging.WARNING, logger="traffic_analyzer.web.agentproxy.runtime"
        ):
            assert runtime_mod.registered_workspaces() == []
        assert "workspace registry unreadable" in caplog.text

    def test_non_list_file_returns_empty(self, registry_file: Path) -> None:
        registry_file.parent.mkdir(parents=True, exist_ok=True)
        registry_file.write_text('{"ws": "/a"}', encoding="utf-8")
        assert runtime_mod.registered_workspaces() == []


class TestListSessionsPassthrough:
    """GET /api/agent/sessions 变为纯透传,不再先做 restore 写副作用。"""

    def test_passthrough_no_restore_side_effect(
        self,
        proxy_app: Any,
        monkeypatch: pytest.MonkeyPatch,
        registry_file: Path,
    ) -> None:
        registry_file.parent.mkdir(parents=True, exist_ok=True)
        registry_file.write_text(
            json.dumps(["/ws/a", "/ws/b"]), encoding="utf-8"
        )
        calls: List[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.path == "/sessions":
                return httpx.Response(200, json={"sessions": [{"id": "s-1"}]})
            return httpx.Response(404)

        _patch_downstream(monkeypatch, handler)
        resp = TestClient(proxy_app).get("/api/agent/sessions")
        assert resp.status_code == 200
        assert resp.json() == {"sessions": [{"id": "s-1"}]}
        # 代理层只发了一个 GET /sessions,没有 /workspaces/restore。
        assert calls == [("GET", "/sessions")]
