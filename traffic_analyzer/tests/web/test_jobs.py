"""Infer-job queue, progress-event lane parsing and lifecycle tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import (
    _FAKE_INFER_SCRIPT,
    _PROGRESS_PREAMBLE,
    _SLEEP_CMD,
    _fake_expert_script,
    _make_tree_workspace,
    _make_workspace,
    _wait_for_job,
    _wait_running,
    _wait_until,
)


# ---------------------------------------------------------------------------
# Jobs: infer queue + progress parsing
# ---------------------------------------------------------------------------

class TestJobs:
    def test_infer_progress_and_done(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", _FAKE_INFER_SCRIPT],
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"stems": ["v1"]})
        assert resp.status_code == 200
        job_id = resp.json()["job_ids"][0]

        job = _wait_for_job(client, job_id)
        assert job["kind"] == "infer"
        assert job["stem"] == "v1"
        assert job["status"] == "done"
        assert job["returncode"] == 0
        assert job["progress"]["step_index"] == 5
        assert job["progress"]["step_label"] == "报告"
        assert job["progress"]["total_steps"] == 5
        assert job["progress"]["fraction"] == 1.0
        assert any("[3.5/4]" in line for line in job["log_tail"])

        # Output directory for the analysis was created up front.
        assert (workspace / "analysis" / "v1").is_dir()

    def test_step_marker_mapping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """step 3.5 maps to step 4 (SFT), not to step 3."""
        from traffic_analyzer.web.jobs import Job, JobManager

        manager = JobManager()
        manager.submit(
            "infer",
            [
                sys.executable,
                "-c",
                _PROGRESS_PREAMBLE + "ev(type='step', step=3.5, total=4, name='SFT')\n",
            ],
            stem="x",
        )
        job = manager._jobs[1]
        deadline = time.time() + 15
        while job.status not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.05)
        assert job.step_index == 4
        assert job.step_label == "SFT"

    def test_infer_serial_queue_two_videos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", f"print('{stem}')"],
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"stems": ["v1", "v2"]})
        job_ids = resp.json()["job_ids"]
        assert len(job_ids) == 2
        assert job_ids[1] == job_ids[0] + 1
        for job_id in job_ids:
            assert _wait_for_job(client, job_id)["status"] == "done"

    def test_infer_unknown_stem_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.post("/api/infer", json={"stems": ["ghost"]})
        assert resp.status_code == 404

    def test_build_infer_command_nested_rel(self, tmp_path: Path) -> None:
        """argv points at workspace/<rel> video and flat analysis/<stem>/ output."""
        from traffic_analyzer.web.jobs import build_infer_command

        argv = build_infer_command(tmp_path, "sub/nested.mp4", "nested")
        assert argv[argv.index("--video") + 1] == str(tmp_path / "sub" / "nested.mp4")
        assert argv[argv.index("--output") + 1] == str(
            tmp_path / "analysis" / "nested" / "report.md"
        )
        assert argv[argv.index("--sft-output-dir") + 1] == str(
            tmp_path / "analysis" / "nested"
        )

    def test_infer_nested_rel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _make_tree_workspace(tmp_path)
        captured: Dict[str, Any] = {}

        def _spy(ws: Path, rel: str, stem: str) -> List[str]:
            captured["rel"] = rel
            captured["stem"] = stem
            return [sys.executable, "-c", "print('ok')"]

        monkeypatch.setattr("traffic_analyzer.web.jobs.build_infer_command", _spy)
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"rels": ["sub/nested.mp4"]})
        assert resp.status_code == 200
        assert captured == {"rel": "sub/nested.mp4", "stem": "nested"}

        job = _wait_for_job(client, resp.json()["job_ids"][0])
        assert job["status"] == "done"
        assert job["stem"] == "nested"
        assert job["rel"] == "sub/nested.mp4"
        # Flat results contract: analysis/<stem>/ was created up front.
        assert (workspace / "analysis" / "nested").is_dir()

    def test_infer_duplicate_stem_rels_400(self, tmp_path: Path) -> None:
        """Two rels sharing a stem would collide in analysis/<stem>/."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "a" / "dup.mp4").write_bytes(b"")
        (tmp_path / "b" / "dup.mp4").write_bytes(b"")
        client = TestClient(create_app(workspace=str(tmp_path)))

        resp = client.post("/api/infer", json={"rels": ["a/dup.mp4", "b/dup.mp4"]})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "dup" in detail
        assert "a/dup.mp4" in detail
        assert "b/dup.mp4" in detail
        # Whole request rejected: no jobs were queued.
        assert client.get("/api/jobs").json() == []

    def test_infer_rel_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.post("/api/infer", json={"rels": ["../v1.mp4"]}).status_code == 404
        assert client.post(
            "/api/infer", json={"rels": ["sub/../../etc/passwd.mp4"]}
        ).status_code == 404

    def test_infer_rel_not_a_video_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.post("/api/infer", json={"rels": ["notes.txt"]}).status_code == 404
        assert client.post("/api/infer", json={"rels": ["ghost.mp4"]}).status_code == 404

    def test_infer_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/infer", json={"stems": ["v1"]})
        assert resp.status_code == 400

    def test_failed_job_records_returncode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", "import sys; sys.exit(3)"],
        )
        client = TestClient(create_app(workspace=str(workspace)))
        job_id = client.post("/api/infer", json={"stems": ["v1"]}).json()["job_ids"][0]
        job = _wait_for_job(client, job_id)
        assert job["status"] == "failed"
        assert job["returncode"] == 3

    def test_log_tail_capped(self, tmp_path: Path) -> None:
        from traffic_analyzer.web.jobs import JobManager

        manager = JobManager()
        manager.submit("infer", [sys.executable, "-c", "print(*range(100), sep='\\n')"], stem="x")
        job = manager._jobs[1]
        deadline = time.time() + 15
        while job.status not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.05)
        assert len(job.log_tail) == 30
        assert job.log_tail[-1] == "99"
# ---------------------------------------------------------------------------
# Jobs: progress-file lane events
# ---------------------------------------------------------------------------
class TestExpertProgress:
    def _submit(self, script: str) -> Any:
        from traffic_analyzer.web.jobs import JobManager

        manager = JobManager()
        manager.submit("infer", [sys.executable, "-c", script], stem="x")
        return manager._jobs[1]

    def test_lanes_and_fraction_climb(self, tmp_path: Path) -> None:
        # The fake child parks on gate files instead of sleeping, so each
        # mid-run state below stays stable while we assert it (no timing window).
        gate1 = tmp_path / "gate1"
        gate2 = tmp_path / "gate2"
        job = self._submit(_fake_expert_script(gate1, gate2))

        # Mid expert phase: fraction = mean of ALL lane fractions, never
        # regressing below the step estimate (register-time mean is 0).
        # Register also seeds the two stage lanes (SFT 标注/报告) as queued.
        assert _wait_until(
            lambda: len(job.experts) == 5
            and job.experts[0]["fraction"] == 0.5
            and job.experts[1]["fraction"] == 0.25
        )
        assert job.experts[0] == {
            "name": "违停", "status": "running", "detected": None,
            "fraction": 0.5, "label": "抽帧",
        }
        assert job.experts[2]["status"] == "queued"
        assert job.experts[3]["name"] == "SFT 标注"
        assert job.experts[3]["status"] == "queued"
        assert job.experts[4]["name"] == "报告"
        assert job.experts[4]["status"] == "queued"
        assert job.step_index == 2
        lane_mean = (0.5 + 0.25 + 0.0 + 0.0 + 0.0) / 5
        assert job.fraction == pytest.approx(max(2 / 5, lane_mean))

        # Adjudication phase: once the all-lane mean passes the step
        # estimate, the fraction IS the mean (sidebar == lanes scale).
        gate1.touch()  # release the child into the adjudication phase
        assert _wait_until(
            lambda: job.step_index == 3 and job.experts[2]["fraction"] == 0.5
        )
        assert job.experts[0]["status"] == "done"
        assert job.experts[0]["detected"] is True
        assert job.experts[0]["fraction"] == 1.0
        assert job.experts[1]["status"] == "done"
        assert job.experts[1]["detected"] is False
        assert job.experts[2]["status"] == "running"
        assert job.experts[2]["label"] == "汇总"
        assert job.fraction == pytest.approx((1.0 + 1.0 + 0.5 + 0.0 + 0.0) / 5)

        gate2.touch()  # release the child to completion
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.experts[2]["detected"] is True
        assert job.fraction == 1.0

    def test_error_lane_and_to_dict(self) -> None:
        script = _PROGRESS_PREAMBLE + (
            "ev(type='step', step=2, total=4, name='专家')\n"
            "ev(type='register', total=2, lanes=['占道','裁决'])\n"
            "ev(type='lane_done', done=1, total=2, lane='占道', result='error')\n"
            "ev(type='lane_done', done=2, total=2, lane='裁决', result='undetected')\n"
        )
        job = self._submit(script)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.experts[0] == {
            "name": "占道", "status": "error", "detected": None,
            "fraction": 1.0, "label": "",
        }
        assert job.experts[1]["status"] == "done"
        assert job.experts[1]["detected"] is False
        # to_dict exposes lanes under progress (as a copy).
        progress = job.to_dict()["progress"]
        assert progress["experts"] == job.experts
        assert progress["experts"] is not job.experts

    def test_step_markers_without_lanes_unchanged(self) -> None:
        """No register event: fraction stays step_index/5 (legacy behavior)."""
        job = self._submit(_FAKE_INFER_SCRIPT)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.experts == []
        assert job.step_index == 5


class TestExpertPhasesApi:
    def test_missing_file_404(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.web.app._EXPERT_PHASES_JSON", tmp_path / "nope.json"
        )
        client = TestClient(create_app())
        assert client.get("/api/expert-phases").status_code == 404

    def test_returns_file_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "version": 1,
            "experts": [{"name": "占道", "phases": ["抽帧", "掩码"]}],
        }
        path = tmp_path / "expert_phases.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr("traffic_analyzer.web.app._EXPERT_PHASES_JSON", path)
        client = TestClient(create_app())
        resp = client.get("/api/expert-phases")
        assert resp.status_code == 200
        assert resp.json() == payload
# ---------------------------------------------------------------------------
# Jobs: process lifecycle (shutdown / cancel)
# ---------------------------------------------------------------------------
class TestJobLifecycle:
    def _sleep_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: list(_SLEEP_CMD),
        )
        return create_app(workspace=str(workspace))

    def test_shutdown_kills_running_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lifespan shutdown (Ctrl+C) kills the running analyze child."""
        app = self._sleep_app(tmp_path, monkeypatch)
        with TestClient(app) as client:
            job_id = client.post("/api/infer", json={"stems": ["v1"]}).json()["job_ids"][0]
            job = app.state.jobs._jobs[job_id]
            proc = _wait_running(job)
            # start_new_session: the child is its own session leader.
            assert os.getsid(proc.pid) == proc.pid
        # Exiting the TestClient ran the lifespan shutdown.
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None
        assert job.status == "failed"
        assert any("server shutdown" in line for line in job.log_tail)

    def test_shutdown_fails_queued_jobs_without_starting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queued jobs at shutdown are failed and their command never runs."""
        marker = tmp_path / "job2_started.txt"
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: (
                list(_SLEEP_CMD)
                if stem == "v1"
                else [sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"]
            ),
        )
        app = create_app(workspace=str(workspace))
        with TestClient(app) as client:
            ids = client.post("/api/infer", json={"stems": ["v1", "v2"]}).json()["job_ids"]
            job1 = app.state.jobs._jobs[ids[0]]
            job2 = app.state.jobs._jobs[ids[1]]
            _wait_running(job1)  # serial queue: v2 stays queued behind v1
            assert job2.status == "queued"
        assert job2.status == "failed"
        assert job2.proc is None
        assert any("server shutdown" in line for line in job2.log_tail)
        assert not marker.exists()

    def test_cancel_running_job(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        app = self._sleep_app(tmp_path, monkeypatch)
        client = TestClient(app)
        job_id = client.post("/api/infer", json={"stems": ["v1"]}).json()["job_ids"][0]
        job = app.state.jobs._jobs[job_id]
        proc = _wait_running(job)

        try:
            resp = client.post(f"/api/jobs/{job_id}/cancel")
            assert resp.status_code == 200
            deadline = time.time() + 5
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            assert proc.poll() is not None
            assert job.status == "failed"
            assert job.returncode is not None
            assert any("cancelled by user" in line for line in job.log_tail)

            # Second cancel: job is already terminal.
            assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
        finally:
            # Never leak the 300s sleep child if an assertion above fails.
            if proc.poll() is None:
                proc.kill()

    def test_cancel_unknown_id_404(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        assert client.post("/api/jobs/999/cancel").status_code == 404

    def test_cancel_queued_job_never_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "job2_started.txt"
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: (
                list(_SLEEP_CMD)
                if stem == "v1"
                else [sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"]
            ),
        )
        app = create_app(workspace=str(workspace))
        client = TestClient(app)
        ids = client.post("/api/infer", json={"stems": ["v1", "v2"]}).json()["job_ids"]
        job1 = app.state.jobs._jobs[ids[0]]
        job2 = app.state.jobs._jobs[ids[1]]
        _wait_running(job1)
        assert job2.status == "queued"

        try:
            resp = client.post(f"/api/jobs/{ids[1]}/cancel")
            assert resp.status_code == 200
            assert job2.status == "failed"
            assert job2.proc is None
            assert any("cancelled by user" in line for line in job2.log_tail)

            # Clean up job1; the worker must then skip the cancelled job2.
            client.post(f"/api/jobs/{ids[0]}/cancel")
            deadline = time.time() + 5
            while job1.proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)  # give the worker a chance to (not) start job2
            assert not marker.exists()
        finally:
            # Never leak the 300s sleep child if an assertion above fails.
            if job1.proc is not None and job1.proc.poll() is None:
                job1.proc.kill()

    def test_cancel_terminal_job_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", "pass"],
        )
        client = TestClient(create_app(workspace=str(workspace)))
        job_id = client.post("/api/infer", json={"stems": ["v1"]}).json()["job_ids"][0]
        assert _wait_for_job(client, job_id)["status"] == "done"
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
