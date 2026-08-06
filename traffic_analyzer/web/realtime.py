"""In-process pub/sub bus and the ``GET /api/events`` SSE endpoint.

``EventBus`` fans events out to one ``asyncio.Queue`` per subscriber;
``GET /api/events`` streams them as ``text/event-stream`` (SSE), one
``event:``/``data:`` pair per event, with ``: ping`` heartbeat comment
lines (~15 s) so proxies do not drop idle connections. Event types:

- ``job.progress``      — job progress snapshot (no log_tail; 进度可重拉)
- ``job.done``          — job reached a terminal state (done/failed)
- ``dashboard.changed`` — dashboard data changed (review PUT 等)
- ``presence``          — 在线名册变化(心跳 beat 后广播最新 roster)

[文件说明]
作用:进程内事件总线 + SSE 端点。EventBus 为每个订阅者维护一个带上限的
asyncio.Queue,publish 溢出时丢弃最旧事件(进度/看板均可重拉,不允许内存
膨胀);publish 是同步且线程安全的:jobs worker 是子进程看守线程、FastAPI
同步端点跑在线程池,二者都经 loop.call_soon_threadsafe 把投递调度回事件
循环;loop 在 app lifespan 里 bind_loop() 绑定,未绑定(如脱离 app 的单测)
时 publish 静默丢弃。单 worker 约束:总线在内存中、不跨进程,与串行任务
队列同属单进程假设;多 worker(多进程)部署是未来前置问题,届时需换
外部 broker。认证:/api/events 走 /api/* 统一 middleware(未认证 401)。
上游:web/app.py(create_app 装配并挂载路由,lifespan 绑定 loop);
web/jobs(queue 在进度更新/任务终态时 publish);web/dashboard.py(review
PUT 成功后 publish);web/presence.py(beat 后 publish)。
下游:frontend/dist 前端(EventSource 订阅 /api/events)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# SSE 心跳间隔(注释行 ": ping",防代理断连)与订阅者队列上限。
_HEARTBEAT_SEC = 15.0
_SUBSCRIBER_QUEUE_MAX = 256


class EventBus:
    """Process-wide fan-out bus; one bounded asyncio.Queue per subscriber."""

    def __init__(self, queue_max: int = _SUBSCRIBER_QUEUE_MAX) -> None:
        self._queue_max = queue_max
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: Set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle(app lifespan 绑定/解绑事件循环)
    # ------------------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def unbind_loop(self) -> None:
        with self._lock:
            self._loop = None
            self._subs.clear()

    # ------------------------------------------------------------------
    # Publish(同步、线程安全;未绑定 loop 时静默丢弃)
    # ------------------------------------------------------------------
    def publish(self, type_: str, payload: Any) -> None:
        with self._lock:
            loop = self._loop
            subs = list(self._subs)
        if loop is None or not subs:
            return
        event = {"type": type_, "data": payload}
        for q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:  # loop 已关闭(服务退出中)
                pass

    def _offer(self, q: asyncio.Queue, event: Dict[str, Any]) -> None:
        """Loop 线程内执行:队列满则丢弃最旧再入队(进度可重拉)。"""
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态防御
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - 竞态防御
            pass

    # ------------------------------------------------------------------
    # Subscribe(SSE 端点在 loop 线程内调用)
    # ------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_max)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)


def publish_from_app(app: Any, type_: str, payload: Any) -> None:
    """Thin helper for route handlers: publish via ``app.state.realtime``."""
    bus = getattr(app.state, "realtime", None)
    if bus is not None:
        bus.publish(type_, payload)


def _format_sse(event: Dict[str, Any]) -> str:
    data = json.dumps(event["data"], ensure_ascii=False)
    return f"event: {event['type']}\ndata: {data}\n\n"


@router.get("/api/events")
async def stream_events(request: Request) -> StreamingResponse:
    """SSE 事件流:订阅即收全量事件,断连自动退订。"""
    bus: EventBus = request.app.state.realtime
    q = bus.subscribe()

    async def _gen() -> Any:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _format_sse(event)
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
