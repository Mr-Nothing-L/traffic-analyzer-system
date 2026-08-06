"""Realtime event bus (SSE /api/events) tests."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web import auth, realtime
from traffic_analyzer.web.app import create_app
from traffic_analyzer.web.realtime import EventBus

from .conftest import _FAKE_INFER_SCRIPT, _make_results, _make_workspace, _wait_for_job


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestEventBus:
    def test_publish_from_worker_thread_reaches_subscriber(self) -> None:
        """跨线程 publish(jobs worker 语义)经 call_soon_threadsafe 投递。"""
        bus = EventBus()

        async def main() -> Dict[str, Any]:
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()
            t = threading.Thread(
                target=bus.publish, args=("job.done", {"id": 1, "status": "done"})
            )
            t.start()
            event = await asyncio.wait_for(q.get(), timeout=5)
            t.join()
            bus.unbind_loop()
            return event

        assert _run(main()) == {"type": "job.done", "data": {"id": 1, "status": "done"}}

    def test_queue_overflow_drops_oldest(self) -> None:
        """订阅者队列满:丢弃最旧事件(进度可重拉),不允许内存膨胀。"""
        bus = EventBus(queue_max=3)

        async def main() -> List[Dict[str, Any]]:
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()
            for i in range(5):
                bus.publish("job.progress", {"i": i})
            await asyncio.sleep(0.2)  # 让 call_soon_threadsafe 回调跑完
            got = []
            while not q.empty():
                got.append(q.get_nowait())
            bus.unbind_loop()
            return got

        got = _run(main())
        assert [e["data"]["i"] for e in got] == [2, 3, 4]

    def test_publish_without_bound_loop_is_noop(self) -> None:
        bus = EventBus()
        bus.publish("job.done", {"id": 1})  # 未绑定 loop:静默丢弃,不抛异常

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()

        async def main() -> bool:
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()
            bus.unsubscribe(q)
            bus.publish("presence", {"roster": []})
            await asyncio.sleep(0.2)
            empty = q.empty()
            bus.unbind_loop()
            return empty

        assert _run(main()) is True


class TestEventsEndpoint:
    @staticmethod
    def _read_sse(app: Any, after_subscribe: Any = None, settle: float = 0.4) -> str:
        """在 ASGI 层直接驱动 GET /api/events,返回收集到的响应体文本。

        TestClient 不能消费无限流(starlette 1.3 的 TestClient 走 httpx 传输,
        会等整个响应体),因此手工构造 scope 收 chunks;生产路径(uvicorn +
        curl)已验证流式输出正常。
        """

        async def main() -> str:
            bus = app.state.realtime
            bus.bind_loop(asyncio.get_running_loop())
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/events",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
            }
            chunks: List[bytes] = []
            sent_request = False

            async def receive() -> Dict[str, Any]:
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.sleep(3600)  # 不主动 disconnect;由 cancel 收尾
                return {"type": "http.disconnect"}

            async def send(message: Dict[str, Any]) -> None:
                if message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))

            task = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.sleep(settle)  # 等端点完成订阅
                if after_subscribe is not None:
                    after_subscribe()
                await asyncio.sleep(settle)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return b"".join(chunks).decode("utf-8")

        return asyncio.run(main())

    def test_sse_streams_published_events(self, tmp_path: Path) -> None:
        app = create_app(workspace=str(_make_workspace(tmp_path)))
        body = self._read_sse(
            app,
            after_subscribe=lambda: app.state.realtime.publish("job.done", {"id": 7}),
        )
        assert "event: job.done\n" in body
        assert 'data: {"id": 7}\n' in body

    def test_sse_heartbeat_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(realtime, "_HEARTBEAT_SEC", 0.1)
        app = create_app(workspace=str(_make_workspace(tmp_path)))
        body = self._read_sse(app)
        assert ": ping\n" in body

    def test_sse_requires_auth_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """认证开启时 /api/events 与其他 /api/* 一致:未认证 401。"""
        monkeypatch.setenv(auth.USERS_ENV_VAR, "zhangsan:pass1")
        monkeypatch.setenv(auth.SECRET_ENV_VAR, "test-secret")
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        assert client.get("/api/events").status_code == 401


class TestPublishCallSites:
    def test_job_progress_and_done_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", _FAKE_INFER_SCRIPT],
        )
        app = create_app(workspace=str(workspace))
        events: List[Tuple[str, Any]] = []
        monkeypatch.setattr(
            app.state.realtime,
            "publish",
            lambda type_, payload: events.append((type_, payload)),
        )
        client = TestClient(app)
        job_id = client.post("/api/infer", json={"stems": ["v1"]}).json()["job_ids"][0]
        job = _wait_for_job(client, job_id)
        assert job["status"] == "done"

        types = [t for t, _ in events]
        assert "job.progress" in types
        assert "job.done" in types
        done_payload = [p for t, p in events if t == "job.done"][-1]
        assert done_payload["id"] == job_id
        assert done_payload["status"] == "done"
        assert done_payload["progress"]["fraction"] == 1.0
        # 高频进度事件不带 log_tail(需要时走 /api/jobs 重拉)。
        assert "log_tail" not in done_payload
        for _, payload in events:
            assert "log_tail" not in payload

    def test_review_put_publishes_dashboard_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        app = create_app(workspace=str(workspace))
        events: List[Tuple[str, Any]] = []
        monkeypatch.setattr(
            app.state.realtime,
            "publish",
            lambda type_, payload: events.append((type_, payload)),
        )
        client = TestClient(app)
        resp = client.put(
            "/api/dashboard/review", json={"stem": "v1", "status": "confirmed"}
        )
        assert resp.status_code == 200
        assert (
            "dashboard.changed", {"stem": "v1", "status": "confirmed"}
        ) in events

    def test_presence_beat_publishes_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = create_app(workspace=str(_make_workspace(tmp_path)))
        events: List[Tuple[str, Any]] = []
        monkeypatch.setattr(
            app.state.realtime,
            "publish",
            lambda type_, payload: events.append((type_, payload)),
        )
        client = TestClient(app)
        resp = client.post("/api/presence", json={"viewing": "v1", "editing": None})
        assert resp.status_code == 200
        presence_events = [p for t, p in events if t == "presence"]
        assert presence_events
        roster = presence_events[-1]["roster"]
        assert roster[0]["viewing"] == "v1"
