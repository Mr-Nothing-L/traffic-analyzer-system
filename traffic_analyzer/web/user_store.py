"""SQLite-backed user account store for the web UI.

Accounts live in ``traffic_analyzer/config/users.db`` (table ``accounts``).
Passwords are stored as PBKDF2-HMAC-SHA256 hashes in the format
``pbkdf2$<iterations>$<salt_hex>$<hash_hex>`` — plaintext never persists.

[文件说明]
作用:web UI 账号的 SQLite 存储(config/users.db,accounts 表);密码以
PBKDF2-HMAC-SHA256(10 万迭代,16B 盐)哈希保存,格式
pbkdf2$iter$salt_hex$hash_hex;提供 add/get/verify/list/remove/set_password/
deactivate 与 import_from_env(从 TRAFFIC_ANALYZER_USERS 字符串导入)。
上游:web/auth.py(登录校验与首次启动迁移);scripts/manage_users.py(CLI)。
下游:仅 SQLite 文件;DB_PATH 常量供测试 monkeypatch。读操作(get/verify/list)
在库文件不存在时直接返回空结果,不会在磁盘上创建空库;写操作才创建文件。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# traffic_analyzer/web/user_store.py → parents[1] = traffic_analyzer/。
# 测试 monkeypatch 此常量(或给各函数显式传 db_path)。
DB_PATH = Path(__file__).resolve().parents[1] / "config" / "users.db"

_PBKDF2_ITERATIONS = 100_000
_SALT_BYTES = 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at REAL,
    active INTEGER DEFAULT 1
)
"""


def _connect(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA)
    return conn


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 hash in ``pbkdf2$iter$salt_hex$hash_hex`` format."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        scheme, iter_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def add_user(
    username: str, password: str, db_path: Optional[Union[str, Path]] = None
) -> bool:
    """Create an account; ``False`` when the username already exists."""
    username = username.strip()
    if not username or not password:
        raise ValueError("username and password must be non-empty")
    with _connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO accounts (username, password_hash, created_at, active)"
                " VALUES (?, ?, ?, 1)",
                (username, hash_password(password), time.time()),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def get_user(
    username: str, db_path: Optional[Union[str, Path]] = None
) -> Optional[Dict[str, Any]]:
    """Account dict (no hash) or ``None`` when unknown."""
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    if not path.is_file():
        return None
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT username, created_at, active FROM accounts WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return {"username": row[0], "created_at": row[1], "active": bool(row[2])}


def verify_password(
    username: str, password: str, db_path: Optional[Union[str, Path]] = None
) -> bool:
    """``True`` only for an existing, active account with a matching password."""
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    if not path.is_file():
        return False
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT password_hash, active FROM accounts WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not row[1]:
        return False
    return _check_hash(password, row[0])


def list_users(db_path: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """All accounts (no hashes), ordered by username."""
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    if not path.is_file():
        return []
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT username, created_at, active FROM accounts ORDER BY username"
        ).fetchall()
    return [
        {"username": r[0], "created_at": r[1], "active": bool(r[2])} for r in rows
    ]


def remove_user(username: str, db_path: Optional[Union[str, Path]] = None) -> bool:
    """Delete an account; ``False`` when it did not exist."""
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
        return cur.rowcount > 0


def set_password(
    username: str, password: str, db_path: Optional[Union[str, Path]] = None
) -> bool:
    """Replace an account's password; ``False`` when the user is unknown."""
    if not password:
        raise ValueError("password must be non-empty")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE username = ?",
            (hash_password(password), username),
        )
        return cur.rowcount > 0


def deactivate(username: str, db_path: Optional[Union[str, Path]] = None) -> bool:
    """Disable an account (kept in the DB, login rejected); ``False`` if unknown."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE accounts SET active = 0 WHERE username = ?", (username,)
        )
        return cur.rowcount > 0


def import_from_env(
    users_str: str, db_path: Optional[Union[str, Path]] = None
) -> int:
    """Import a ``TRAFFIC_ANALYZER_USERS`` string (``user:pass,user:pass``).

    Existing usernames are left untouched (no password overwrite); malformed
    entries are skipped. Returns the number of newly added accounts.
    """
    added = 0
    for item in (users_str or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, password = item.split(":", 1)
        name = name.strip()
        # 与 auth._parse_users 同一口径:'|' 是 cookie 负载分隔符,拒绝。
        if not name or not password or "|" in name:
            continue
        if add_user(name, password, db_path):
            added += 1
    return added
