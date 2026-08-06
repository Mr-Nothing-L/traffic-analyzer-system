"""Shared helpers and fixtures for the web UI backend tests (traffic_analyzer.web)."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Workspace / results fabricators
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
                "event_id": 1,
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
                "event_id": 2,
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


def _sft_payload() -> Dict[str, Any]:
    """完整的 SFT 样本(与 <stem>.json 契约的 7 个键一致)。"""
    return {
        "chunk": "chunk #1",
        "idx": 1,
        "action": [2],
        "description": (
            "<think>\n违法停车：未发现。\n\n应急车道占用：一辆白色小车静止于应急车道。\n"
            "</think>\n<answer>\n天气：晴天\n时间：白天\n场景：高速公路主路。\n"
            "最终结论：本视频块检出以下事件。\nclass2: 应急车道占用\n</answer>"
        ),
        "start_timestamp": 0.0,
        "end_timestamp": 15.0,
        "chunk_name": "v1.mp4",
    }


def _make_tree_workspace(tmp_path: Path) -> Path:
    """Workspace with nested dirs, videos at two levels, dotfiles, plain files."""
    (tmp_path / "v1.mp4").write_bytes(b"")
    (tmp_path / "v2.avi").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("doc", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.mp4").write_bytes(b"")
    (sub / "readme.md").write_text("hi", encoding="utf-8")
    (tmp_path / ".hiddendir").mkdir()
    return tmp_path


def _make_tree(root: Path) -> Path:
    """Directory tree: two visible dirs, one hidden dir and a plain file."""
    (root / "beta").mkdir()
    (root / "Alpha").mkdir()
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("not a dir", encoding="utf-8")
    return root


def _make_stream_workspace(tmp_path: Path) -> Path:
    """Workspace with a nested (dummy-content) video plus a plain file."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.mp4").write_bytes(b"\x00" * 2048)
    (sub / "notes.txt").write_text("not a video", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Job waiting
# ---------------------------------------------------------------------------


def _wait_for_job(client: TestClient, job_id: int, timeout: float = 15.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {job["id"]: job for job in client.get("/api/jobs").json()}
        job = jobs[job_id]
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def _wait_until(cond: Any, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


# Fake children write structured progress events (JSONL) to the file named by
# TRAFFIC_ANALYZER_PROGRESS_FILE (the job worker creates it and sets the env
# var); stdout stays free-form and only feeds log_tail.
_PROGRESS_PREAMBLE = (
    "import json, os, time\n"
    "_pf = os.environ['TRAFFIC_ANALYZER_PROGRESS_FILE']\n"
    "def ev(**kw):\n"
    "    kw['ts'] = time.time()\n"
    "    open(_pf, 'a', encoding='utf-8').write(json.dumps(kw, ensure_ascii=False) + '\\n')\n"
)


_FAKE_INFER_SCRIPT = (
    _PROGRESS_PREAMBLE
    + "ev(type='step', step=1, total=4, name='预处理')\nprint('[1/4] Preprocessing video...');"
    "print('[2/4] Expert Agent Layer...');"
    "print('[3/4] Adjudication...');"
    "print('[3.5/4] SFT label rewrite...');"
    "print('[4/4] Generating report...')\n"
    "ev(type='step', step=2, total=4, name='专家')\n"
    "ev(type='step', step=3, total=4, name='裁决')\n"
    "ev(type='step', step=3.5, total=4, name='SFT')\n"
    "ev(type='step', step=4, total=4, name='报告')\n"
)


# Fake child emitting the structured lane-event contract; instead of fixed
# sleeps the child parks on gate files that the test creates when it is done
# asserting a mid-run state — this keeps the sampling windows race-free.
def _fake_expert_script(gate1: Path, gate2: Path) -> str:
    return (
        _PROGRESS_PREAMBLE
        + "def w(f):\n"
        "    while not os.path.exists(f):\n"
        "        time.sleep(0.02)\n"
        "ev(type='step', step=2, total=4, name='专家')\n"
        "ev(type='register', total=3, lanes=['违停','占道','裁决'])\n"
        "ev(type='start', lane='违停')\n"
        "ev(type='phase', lane='违停', fraction=0.5, label='抽帧')\n"
        "ev(type='start', lane='占道')\n"
        "ev(type='phase', lane='占道', fraction=0.25, label='掩码')\n"
        f"w(r'{gate1}')\n"
        "ev(type='phase', lane='违停', fraction=1.0, label='检出')\n"
        "ev(type='lane_done', done=1, total=3, lane='违停', result='detected')\n"
        "ev(type='phase', lane='占道', fraction=1.0, label='未检出')\n"
        "ev(type='lane_done', done=2, total=3, lane='占道', result='undetected')\n"
        "ev(type='step', step=3, total=4, name='裁决')\n"
        "ev(type='start', lane='裁决')\n"
        "ev(type='phase', lane='裁决', fraction=0.5, label='汇总')\n"
        f"w(r'{gate2}')\n"
        "ev(type='phase', lane='裁决', fraction=1.0, label='检出')\n"
        "ev(type='lane_done', done=3, total=3, lane='裁决', result='detected')\n"
        "ev(type='step', step=3.5, total=4, name='SFT')\n"
        "ev(type='step', step=4, total=4, name='报告')\n"
    )


_SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(300)"]


def _wait_running(job: Any, timeout: float = 15.0) -> Any:
    """Wait until the job has a live child process; return the proc."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status == "running" and job.proc is not None and job.proc.poll() is None:
            return job.proc
        time.sleep(0.05)
    raise AssertionError(f"job {job.id} did not start a live child in {timeout}s")


# ---------------------------------------------------------------------------
# Frame extraction fakes
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


@pytest.fixture(autouse=True)
def _reset_fake_capture_instances() -> Any:
    """Class-level counter must not leak across tests (order-dependent asserts)."""
    _FakeCapture.instances = 0
    yield


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate config/.env and users.db: web auth configure() falls back to them,
    and ConfigManager's load_dotenv injects config/.env into os.environ.
    Neutralize all so tests stay deterministic regardless of local contents."""
    from traffic_analyzer.web import auth as _auth
    from traffic_analyzer.web import user_store as _user_store
    from traffic_analyzer.web import workspace as _workspace

    monkeypatch.setattr(_auth, "_ENV_PATH", tmp_path / ".env.nonexistent")
    # users.db 放进点目录:不污染 workspace tree/videos 列表(点目录会被跳过)。
    monkeypatch.setattr(_user_store, "DB_PATH", tmp_path / ".auth" / "users.db")
    monkeypatch.setattr(_workspace, "_CONFIG_ENV_PATH", tmp_path / ".env.nonexistent")
    monkeypatch.delenv(_auth.USERS_ENV_VAR, raising=False)
    monkeypatch.delenv(_auth.SECRET_ENV_VAR, raising=False)
    monkeypatch.delenv(_workspace.WORKSPACE_DIRS_ENV_VAR, raising=False)
    yield


@pytest.fixture(autouse=True)
def _clear_web_caches() -> Any:
    """进程内 TTL 缓存(dashboard/videos)跨用例隔离:每个用例前后清空。"""
    from traffic_analyzer.web import workspace as _workspace

    _workspace.invalidate_caches()
    yield
    _workspace.invalidate_caches()


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


# ---------------------------------------------------------------------------
# Video streaming / transcode
# ---------------------------------------------------------------------------

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None or _FFPROBE is None, reason="ffmpeg/ffprobe not installed"
)

_MP4TEST_MPEG4 = (
    Path(__file__).resolve().parents[3] / "演示区" / "01-02-04_Event_2048_1750664210002_1.mp4"
)
requires_mp4test = pytest.mark.skipif(
    not _MP4TEST_MPEG4.is_file(), reason="demo clip not available"
)


def _make_tiny_video(path: Path, frames: int = 8) -> Path:
    """Tiny MPEG-4 Part 2 (mp4v) clip — not browser-native, needs transcode."""
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    if not writer.isOpened():
        writer.release()
        pytest.skip("cv2 VideoWriter cannot write mp4v on this host")
    for i in range(frames):
        writer.write(np.full((48, 64, 3), i * 20, dtype=np.uint8))
    writer.release()
    if path.stat().st_size == 0:
        pytest.skip("cv2 VideoWriter produced an empty mp4v file")
    return path


# ---------------------------------------------------------------------------
# scripts/batch_evaluate.py loader (module is a script, not a package)
# ---------------------------------------------------------------------------


@pytest.fixture()
def batch_evaluate_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "batch_evaluate",
        Path(__file__).resolve().parents[3] / "scripts" / "batch_evaluate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
