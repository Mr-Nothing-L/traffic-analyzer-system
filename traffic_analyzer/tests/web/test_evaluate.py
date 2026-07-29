"""Evaluation endpoint tests (incl. scripts/batch_evaluate.py contracts)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List

from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _make_results, _make_workspace, _wait_for_job


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_evaluate_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.post("/api/evaluate", json={}).status_code == 400

    def test_evaluate_without_results_400(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.post("/api/evaluate", json={}).status_code == 400

        # Empty analysis directory is still a 400.
        (workspace / "analysis").mkdir()
        assert client.post("/api/evaluate", json={}).status_code == 400

    def test_evaluate_latest_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/evaluate/latest").status_code == 404

    def test_evaluate_run_and_read_latest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        metrics = {"macro": {"precision": 1.0}, "total_videos": 1}

        def _fake_command(ws: Path) -> List[str]:
            latest = ws / "analysis" / "evaluation" / "latest.json"
            script = (
                "import json, pathlib;"
                f"p = pathlib.Path(r'{latest}');"
                "p.parent.mkdir(parents=True, exist_ok=True);"
                f"p.write_text(json.dumps({metrics!r}), encoding='utf-8')"
            )
            return [sys.executable, "-c", script]

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_evaluate_command", _fake_command
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/evaluate", json={})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        job = _wait_for_job(client, job_id)
        assert job["kind"] == "evaluate"
        assert job["status"] == "done"
        assert job["progress"]["fraction"] == 1.0

        latest = client.get("/api/evaluate/latest")
        assert latest.status_code == 200
        assert latest.json()["total_videos"] == 1
class TestBatchEvaluateAtomicWrite:
    def test_atomic_write_text(self, tmp_path: Path, batch_evaluate_module: Any) -> None:
        module = batch_evaluate_module

        target = tmp_path / "latest.json"
        module._atomic_write_text(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        assert not (tmp_path / "latest.json.tmp").exists()
        module._atomic_write_text(target, '{"a": 2}')
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


class TestBatchEvaluateNestedVideo:
    def test_nested_video_matched_by_rglob(
        self, tmp_path: Path, batch_evaluate_module: Any
    ) -> None:
        """视频在子目录时(rglob 回退)也能被评估,不再报 No matching video。"""
        module = batch_evaluate_module

        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "01_Event_129_1_1.mp4").write_bytes(b"\x00" * 64)
        out_dir = ws / "analysis" / "01_Event_129_1_1"
        out_dir.mkdir(parents=True)
        (out_dir / "report.md").write_text(
            "# 报告\n\n二进制编码: `1_0_0_0_0_0_0_0_0_0`\n", encoding="utf-8"
        )
        output = tmp_path / "latest.json"
        rc = module.main(
            [
                "--video-dir", str(ws),
                "--report-dir", str(ws / "analysis"),
                "--gt-mode", "filename",
                "--output", str(output),
            ]
        )
        assert rc == 0
        text = output.read_text(encoding="utf-8")
        assert "01_Event_129_1_1" in text
class TestEvaluateLatestCorrupt:
    def test_corrupt_latest_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        latest = workspace / "analysis" / "evaluation" / "latest.json"
        latest.parent.mkdir(parents=True)
        latest.write_text('{"truncated": ', encoding="utf-8")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/evaluate/latest")
        assert resp.status_code == 404  # not a 500
        assert "mid-write" in resp.json()["detail"]
