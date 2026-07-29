"""Regression tests for the wave-1A web backend hardening.

Covers the critical fixes that are easiest to regress:
- cancel landing while the worker is still inside ``subprocess.Popen``
  (job.proc not yet attached) must still kill the spawned child;
- POST /api/infer dedup: a queued/running infer for the same stem -> 409;
- corrupt (unparseable) analysis JSON on PUT -> 422, never a silent 404.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app


def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / "v1.mp4").write_bytes(b"")
    (tmp_path / "v2.avi").write_bytes(b"")
    return tmp_path


def _sft_payload() -> Dict[str, Any]:
    return {
        "chunk": "chunk #1",
        "idx": 1,
        "action": [2],
        "description": (
            "<think>\n违法停车：未发现。\n\n应急车道占用：一辆白色小车静止于应急车道。\n"
            "</think>\n<answer>\n天气：晴天\n</answer>"
        ),
        "start_timestamp": 0.0,
        "end_timestamp": 15.0,
        "chunk_name": "v1.mp4",
    }


def _evidence_payload() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "video": {"file_name": "v1.mp4"},
        "events": [],
    }


def _wait_until(cond: Any, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


_SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(300)"]


class TestCancelRace:
    def test_cancel_during_popen_kills_spawned_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cancel() lands while the worker is inside Popen (job.proc still None).

        The worker must notice the non-running status right after attaching
        proc and terminate the just-spawned child instead of losing track of it.
        """
        from traffic_analyzer.web import jobs as jobs_mod

        manager = jobs_mod.JobManager()
        spawned: List[Any] = []
        real_popen = subprocess.Popen

        def _racy_popen(argv: List[str], **kwargs: Any) -> Any:
            proc = real_popen(argv, **kwargs)
            spawned.append(proc)
            manager.cancel(1)  # cancel before the worker attaches job.proc
            return proc

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.subprocess.Popen", _racy_popen
        )
        manager.submit("infer", list(_SLEEP_CMD), stem="v1")
        job = manager._jobs[1]

        assert _wait_until(lambda: job.status == "failed" and spawned)
        proc = spawned[0]
        assert _wait_until(lambda: proc.poll() is not None, timeout=5)
        assert job.returncode is not None
        assert any("cancelled by user" in line for line in job.log_tail)

    def test_submit_after_shutdown_rejected(self) -> None:
        from traffic_analyzer.web.jobs import JobManager

        manager = JobManager()
        manager.shutdown()
        with pytest.raises(RuntimeError):
            manager.submit("infer", list(_SLEEP_CMD), stem="v1")

    def test_job_timeout_marks_failed(self) -> None:
        from traffic_analyzer.web.jobs import JobManager

        manager = JobManager(timeout_sec=0.5)
        manager.submit("infer", list(_SLEEP_CMD), stem="v1")
        job = manager._jobs[1]
        assert _wait_until(lambda: job.status == "failed", timeout=10)
        assert job.returncode is not None
        assert any("timed out" in line for line in job.log_tail)


class TestInferDedup:
    def test_duplicate_active_infer_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, rel, stem: list(_SLEEP_CMD),
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"stems": ["v1"]})
        assert resp.status_code == 200
        job_id = resp.json()["job_ids"][0]

        dup = client.post("/api/infer", json={"stems": ["v1"]})
        assert dup.status_code == 409
        assert str(job_id) in dup.json()["detail"]
        # 去重不应产生新任务。
        assert [j["id"] for j in client.get("/api/jobs").json()] == [job_id]

        # 取消(进入终态)后同一 stem 可以再次提交。
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        resp2 = client.post("/api/infer", json={"stems": ["v1"]})
        assert resp2.status_code == 200
        job2 = resp2.json()["job_ids"][0]
        client.post(f"/api/jobs/{job2}/cancel")  # 清理 sleep 子进程


class TestCorruptJsonPut:
    def _client(self, tmp_path: Path) -> TestClient:
        workspace = _make_workspace(tmp_path)
        out_dir = workspace / "analysis" / "v1"
        out_dir.mkdir(parents=True)
        (out_dir / "v1.json").write_text(
            json.dumps(_sft_payload(), ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (out_dir / "v1_evidence.json").write_text(
            json.dumps(_evidence_payload(), ensure_ascii=False), encoding="utf-8"
        )
        return TestClient(create_app(workspace=str(workspace)))

    def test_put_evidence_corrupt_disk_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        (tmp_path / "analysis" / "v1" / "v1_evidence.json").write_text(
            '{"truncated": ', encoding="utf-8"
        )
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 422  # 损坏 ≠ 不存在,不得静默 404
        assert "corrupt" in resp.json()["detail"]

    def test_put_sft_corrupt_disk_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        (tmp_path / "analysis" / "v1" / "v1.json").write_text(
            '{"truncated": ', encoding="utf-8"
        )
        resp = client.put("/api/results/v1/sft", json=_sft_payload())
        assert resp.status_code == 422
        assert "corrupt" in resp.json()["detail"]

    def test_get_results_corrupt_500(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        (tmp_path / "analysis" / "v1" / "v1.json").write_text(
            '{"truncated": ', encoding="utf-8"
        )
        resp = client.get("/api/results/v1")
        assert resp.status_code == 500
