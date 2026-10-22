"""SFT Forge web API(POST /api/sftforge/jobs + SSE events + result)测试。

fake engine 经包命名空间 monkeypatch(traffic_analyzer.web.sftforge.build_engine)
注入,验证三个端点与 SSE 事件序列(round_started/round_completed 载荷含
RoundTrace、video_completed、job_completed),不触碰真实 VLM provider。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.sftforge.models import RoundTrace, VideoForgeResult
from traffic_analyzer.web import sftforge
from traffic_analyzer.web.app import create_app


_NAMES = {1: "违法停车", 2: "应急车道占用", 3: "实线变道"}


def _make_videos(tmp_path: Path) -> Dict[str, Path]:
    videos = {}
    for name in ("match.mp4", "skip.mp4", "quar.mp4"):
        path = tmp_path / name
        path.write_bytes(b"\x00" * 16)
        videos[name] = path
    return videos


class _FakeEngine:
    """按文件名分派结果;用 on_event 回放两轮纠错(漏检补齐)事件序列。"""

    def __init__(self, on_event: Any, gate: Optional[threading.Event] = None) -> None:
        self._on_event = on_event
        self._names = dict(_NAMES)
        self._gate = gate

    def _emit_round(
        self, video: str, round_no: int, kind: str, event_ids: List[int], patched: bool
    ) -> RoundTrace:
        trace = RoundTrace(
            round_no=round_no,
            kind=kind,
            event_ids=list(event_ids),
            prompt=f"prompt-{kind}-{round_no}",
            response=f"response-{kind}-{round_no}",
            patched=patched,
        )
        self._on_event(
            "round_started",
            {"video": video, "round_no": round_no, "kind": kind, "event_ids": event_ids},
        )
        self._on_event(
            "round_completed", {"video": video, "trace": trace.model_dump()}
        )
        return trace

    def run(self, video_path: Any) -> VideoForgeResult:
        if self._gate is not None:
            assert self._gate.wait(timeout=15)
        path = Path(video_path)
        video = str(path)
        if path.stem == "match":
            # 第一轮漏检 class2,第二轮 correct_missing 补齐后 match。
            t0 = self._emit_round(video, 0, "forward", [], patched=False)
            t1 = self._emit_round(video, 1, "correct_missing", [2], patched=True)
            return VideoForgeResult(
                video=video,
                gt=[1, 2],
                final_output="<think>\n</think>\n<answer>class1 class2</answer>",
                rounds=[t0, t1],
                match=True,
            )
        if path.stem == "skip":
            return VideoForgeResult(
                video=video, skipped=True, reason="正常视频(09),跳过"
            )
        if path.stem == "boom":
            raise RuntimeError("caller exploded")
        # quar.mp4:达到 max_rounds 仍不一致 → quarantine。
        t0 = self._emit_round(video, 0, "forward", [], patched=False)
        return VideoForgeResult(
            video=video,
            gt=[1],
            final_output="<think>\n</think>\n<answer>class1 class3</answer>",
            rounds=[t0],
            match=False,
            reason="final_mismatch: missing=[] extra=[3]",
        )


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    recorded: Dict[str, Any],
    gate: Optional[threading.Event] = None,
) -> None:
    def fake_build_engine(max_rounds: int, on_event: Any) -> _FakeEngine:
        recorded["max_rounds"] = max_rounds
        return _FakeEngine(on_event, gate=gate)

    monkeypatch.setattr(sftforge, "build_engine", fake_build_engine)


def _wait_result(client: TestClient, job_id: int, timeout: float = 15.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/sftforge/jobs/{job_id}/result").json()
        if payload["status"] in ("done", "failed"):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"sftforge job {job_id} did not finish in {timeout}s")


def _parse_sse(body: str) -> List[Dict[str, Any]]:
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        type_, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                type_ = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        assert type_ is not None and data is not None, block
        events.append({"type": type_, "data": json.loads(data)})
    return events


class TestCreateJob:
    def test_requires_dir_or_videos(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post("/api/sftforge/jobs", json={})
        assert resp.status_code == 400

    def test_dir_must_exist(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post(
            "/api/sftforge/jobs", json={"dir": str(tmp_path / "nope")}
        )
        assert resp.status_code == 400

    def test_video_must_exist(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post(
            "/api/sftforge/jobs", json={"videos": [str(tmp_path / "nope.mp4")]}
        )
        assert resp.status_code == 404

    def test_rejects_non_video(self, tmp_path: Path) -> None:
        note = tmp_path / "notes.txt"
        note.write_text("x", encoding="utf-8")
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.post("/api/sftforge/jobs", json={"videos": [str(note)]})
        assert resp.status_code == 400


class TestJobLifecycle:
    def test_result_aggregates_rows_stats_quarantine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        videos = _make_videos(tmp_path)
        recorded: Dict[str, Any] = {}
        _install_fake_engine(monkeypatch, recorded)
        client = TestClient(create_app(workspace=str(tmp_path)))

        resp = client.post(
            "/api/sftforge/jobs",
            json={"dir": str(tmp_path), "max_rounds": 3},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        assert recorded["max_rounds"] == 3

        payload = _wait_result(client, job_id)
        assert payload["status"] == "done"
        assert payload["stats"] == {"match": 1, "quarantine": 1, "skipped": 1}

        # match 视频产出训练 JSONL 行(契约:index/video/output/gt_classes/gt_class_names)。
        assert len(payload["jsonl"]) == 1
        row = payload["jsonl"][0]
        assert row["index"] == 0
        assert row["video"].endswith("match.mp4")
        assert row["gt_classes"] == [1, 2]
        assert row["gt_class_names"] == {"1": "违法停车", "2": "应急车道占用"}

        assert len(payload["quarantine"]) == 1
        assert payload["quarantine"][0]["video"].endswith("quar.mp4")
        assert "final_mismatch" in payload["quarantine"][0]["reason"]

        summaries = {Path(v["video"]).stem: v for v in payload["videos"]}
        assert summaries["match"]["match"] is True
        assert summaries["match"]["rounds"] == 2
        assert summaries["skip"]["skipped"] is True
        assert summaries["quar"]["match"] is False

    def test_engine_failure_marks_job_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "boom.mp4").write_bytes(b"\x00" * 16)
        _install_fake_engine(monkeypatch, {})
        client = TestClient(create_app(workspace=str(tmp_path)))
        job_id = client.post(
            "/api/sftforge/jobs", json={"videos": [str(tmp_path / "boom.mp4")]}
        ).json()["job_id"]

        payload = _wait_result(client, job_id)
        assert payload["status"] == "failed"
        assert "RuntimeError" in payload["error"]

    def test_unknown_job_404(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        assert client.get("/api/sftforge/jobs/999/result").status_code == 404
        assert client.get("/api/sftforge/jobs/999/events").status_code == 404


class TestEventsStream:
    def test_sse_replays_full_correction_timeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任务完成后订阅:回放全部事件,终态 job_completed 后关流。"""
        videos = _make_videos(tmp_path)
        _install_fake_engine(monkeypatch, {})
        client = TestClient(create_app(workspace=str(tmp_path)))
        job_id = client.post(
            "/api/sftforge/jobs", json={"dir": str(tmp_path)}
        ).json()["job_id"]
        assert _wait_result(client, job_id)["status"] == "done"

        resp = client.get(f"/api/sftforge/jobs/{job_id}/events")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(resp.text)

        # 逐视频逐轮:match.mp4 的 forward → correct_missing 完整纠错过程可见。
        match_events = [
            e for e in events if e["data"].get("video", "").endswith("match.mp4")
        ]
        assert [e["type"] for e in match_events] == [
            "round_started",
            "round_completed",
            "round_started",
            "round_completed",
            "video_completed",
        ]
        started = match_events[0]["data"]
        assert started["round_no"] == 0
        assert started["kind"] == "forward"
        completed = match_events[3]["data"]["trace"]
        assert completed["round_no"] == 1
        assert completed["kind"] == "correct_missing"
        assert completed["event_ids"] == [2]
        assert completed["patched"] is True
        assert completed["prompt"]
        assert completed["response"]

        # 终态事件收尾,统计与 /result 一致。
        assert events[-1]["type"] == "job_completed"
        assert events[-1]["data"]["stats"] == {
            "match": 1,
            "quarantine": 1,
            "skipped": 1,
        }
        video_done = [e for e in events if e["type"] == "video_completed"]
        assert len(video_done) == 3
        assert not any(e["type"] == "job_failed" for e in events)

    def test_sse_live_delivery_while_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任务运行中订阅:引擎事件经 worker→loop 实时推送。

        TestClient 不能消费无限流(会等整个响应体,见 test_realtime.py),
        故手工构造 ASGI scope 直驱 app,与生产路径(uvicorn)一致。
        """
        import asyncio

        (tmp_path / "match.mp4").write_bytes(b"\x00" * 16)
        gate = threading.Event()
        _install_fake_engine(monkeypatch, {}, gate=gate)
        client = TestClient(create_app(workspace=str(tmp_path)))
        job_id = client.post(
            "/api/sftforge/jobs", json={"videos": [str(tmp_path / "match.mp4")]}
        ).json()["job_id"]

        async def main() -> str:
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": f"/api/sftforge/jobs/{job_id}/events",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": client.app,
            }
            chunks: List[bytes] = []
            sent_request = False

            async def receive() -> Dict[str, Any]:
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.sleep(3600)
                return {"type": "http.disconnect"}

            async def send(message: Dict[str, Any]) -> None:
                if message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))

            task = asyncio.create_task(client.app(scope, receive, send))
            await asyncio.sleep(0.3)  # 等端点完成订阅
            gate.set()  # 放行引擎;订阅已建立,事件经 call_soon_threadsafe 实时推送
            await asyncio.wait_for(task, timeout=15)  # 终态事件后流自然结束
            return b"".join(chunks).decode("utf-8")

        body = asyncio.run(main())
        events = _parse_sse(body)
        assert [e["type"] for e in events] == [
            "round_started",
            "round_completed",
            "round_started",
            "round_completed",
            "video_completed",
            "job_completed",
        ]
