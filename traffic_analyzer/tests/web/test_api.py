"""Tests for the web UI backend (traffic_analyzer.web) and the web CLI."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.cli import build_parser, main
from traffic_analyzer.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Workspace with two (empty) video files."""
    (tmp_path / "v1.mp4").write_bytes(b"")
    (tmp_path / "v2.avi").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("not a video", encoding="utf-8")
    return tmp_path


def _make_results(workspace: Path, stem: str = "v1") -> Path:
    """Fabricate analysis/<stem>/ with report, SFT sample, evidence, images."""
    out_dir = workspace / "analysis" / stem
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("# 报告\n内容", encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"chunk": 0, "idx": 0, "action": [1], "description": "<think>\n</think>"}),
        encoding="utf-8",
    )
    (out_dir / f"{stem}_evidence.json").write_text(
        json.dumps(_evidence_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "images" / "zoom_1.jpg").write_bytes(b"\xff\xd8jpeg")
    return out_dir


def _evidence_payload() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "video": {
            "file_name": "v1.mp4",
            "duration_sec": 15.0,
            "fps": 25.0,
            "width": 1920,
            "height": 1080,
        },
        "events": [
            {
                "event_id": 0,
                "name": "违法停车",
                "detected": False,
                "calibration": {
                    "frame_index": None,
                    "emergency_polygon_rel": None,
                    "chevron_polygon_rel": None,
                },
                "evidence_regions": [],
                "gallery_images": [],
            },
            {
                "event_id": 1,
                "name": "应急车道占用",
                "detected": True,
                "calibration": {
                    "frame_index": 4,
                    "emergency_polygon_rel": [[0.1, 0.2], [0.3, 0.2], [0.3, 0.8], [0.1, 0.8]],
                    "chevron_polygon_rel": [[0.4, 0.4], [0.5, 0.4], [0.5, 0.6], [0.4, 0.6]],
                },
                "evidence_regions": [
                    {
                        "frame_index": 4,
                        "box_rel": [0.12, 0.3, 0.2, 0.5],
                        "label": "白色轿车",
                        "image": "images/zoom_1.jpg",
                    }
                ],
                "gallery_images": ["images/overlay.jpg"],
            },
        ],
    }


def _wait_for_job(client: TestClient, job_id: int, timeout: float = 15.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {job["id"]: job for job in client.get("/api/jobs").json()}
        job = jobs[job_id]
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


_FAKE_INFER_SCRIPT = (
    "print('[1/4] Preprocessing video...');"
    "print('[2/4] Expert Agent Layer...');"
    "print('[3/4] Adjudication...');"
    "print('[3.5/4] SFT label rewrite...');"
    "print('[4/4] Generating report...')"
)


# ---------------------------------------------------------------------------
# Workspace endpoints
# ---------------------------------------------------------------------------


class TestWorkspace:
    def test_get_workspace_empty(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/workspace").json() == {"path": None}

    def test_set_and_get_workspace(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(tmp_path)})
        assert resp.status_code == 200
        assert resp.json() == {"path": str(tmp_path)}
        assert client.get("/api/workspace").json() == {"path": str(tmp_path)}

    def test_set_workspace_missing_path(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(tmp_path / "nope")})
        assert resp.status_code == 400

    def test_set_workspace_not_a_directory(self, tmp_path: Path) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(file_path)})
        assert resp.status_code == 400

    def test_preset_workspace_via_factory(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        assert client.get("/api/workspace").json() == {"path": str(tmp_path)}

    def test_list_videos_requires_workspace(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/workspace/videos").status_code == 400

    def test_list_videos(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))

        videos = client.get("/api/workspace/videos").json()
        by_name = {v["name"]: v for v in videos}
        assert set(by_name) == {"v1.mp4", "v2.avi"}

        v1 = by_name["v1.mp4"]
        assert v1["stem"] == "v1"
        assert v1["size"] == 0
        assert isinstance(v1["mtime"], float)
        assert v1["has_results"] is True
        assert by_name["v2.avi"]["has_results"] is False


# ---------------------------------------------------------------------------
# Results reading
# ---------------------------------------------------------------------------


class TestResults:
    def test_get_results(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/results/v1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["report_md"] == "# 报告\n内容"
        assert data["sft_label"]["action"] == [1]
        assert data["evidence"]["schema_version"] == 1
        assert len(data["evidence"]["events"]) == 2

    def test_get_results_unknown_stem_returns_nulls(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        data = client.get("/api/results/v2").json()
        assert data == {"report_md": None, "sft_label": None, "evidence": None}


# ---------------------------------------------------------------------------
# Evidence editing
# ---------------------------------------------------------------------------


class TestEvidencePut:
    def _client(self, tmp_path: Path) -> TestClient:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        return TestClient(create_app(workspace=str(workspace)))

    def test_put_unchanged_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 200

    def test_put_polygon_edit_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["calibration"]["emergency_polygon_rel"][0] = [0.15, 0.25]
        payload["events"][1]["calibration"]["chevron_polygon_rel"] = None
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 200

        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1_evidence.json").read_text(encoding="utf-8")
        )
        assert disk["events"][1]["calibration"]["emergency_polygon_rel"][0] == [0.15, 0.25]
        assert disk["events"][1]["calibration"]["chevron_polygon_rel"] is None

    def test_put_box_and_label_edit_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["evidence_regions"][0]["box_rel"] = [0.1, 0.1, 0.9, 0.9]
        payload["events"][1]["evidence_regions"][0]["label"] = "黑色货车"
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 200

        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1_evidence.json").read_text(encoding="utf-8")
        )
        region = disk["events"][1]["evidence_regions"][0]
        assert region["box_rel"] == [0.1, 0.1, 0.9, 0.9]
        assert region["label"] == "黑色货车"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["events"][1].update({"name": "改名"}),
            lambda p: p["events"][1].update({"detected": False}),
            lambda p: p["events"][1].update({"event_id": 9}),
            lambda p: p["events"][1]["calibration"].update({"frame_index": 7}),
            lambda p: p["events"][1].update({"gallery_images": []}),
            lambda p: p["events"][1]["evidence_regions"][0].update({"frame_index": 1}),
            lambda p: p["events"][1]["evidence_regions"][0].update({"image": None}),
            lambda p: p["video"].update({"duration_sec": 99.0}),
            lambda p: p.update({"events": p["events"][:1]}),
        ],
    )
    def test_put_non_editable_change_422(self, tmp_path: Path, mutate: Any) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        mutate(payload)
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 422

    def test_put_coordinate_out_of_range_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["evidence_regions"][0]["box_rel"] = [0.1, 0.1, 1.5, 0.9]
        assert client.put("/api/results/v1/evidence", json=payload).status_code == 422

    def test_put_wrong_schema_version_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["schema_version"] = 2
        assert client.put("/api/results/v1/evidence", json=payload).status_code == 422

    def test_put_malformed_box_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["evidence_regions"][0]["box_rel"] = [0.1, 0.2]
        assert client.put("/api/results/v1/evidence", json=payload).status_code == 422

    def test_put_missing_evidence_file_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Result images
# ---------------------------------------------------------------------------


class TestResultImages:
    def test_get_image_ok(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/results/v1/images/zoom_1.jpg")
        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8jpeg"

    def test_get_image_missing_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/results/v1/images/nope.jpg").status_code == 404

    def test_get_image_path_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        # URL-encoded "../" — must never escape the images/ directory.
        resp = client.get("/api/results/v1/images/..%2F..%2Fv1_evidence.json")
        assert resp.status_code == 404
        assert client.get("/api/results/v1/images/..%2Fzoom_1.jpg").status_code == 404


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
        """[3.5/4] maps to step 4 (SFT), not to step 3."""
        from traffic_analyzer.web.jobs import Job, JobManager

        manager = JobManager()
        manager.submit(
            "infer",
            [sys.executable, "-c", "print('[3.5/4] SFT label rewrite...')"],
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


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


class _FakeCapture:
    instances = 0

    def __init__(self, path: str) -> None:
        type(self).instances += 1
        self._index = 0

    def get(self, prop: int) -> float:
        return 10.0

    def set(self, prop: int, value: float) -> bool:
        self._index = int(value)
        return True

    def read(self) -> Any:
        return True, f"frame-{self._index}"

    def release(self) -> None:
        pass


class _FakeBuf:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def tobytes(self) -> bytes:
        return self._data


def _install_fake_cv2(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCapture.instances = 0
    fake = SimpleNamespace(
        VideoCapture=_FakeCapture,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
        imencode=lambda ext, frame: (True, _FakeBuf(f"jpeg:{frame}".encode())),
    )
    monkeypatch.setattr("traffic_analyzer.web.frames.cv2", fake)


class TestFrames:
    def test_get_frame_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_cv2(monkeypatch)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/videos/v1/frame", params={"index": 3})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"jpeg:frame-3"

    def test_get_frame_index_out_of_range_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cv2(monkeypatch)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/videos/v1/frame", params={"index": 99}).status_code == 404

    def test_get_frame_unknown_video_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cv2(monkeypatch)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/videos/ghost/frame", params={"index": 0}).status_code == 404

    def test_frame_lru_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_cv2(monkeypatch)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        for _ in range(3):
            resp = client.get("/api/videos/v1/frame", params={"index": 5})
            assert resp.status_code == 200
        assert _FakeCapture.instances == 1  # second+ hits served from cache

        client.get("/api/videos/v1/frame", params={"index": 6})
        assert _FakeCapture.instances == 2


# ---------------------------------------------------------------------------
# CLI: web subcommand
# ---------------------------------------------------------------------------


class TestCliWeb:
    def test_web_help(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["web", "--help"])
        assert exc_info.value.code == 0

    def test_web_defaults(self) -> None:
        args = build_parser().parse_args(["web"])
        assert args.command == "web"
        assert args.host == "127.0.0.1"
        assert args.port == 8600
        assert args.workspace is None

    def test_web_custom_args(self) -> None:
        args = build_parser().parse_args(["web", "--host", "0.0.0.0", "--port", "9000", "-w", "/tmp/ws"])
        assert args.host == "0.0.0.0"
        assert args.port == 9000
        assert args.workspace == "/tmp/ws"

    def test_cmd_web_invalid_workspace(self, tmp_path: Path) -> None:
        from traffic_analyzer.cli import cmd_web

        args = build_parser().parse_args(["web", "--workspace", str(tmp_path / "nope")])
        assert cmd_web(args) == 1
