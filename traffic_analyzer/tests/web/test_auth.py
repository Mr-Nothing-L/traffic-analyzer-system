"""Auth tests: disabled-by-default, login/me/logout, IP binding, page/API gating."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web import auth
from traffic_analyzer.web.app import create_app

from .conftest import _make_results, _make_workspace

_USERS = "zhangsan:pass1,lisi:pass2"
_SECRET = "test-secret"


@pytest.fixture()
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable auth with a fixed secret (no .env write-back)."""
    monkeypatch.setenv(auth.USERS_ENV_VAR, _USERS)
    monkeypatch.setenv(auth.SECRET_ENV_VAR, _SECRET)


def _login(client: TestClient, username: str = "zhangsan", password: str = "pass1"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestAuthDisabled:
    def test_api_and_pages_open_without_env(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/results/v1").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 200

    def test_me_reports_local_user(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json() == {"username": "local", "login_ts": None, "login_ip": None}

    def test_login_404_when_disabled(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        assert _login(client).status_code == 404


@pytest.mark.usefixtures("auth_env")
class TestAuthEnabled:
    def test_wrong_password_401(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        assert _login(client, password="wrong").status_code == 401
        assert _login(client, username="nobody").status_code == 401

    def test_login_sets_signed_httponly_cookie(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        resp = _login(client)
        assert resp.status_code == 200
        set_cookie = resp.headers["set-cookie"]
        assert "ta_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "samesite=lax" in set_cookie.lower()
        body = resp.json()
        assert body["username"] == "zhangsan"
        assert body["login_ip"] == "testclient"
        assert isinstance(body["login_ts"], int)

    def test_me_with_session(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        _login(client)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "zhangsan"

    def test_logout_clears_session(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        _login(client)
        assert client.get("/api/auth/me").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401

    def test_unauthenticated_api_401(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        resp = client.get("/api/dashboard")
        assert resp.status_code == 401

    def test_unauthenticated_page_302_login(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
        # 登录页与其静态资源不鉴权(否则登录页自身也打不开)。
        assert client.get("/login", follow_redirects=False).status_code != 302
        assert client.get("/js/main.js", follow_redirects=False).status_code != 302

    def test_api_ok_after_login(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        _login(client)
        assert client.get("/api/results/v1").status_code == 200

    def test_ip_change_invalidates_session(self, tmp_path: Path) -> None:
        app = create_app(workspace=str(_make_workspace(tmp_path)))
        client_a = TestClient(app, client=("1.2.3.4", 5000))
        assert _login(client_a).status_code == 200
        assert client_a.get("/api/auth/me").json()["login_ip"] == "1.2.3.4"
        # 同一 cookie 从另一个 IP 访问 → 401。
        client_b = TestClient(app, client=("9.9.9.9", 5000))
        client_b.cookies.set(auth.COOKIE_NAME, client_a.cookies.get(auth.COOKIE_NAME))
        assert client_b.get("/api/auth/me").status_code == 401

    def test_tampered_cookie_401(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        _login(client)
        valid = client.cookies.get(auth.COOKIE_NAME)
        # 篡改签名末位(保持 cookie 字符集合法),签名比对必须失败。
        flipped = valid[:-1] + ("0" if valid[-1] != "0" else "1")
        client.cookies.clear()
        client.cookies.set(auth.COOKIE_NAME, flipped)
        assert client.get("/api/auth/me").status_code == 401


class TestSecretBootstrap:
    def test_secret_generated_and_written_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        monkeypatch.setattr(auth, "_ENV_PATH", env_file)
        monkeypatch.setenv(auth.USERS_ENV_VAR, _USERS)
        monkeypatch.delenv(auth.SECRET_ENV_VAR, raising=False)
        config = auth.configure()
        assert config.enabled and config.secret
        # 密钥追加写回 .env,且再次 configure 复用同一密钥(重启会话不失效)。
        assert f"{auth.SECRET_ENV_VAR}={config.secret}" in env_file.read_text()
        assert auth.configure().secret == config.secret

    def test_existing_env_secret_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(f"{auth.SECRET_ENV_VAR}=from-dotenv\n", encoding="utf-8")
        monkeypatch.setattr(auth, "_ENV_PATH", env_file)
        monkeypatch.setenv(auth.USERS_ENV_VAR, _USERS)
        monkeypatch.delenv(auth.SECRET_ENV_VAR, raising=False)
        assert auth.configure().secret == "from-dotenv"
