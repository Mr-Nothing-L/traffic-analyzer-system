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
        # 流式增量事件(round_delta):思考与正文各一帧,验证 SSE 传输与回放。
        self._on_event(
            "round_delta",
            {"video": video, "round_no": round_no, "kind": kind,
             "field": "reasoning", "text": f"reasoning-{kind}-{round_no}"},
        )
        self._on_event(
            "round_delta",
            {"video": video, "round_no": round_no, "kind": kind,
             "field": "response", "text": f"response-{kind}-{round_no}"},
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
                r0_match=False,
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
            r0_match=False,
            reason="final_mismatch: missing=[] extra=[3]",
        )


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    recorded: Dict[str, Any],
    gate: Optional[threading.Event] = None,
) -> None:
    def fake_build_engine(
        max_rounds: int, on_event: Any, thinking: str = "medium", draft: str = "pipeline"
    ) -> _FakeEngine:
        recorded["max_rounds"] = max_rounds
        recorded["thinking"] = thinking
        recorded["draft"] = draft
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
            json={"dir": str(tmp_path), "max_rounds": 3, "thinking": "high", "draft": "prompt"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        assert recorded["max_rounds"] == 3
        assert recorded["thinking"] == "high"
        assert recorded["draft"] == "prompt"

        payload = _wait_result(client, job_id)
        assert payload["status"] == "done"
        # corrected:match 但 R0 不一致(纠错后一致)的视频数
        assert payload["stats"] == {"match": 1, "quarantine": 1, "skipped": 1, "corrected": 1}

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
        assert summaries["match"]["r0_match"] is False
        assert summaries["match"]["rounds"] == 2
        assert summaries["skip"]["skipped"] is True
        assert summaries["skip"]["r0_match"] is None
        assert summaries["quar"]["match"] is False
        assert summaries["quar"]["r0_match"] is False

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

        # 逐视频逐轮:match.mp4 的 forward → correct_missing 完整纠错过程可见,
        # 每轮含 round_delta 流式增量(reasoning/response 各一帧)。
        match_events = [
            e for e in events if e["data"].get("video", "").endswith("match.mp4")
        ]
        assert [e["type"] for e in match_events] == [
            "round_started",
            "round_delta",
            "round_delta",
            "round_completed",
            "round_started",
            "round_delta",
            "round_delta",
            "round_completed",
            "video_completed",
        ]
        started = match_events[0]["data"]
        assert started["round_no"] == 0
        assert started["kind"] == "forward"
        delta = match_events[1]["data"]
        assert delta["field"] == "reasoning"
        assert delta["text"] == "reasoning-forward-0"
        completed = match_events[7]["data"]["trace"]
        assert completed["round_no"] == 1
        assert completed["kind"] == "correct_missing"
        assert completed["event_ids"] == [2]
        assert completed["patched"] is True
        assert completed["prompt"]
        assert completed["response"]
        # video_completed 载荷带 final_output(前端最终结论视图数据源)。
        assert match_events[8]["data"]["final_output"].endswith("</answer>")
        assert match_events[8]["data"]["r0_match"] is False

        # 终态事件收尾,统计与 /result 一致。
        assert events[-1]["type"] == "job_completed"
        assert events[-1]["data"]["stats"] == {
            "match": 1,
            "quarantine": 1,
            "skipped": 1,
            "corrected": 1,
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
            "round_delta",
            "round_delta",
            "round_completed",
            "round_started",
            "round_delta",
            "round_delta",
            "round_completed",
            "video_completed",
            "job_completed",
        ]


class TestResultStore:
    """工作区持久化(GET/DELETE /api/sftforge/store):保存 → 读取 → 删除。

    任务终态(done/failed)落盘 <workspace>/.agent/sftforge/latest_result.json,
    新建 app(模拟服务重启)仍能读到;新任务终态覆盖旧缓存。
    """

    def _wait_store(self, client: TestClient, timeout: float = 15.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = client.get("/api/sftforge/store")
            if resp.status_code == 200:
                return resp.json()
            time.sleep(0.02)
        raise AssertionError("stored sftforge result did not appear")

    def test_persist_read_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_videos(tmp_path)
        _install_fake_engine(monkeypatch, {})
        client = TestClient(create_app(workspace=str(tmp_path)))

        # 无缓存:GET 404
        assert client.get("/api/sftforge/store").status_code == 404

        job_id = client.post(
            "/api/sftforge/jobs", json={"dir": str(tmp_path)}
        ).json()["job_id"]
        payload = _wait_result(client, job_id)
        assert payload["status"] == "done"

        stored = self._wait_store(client)
        stored_path = tmp_path / ".agent" / "sftforge" / "latest_result.json"
        assert stored_path.is_file()
        assert stored["status"] == "done"
        assert stored["job_id"] == job_id
        assert stored["saved_at"]
        assert stored["stats"] == {"match": 1, "quarantine": 1, "skipped": 1, "corrected": 1}
        assert len(stored["jsonl"]) == 1
        assert len(stored["quarantine"]) == 1
        # videos 摘要与 /result 一致(含 final_output 与 r0_match)
        assert stored["videos"] == payload["videos"]
        match_summary = next(v for v in stored["videos"] if v["video"].endswith("match.mp4"))
        assert match_summary["r0_match"] is False
        # 轮次精简信息:match.mp4 两轮,第二轮 correct_missing patched=True
        match_key = next(k for k in stored["rounds"] if k.endswith("match.mp4"))
        assert stored["rounds"][match_key] == [
            {"round_no": 0, "kind": "forward", "event_ids": [], "patched": False},
            {"round_no": 1, "kind": "correct_missing", "event_ids": [2], "patched": True},
        ]

        # 新建 app(模拟服务重启):内存任务表为空,缓存仍可读
        client2 = TestClient(create_app(workspace=str(tmp_path)))
        assert client2.get("/api/sftforge/store").json()["job_id"] == job_id

        # DELETE 删缓存,再 GET 404;重复 DELETE 幂等(deleted=false)
        assert client2.delete("/api/sftforge/store").json() == {"deleted": True}
        assert not stored_path.exists()
        assert client2.get("/api/sftforge/store").status_code == 404
        assert client2.delete("/api/sftforge/store").json() == {"deleted": False}

    def test_new_terminal_run_overwrites_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "match.mp4").write_bytes(b"\x00" * 16)
        _install_fake_engine(monkeypatch, {})
        client = TestClient(create_app(workspace=str(tmp_path)))
        first = client.post(
            "/api/sftforge/jobs", json={"videos": [str(tmp_path / "match.mp4")]}
        ).json()["job_id"]
        assert _wait_result(client, first)["status"] == "done"
        assert self._wait_store(client)["status"] == "done"

        # 重新推理(失败终态)覆盖旧缓存
        (tmp_path / "boom.mp4").write_bytes(b"\x00" * 16)
        second = client.post(
            "/api/sftforge/jobs", json={"videos": [str(tmp_path / "boom.mp4")]}
        ).json()["job_id"]
        assert _wait_result(client, second)["status"] == "failed"
        deadline = time.time() + 15
        while True:
            stored = client.get("/api/sftforge/store").json()
            if stored["job_id"] == second:
                break
            assert time.time() < deadline, "store was not overwritten"
            time.sleep(0.02)
        assert stored["status"] == "failed"
        assert "RuntimeError" in stored["error"]


class TestEmittingEnginePatched:
    """_EmittingEngine 事件载荷回归:round_completed 必须在 patched 置位后发出。

    历史 bug:曾在 _call 内提前发 round_completed,纠错轮的 trace 永远带
    patched=False(前端「未产生修改」误显示)。
    """

    def test_correction_round_completed_carries_patched(self) -> None:
        from traffic_analyzer.models.schemas import EventCategory, PromptTemplate
        from traffic_analyzer.sftforge.engine import (
            TEMPLATE_CORRECT_MISSING,
            TEMPLATE_FORWARD,
            TEMPLATE_REVIEW_EXTRA,
        )
        from traffic_analyzer.web.sftforge.service import _EmittingEngine

        names = {1: "违法停车", 2: "应急车道占用", 9: "无异常事件"}
        templates = {
            TEMPLATE_FORWARD: PromptTemplate(
                template_id=TEMPLATE_FORWARD, name="fwd", user_prompt="正向 {{ event_definitions }}"
            ),
            TEMPLATE_CORRECT_MISSING: PromptTemplate(
                template_id=TEMPLATE_CORRECT_MISSING, name="cm",
                user_prompt="补 {{ event_name }}:{{ event_definition }}",
            ),
            TEMPLATE_REVIEW_EXTRA: PromptTemplate(
                template_id=TEMPLATE_REVIEW_EXTRA, name="re",
                user_prompt="复核 {{ event_name }}:{{ event_definition }}|{{ first_round_segment }}",
            ),
        }
        categories = [
            EventCategory(
                event_id=1, event_code="A", name="Illegal Parking",
                name_zh="违法停车", description="", definition="违停判定定义",
            ),
            EventCategory(
                event_id=2, event_code="B", name="Emergency Lane Occupancy",
                name_zh="应急车道占用", description="", definition="占用判定定义",
            ),
        ]
        forward = (
            "<think>\n违法停车：观察：静止。\n\n应急车道占用：未见占用。\n</think>\n\n"
            "<answer>\n天气：晴天\n时间：白天\n场景：高速公路。\n"
            "最终结论：本视频块检出以下事件。\nclass1: 违法停车\n</answer>"
        )
        correction = "应急车道占用：观察：一辆白色面包车静止于应急车道。结论：构成占用。"
        responses = iter([forward, correction])

        def caller(system: str, user: str, video_path, **_kwargs):
            return next(responses)

        events: list = []
        engine = _EmittingEngine(
            caller=caller,
            templates=templates,
            categories=categories,
            max_rounds=2,
            names=names,
            on_event=lambda type_, payload: events.append((type_, payload)),
        )
        result = engine.run("01-02_Event_test_1.mp4")
        assert result.match is True

        completed = [p for t, p in events if t == "round_completed"]
        assert [c["trace"]["kind"] for c in completed] == ["forward", "correct_missing"]
        # 关键回归断言:纠错轮的 SSE trace 必须带 patched=True
        assert completed[1]["trace"]["patched"] is True
        assert completed[1]["trace"]["event_ids"] == [2]


class TestEmittingEngineReviewExtraNormalGT:
    """_EmittingEngine._review_extra 必须接收并透传 normal_ground_truth(09 真值)。

    回归:基类 SftForgeEngine._review_extra 新增 normal_ground_truth 参数后,
    若 Web 子类覆写签名未同步,09 视频触发 review_extra 时 TypeError,
    整任务被 runner 兜底判失败。
    """

    def test_review_extra_forwards_normal_ground_truth(self) -> None:
        from traffic_analyzer.models.schemas import EventCategory, PromptTemplate
        from traffic_analyzer.sftforge.engine import (
            TEMPLATE_CORRECT_MISSING,
            TEMPLATE_FORWARD,
            TEMPLATE_REVIEW_EXTRA,
        )
        from traffic_analyzer.web.sftforge.service import _EmittingEngine

        names = {1: "违法停车", 2: "应急车道占用", 9: "无异常事件"}
        templates = {
            TEMPLATE_FORWARD: PromptTemplate(
                template_id=TEMPLATE_FORWARD, name="fwd", user_prompt="正向 {{ event_definitions }}"
            ),
            TEMPLATE_CORRECT_MISSING: PromptTemplate(
                template_id=TEMPLATE_CORRECT_MISSING, name="cm",
                user_prompt="补 {{ event_name }}:{{ event_definition }}",
            ),
            TEMPLATE_REVIEW_EXTRA: PromptTemplate(
                template_id=TEMPLATE_REVIEW_EXTRA, name="re",
                user_prompt="复核 {{ event_name }}:{{ event_definition }}|{{ first_round_segment }}|正常={{ normal_ground_truth }}",
            ),
        }
        categories = [
            EventCategory(
                event_id=1, event_code="A", name="Illegal Parking",
                name_zh="违法停车", description="", definition="违停判定定义",
            ),
            EventCategory(
                event_id=2, event_code="B", name="Emergency Lane Occupancy",
                name_zh="应急车道占用", description="", definition="占用判定定义",
            ),
        ]
        # 09 正常真值视频,初稿误报 class1 → 触发 review_extra(normal_ground_truth=True)
        forward = (
            "<think>\n违法停车：观察：面包车静止。结论：构成违法停车。\n\n"
            "应急车道占用：扫描应急车道，未见占用。\n</think>\n\n"
            "<answer>\n天气：晴天\n时间：白天\n场景：高速公路。\n"
            "最终结论：本视频块检出以下事件。\nclass1: 违法停车\n</answer>"
        )
        review = '{"verdict": "false_positive", "thinking": "经复核，该车持续行驶，原违停检出为误报。"}'
        responses = iter([forward, review])
        calls: list = []

        def caller(system: str, user: str, video_path, **_kwargs):
            calls.append(user)
            return next(responses)

        events: list = []
        engine = _EmittingEngine(
            caller=caller,
            templates=templates,
            categories=categories,
            max_rounds=2,
            names=names,
            on_event=lambda type_, payload: events.append((type_, payload)),
        )
        result = engine.run("09_Event_test_1.mp4")
        assert result.match is True
        assert result.r0_match is False
        # 关键回归断言:09 真值透传进复核 prompt(签名未同步时上方 run 已 TypeError)
        assert "正常=True" in calls[1]

        completed = [p for t, p in events if t == "round_completed"]
        assert [c["trace"]["kind"] for c in completed] == ["forward", "review_extra"]
        assert completed[1]["trace"]["patched"] is True
