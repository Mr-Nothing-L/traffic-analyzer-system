"""Presence heartbeats: who is viewing/editing what (30 s TTL roster).

``POST /api/presence`` records a heartbeat for ``request.state.user``
(``"local"`` when auth is off) with the currently viewed/edited stems;
``GET /api/presence`` returns the live roster, pruning entries older than
the TTL. Storage is per-app (``app.state.presence``), in-memory only.

[文件说明]
作用:在线状态接口。POST /api/presence {viewing, editing} 按 request.state.user
(认证关闭时为 'local')记心跳;GET /api/presence 返回 [{user, viewing, editing, ts}],
30s TTL 惰性剔除。存储为 app.state.presence(内存,线程锁保护)。
上游:web/app.py(create_app 挂载路由并初始化 store);web/auth.py(request.state.user)。
下游:web/static 前端(协作在线状态展示)。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()

PRESENCE_TTL_SEC = 30.0


class PresenceStore:
    """Thread-safe in-memory roster keyed by user; TTL pruned lazily on read."""

    def __init__(self, ttl: float = PRESENCE_TTL_SEC) -> None:
        self.ttl = ttl
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def beat(self, user: str, viewing: Optional[str], editing: Optional[str]) -> None:
        with self._lock:
            self._entries[user] = {
                "user": user,
                "viewing": viewing,
                "editing": editing,
                "ts": time.time(),
            }

    def roster(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._entries = {
                user: entry
                for user, entry in self._entries.items()
                if now - entry["ts"] < self.ttl
            }
            return sorted(self._entries.values(), key=lambda e: e["user"])


class PresenceBeat(BaseModel):
    viewing: Optional[str] = None
    editing: Optional[str] = None


@router.post("/api/presence")
def post_presence(body: PresenceBeat, request: Request) -> Dict[str, Any]:
    store: PresenceStore = request.app.state.presence
    user = getattr(request.state, "user", "local")
    store.beat(user, body.viewing, body.editing)
    return {"ok": True}


@router.get("/api/presence")
def get_presence(request: Request) -> List[Dict[str, Any]]:
    store: PresenceStore = request.app.state.presence
    return store.roster()
