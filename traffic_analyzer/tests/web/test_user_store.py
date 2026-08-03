"""user_store tests: CRUD, PBKDF2 hashing, env import, deactivate semantics."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web import auth, user_store
from traffic_analyzer.web.app import create_app

from .conftest import _make_workspace


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "users.db"


class TestCrud:
    def test_add_get_list(self, db: Path) -> None:
        assert user_store.add_user("alice", "pw1", db) is True
        assert user_store.add_user("bob", "pw2", db) is True
        user = user_store.get_user("alice", db)
        assert user is not None
        assert user["username"] == "alice"
        assert user["active"] is True
        assert isinstance(user["created_at"], float)
        assert "password" not in user and "password_hash" not in user
        assert [u["username"] for u in user_store.list_users(db)] == ["alice", "bob"]

    def test_add_duplicate_false(self, db: Path) -> None:
        assert user_store.add_user("alice", "pw1", db) is True
        assert user_store.add_user("alice", "other", db) is False
        # 重复添加不覆盖原密码。
        assert user_store.verify_password("alice", "pw1", db) is True

    def test_add_rejects_empty(self, db: Path) -> None:
        with pytest.raises(ValueError):
            user_store.add_user("", "pw", db)
        with pytest.raises(ValueError):
            user_store.add_user("alice", "", db)

    def test_get_unknown_none(self, db: Path) -> None:
        assert user_store.get_user("nobody", db) is None

    def test_remove(self, db: Path) -> None:
        user_store.add_user("alice", "pw1", db)
        assert user_store.remove_user("alice", db) is True
        assert user_store.remove_user("alice", db) is False
        assert user_store.get_user("alice", db) is None

    def test_set_password(self, db: Path) -> None:
        user_store.add_user("alice", "old", db)
        assert user_store.set_password("alice", "new", db) is True
        assert user_store.verify_password("alice", "new", db) is True
        assert user_store.verify_password("alice", "old", db) is False
        assert user_store.set_password("nobody", "x", db) is False


class TestHashing:
    def test_hash_format_and_verify(self, db: Path) -> None:
        user_store.add_user("alice", "s3cret", db)
        with sqlite3.connect(str(db)) as conn:
            stored = conn.execute(
                "SELECT password_hash FROM accounts WHERE username = 'alice'"
            ).fetchone()[0]
        scheme, iter_s, salt_hex, hash_hex = stored.split("$")
        assert scheme == "pbkdf2"
        assert int(iter_s) == 100_000
        assert len(bytes.fromhex(salt_hex)) == 16
        assert "s3cret" not in stored
        assert user_store.verify_password("alice", "s3cret", db) is True
        assert user_store.verify_password("alice", "wrong", db) is False
        assert user_store.verify_password("nobody", "s3cret", db) is False

    def test_salts_differ(self, db: Path) -> None:
        user_store.add_user("a", "same", db)
        user_store.add_user("b", "same", db)
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute("SELECT password_hash FROM accounts").fetchall()
        assert rows[0][0] != rows[1][0]

    def test_malformed_hash_rejected(self, db: Path) -> None:
        user_store.add_user("alice", "pw", db)
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE accounts SET password_hash = 'bogus' WHERE username = 'alice'"
            )
        assert user_store.verify_password("alice", "pw", db) is False


class TestImportFromEnv:
    def test_import_and_dedupe(self, db: Path) -> None:
        assert user_store.import_from_env("zhangsan:pass1,lisi:pass2", db) == 2
        # 重复导入全部跳过;新增用户只加一个。
        assert user_store.import_from_env("zhangsan:pass1,wangwu:pass3", db) == 1
        assert [u["username"] for u in user_store.list_users(db)] == [
            "lisi",
            "wangwu",
            "zhangsan",
        ]
        assert user_store.verify_password("zhangsan", "pass1", db) is True

    def test_import_skips_malformed(self, db: Path) -> None:
        # 空段、无冒号、空密码、含 cookie 分隔符 '|' 的用户名都跳过(同 auth 口径)。
        assert user_store.import_from_env("ok:pw,,noColon,:pw2,a|b:pw3", db) == 1
        assert [u["username"] for u in user_store.list_users(db)] == ["ok"]

    def test_import_does_not_overwrite_password(self, db: Path) -> None:
        user_store.add_user("alice", "original", db)
        assert user_store.import_from_env("alice:changed", db) == 0
        assert user_store.verify_password("alice", "original", db) is True


class TestDeactivate:
    def test_deactivate_blocks_verify(self, db: Path) -> None:
        user_store.add_user("alice", "pw", db)
        assert user_store.deactivate("alice", db) is True
        assert user_store.deactivate("nobody", db) is False
        assert user_store.verify_password("alice", "pw", db) is False
        user = user_store.get_user("alice", db)
        assert user is not None and user["active"] is False

    def test_deactivated_user_login_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(auth.SECRET_ENV_VAR, "test-secret")
        user_store.add_user("alice", "pw")  # DB_PATH 已由 conftest 隔离到 tmp_path
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        login = lambda: client.post(  # noqa: E731
            "/api/auth/login", json={"username": "alice", "password": "pw"}
        )
        assert login().status_code == 200
        user_store.deactivate("alice")
        assert login().status_code == 401
