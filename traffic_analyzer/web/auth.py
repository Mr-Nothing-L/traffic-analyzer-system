"""Optional session auth for the web UI (SQLite user store + env bootstrap).

Auth is fully OFF unless accounts exist — either in the SQLite user store
(``traffic_analyzer/config/users.db``, see ``web/user_store.py``) or in
``TRAFFIC_ANALYZER_USERS`` (``zhangsan:pass1,lisi:pass2``); when off every
request gets ``request.state.user = "local"`` and behavior is identical to
before. On first startup with an empty store, ``TRAFFIC_ANALYZER_USERS`` is
imported into the store and the line in ``traffic_analyzer/config/.env`` is
commented out (``# migrated to users.db: ...``); afterwards the store is the
source of truth.

When on, ``POST /api/auth/login`` sets an HMAC-signed ``ta_session`` cookie
(``user|expiry|login_ip|login_ts|sig``, 30-day validity, httpOnly,
sameSite=lax). The signing key comes from ``TRAFFIC_ANALYZER_SECRET``; when
absent a random one is generated and appended to
``traffic_analyzer/config/.env`` so sessions survive restarts. The middleware
re-verifies signature, expiry and that the request IP still equals
``login_ip``; unauthenticated page requests get 302 → ``/login``, API
requests 401.

[文件说明]
作用:可选的会话认证(账号在 config/users.db,未配置则完全关闭,
request.state.user='local';库为空且 config/.env 有 TRAFFIC_ANALYZER_USERS 时首次
启动自动导入 users.db 并把该行注释为 '# migrated to users.db: ...')。
POST /api/auth/login 校验账号后 Set-Cookie ta_session(HMAC 签名
user|expiry|login_ip|login_ts,密钥 TRAFFIC_ANALYZER_SECRET,缺省自动生成并追加写回
config/.env,30 天有效,httpOnly,sameSite=lax);middleware 校验签名+过期+
请求 IP==login_ip,未认证访问页面 302 /login、API 401;POST /api/auth/logout 清
cookie;GET /api/auth/me 返回 {username, login_ts, login_ip}。
上游:web/app.py(create_app 挂载路由并在 no-cache 之后注册 middleware);
web/presence.py、web/dashboard.py、web/evidence_api.py(读 request.state.user)。
下游:traffic_analyzer/config/.env(密钥缺省时追加写回;USERS 行迁移后注释);
web/user_store.py(账号存取与密码校验)。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import user_store

logger = logging.getLogger(__name__)

router = APIRouter()

USERS_ENV_VAR = "TRAFFIC_ANALYZER_USERS"
SECRET_ENV_VAR = "TRAFFIC_ANALYZER_SECRET"
COOKIE_NAME = "ta_session"
SESSION_TTL_SEC = 30 * 86400

# traffic_analyzer/config/.env(traffic_analyzer/web/auth.py → parents[1])。
# 测试 monkeypatch 此常量。
_ENV_PATH = Path(__file__).resolve().parents[1] / "config" / ".env"


class AuthConfig:
    """Parsed auth settings; ``enabled=False`` means auth is fully off."""

    def __init__(
        self, users: Dict[str, str], secret: Optional[str], db_has_users: bool = False
    ) -> None:
        self.users = users
        self.secret = secret
        self.db_has_users = db_has_users
        self.enabled = (bool(users) or db_has_users) and bool(secret)


def _parse_users(raw: str) -> Dict[str, str]:
    """``zhangsan:pass1,lisi:pass2`` → dict; malformed/ambiguous entries skipped."""
    users: Dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, password = item.split(":", 1)
        name = name.strip()
        # '|' 是 cookie 负载分隔符,用户名含它会破坏解析,直接拒绝该条目。
        if name and password and "|" not in name:
            users[name] = password
    return users


def _load_or_create_secret() -> str:
    """Secret from env, then config/.env, else generate + append to config/.env."""
    secret = os.environ.get(SECRET_ENV_VAR)
    if secret:
        return secret
    value = _env_value(SECRET_ENV_VAR)
    if value:
        return value
    secret = secrets.token_hex(32)
    try:
        existing = ""
        try:
            existing = _ENV_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with open(_ENV_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{separator}{SECRET_ENV_VAR}={secret}\n")
        logger.info("Generated %s and appended it to %s", SECRET_ENV_VAR, _ENV_PATH)
    except OSError as exc:
        # 写不回 .env 不阻断启动;代价仅是重启后旧会话失效。
        logger.warning("Cannot persist %s to %s: %s", SECRET_ENV_VAR, _ENV_PATH, exc)
    return secret


def _env_value(key: str) -> Optional[str]:
    """Read one ``KEY=value`` entry from config/.env ('' if missing)."""
    try:
        existing = _ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in existing.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    return None


def _comment_out_users_env_line() -> None:
    """Comment out the ``TRAFFIC_ANALYZER_USERS`` line in config/.env after migration."""
    try:
        text = _ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{USERS_ENV_VAR}=") and not stripped.startswith("#"):
            lines[i] = f"# migrated to users.db: {line}"
            changed = True
    if not changed:
        return
    try:
        _ENV_PATH.write_text("".join(lines), encoding="utf-8")
        logger.info("Commented out %s in %s (migrated to users.db)", USERS_ENV_VAR, _ENV_PATH)
    except OSError as exc:
        # 注释失败不阻断启动;下次启动若库仍非空也不会重复导入。
        logger.warning("Cannot comment out %s in %s: %s", USERS_ENV_VAR, _ENV_PATH, exc)


def configure() -> AuthConfig:
    """Build the AuthConfig (env vars first, then config/.env, then users.db).

    First-startup migration: when the store is empty and
    ``TRAFFIC_ANALYZER_USERS`` is set, import it into users.db and comment the
    line out in config/.env; from then on the store alone enables auth.
    """
    raw = os.environ.get(USERS_ENV_VAR, "") or (_env_value(USERS_ENV_VAR) or "")
    users = _parse_users(raw)
    db_has_users = bool(user_store.list_users())
    if users and not db_has_users:
        imported = user_store.import_from_env(raw)
        if imported:
            _comment_out_users_env_line()
            logger.info("Migrated %d account(s) from %s to users.db", imported, USERS_ENV_VAR)
        db_has_users = bool(user_store.list_users())
    has_users = bool(users) or db_has_users
    secret = _load_or_create_secret() if has_users else None
    return AuthConfig(users, secret, db_has_users)


def _request_ip(request: Request) -> str:
    """Client IP used for the login_ip binding (module-level for monkeypatching)."""
    return request.client.host if request.client else "unknown"


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_cookie(config: AuthConfig, username: str, login_ip: str) -> Dict[str, Any]:
    login_ts = int(time.time())
    expiry = login_ts + SESSION_TTL_SEC
    payload = f"{username}|{expiry}|{login_ip}|{login_ts}"
    value = f"{payload}|{_sign(config.secret or '', payload)}"
    return {"value": value, "login_ts": login_ts, "login_ip": login_ip}


def _verify_cookie(
    config: AuthConfig, value: Optional[str], request_ip: str
) -> Optional[Dict[str, Any]]:
    """Valid cookie → session dict; any mismatch (sig/expiry/IP) → None."""
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != 5:
        return None
    username, expiry_s, login_ip, login_ts_s, sig = parts
    payload = "|".join(parts[:4])
    expected = _sign(config.secret or "", payload)
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        expiry = int(expiry_s)
        login_ts = int(login_ts_s)
    except ValueError:
        return None
    if expiry < time.time():
        return None
    if login_ip != request_ip:
        return None
    return {"username": username, "login_ts": login_ts, "login_ip": login_ip}


# 登录页与其所需静态资源不鉴权;其余页面 302、API 401。
_EXACT_EXEMPT = frozenset(
    {"/login", "/login.html", "/style.css", "/usability.css", "/favicon.ico"}
)
_PREFIX_EXEMPT = ("/js/", "/fonts/")


def _is_exempt(path: str, method: str) -> bool:
    if path == "/api/auth/login" and method == "POST":
        return True
    return path in _EXACT_EXEMPT or path.startswith(_PREFIX_EXEMPT)


def install(app: Any) -> None:
    """Register the auth middleware. Call AFTER the no-cache middleware so it
    runs outside it (auth decision first, cache headers only for passed requests)."""

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        config: AuthConfig = request.app.state.auth
        if not config.enabled:
            request.state.user = "local"
            return await call_next(request)
        path = request.url.path
        if _is_exempt(path, request.method):
            return await call_next(request)
        session = _verify_cookie(
            config, request.cookies.get(COOKIE_NAME), _request_ip(request)
        )
        if session is not None:
            request.state.user = session["username"]
            request.state.session = session
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "username": session["username"],
        "login_ts": session["login_ts"],
        "login_ip": session["login_ip"],
    }


@router.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
    config: AuthConfig = request.app.state.auth
    if not config.enabled:
        raise HTTPException(status_code=404, detail="auth is not enabled")
    # 先查库(users.db 是迁移后的权威来源,deactivate 以库为准);
    # 库中不存在该用户时才回退到 env 配置的明文账号(迁移前/未迁移场景)。
    if user_store.get_user(body.username) is not None:
        verified = user_store.verify_password(body.username, body.password)
    else:
        expected = config.users.get(body.username)
        verified = expected is not None and hmac.compare_digest(
            expected.encode("utf-8"), body.password.encode("utf-8")
        )
    if not verified:
        raise HTTPException(status_code=401, detail="invalid credentials")
    login_ip = _request_ip(request)
    cookie = _make_cookie(config, body.username, login_ip)
    response.set_cookie(
        COOKIE_NAME,
        cookie["value"],
        max_age=SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
    )
    session = {
        "username": body.username,
        "login_ts": cookie["login_ts"],
        "login_ip": login_ip,
    }
    return _session_response(session)


@router.post("/api/auth/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/api/auth/me")
def me(request: Request) -> Dict[str, Any]:
    session = getattr(request.state, "session", None)
    if session is not None:
        return _session_response(session)
    # 到达这里说明认证关闭(middleware 已放行),按本地单用户口径回答。
    return {"username": "local", "login_ts": None, "login_ip": None}
