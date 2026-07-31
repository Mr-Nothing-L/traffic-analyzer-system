"""Presence tests: heartbeat, roster, TTL eviction, authenticated usernames."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from traffic_analyzer.web import auth
from traffic_analyzer.web.app import create_app

from .conftest import _make_workspace


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(workspace=str(_make_workspace(tmp_path))))


class TestPresence:
    def test_heartbeat_and_roster(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        resp = client.post("/api/presence", json={"viewing": "v1", "editing": None})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        roster = client.get("/api/presence").json()
        assert len(roster) == 1
        entry = roster[0]
        assert entry["user"] == "local"  # 认证关闭时记 'local'
        assert entry["viewing"] == "v1"
        assert entry["editing"] is None
        assert entry["ts"] > 0

    def test_heartbeat_overwrites_previous(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        client.post("/api/presence", json={"viewing": "v1", "editing": None})
        client.post("/api/presence", json={"viewing": None, "editing": "v2"})
        roster = client.get("/api/presence").json()
        assert len(roster) == 1
        assert roster[0]["viewing"] is None
        assert roster[0]["editing"] == "v2"

    def test_ttl_evicts_stale_entries(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        client.app.state.presence.ttl = 0.05
        client.post("/api/presence", json={"viewing": "v1", "editing": None})
        assert len(client.get("/api/presence").json()) == 1
        time.sleep(0.1)
        assert client.get("/api/presence").json() == []

    def test_roster_uses_authenticated_username(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(auth.USERS_ENV_VAR, "zhangsan:pass1")
        monkeypatch.setenv(auth.SECRET_ENV_VAR, "test-secret")
        client = _client(tmp_path)
        client.post("/api/auth/login", json={"username": "zhangsan", "password": "pass1"})
        client.post("/api/presence", json={"viewing": "v1", "editing": None})
        roster = client.get("/api/presence").json()
        assert [e["user"] for e in roster] == ["zhangsan"]

    def test_presence_requires_auth_when_enabled(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(auth.USERS_ENV_VAR, "zhangsan:pass1")
        monkeypatch.setenv(auth.SECRET_ENV_VAR, "test-secret")
        client = _client(tmp_path)
        assert client.get("/api/presence").status_code == 401
        assert (
            client.post("/api/presence", json={"viewing": None, "editing": None}).status_code
            == 401
        )
