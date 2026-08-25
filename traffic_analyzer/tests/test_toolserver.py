"""Unit tests for the toolserver /tools/* endpoints.

[文件说明]
作用:用 FastAPI TestClient + 演示区真实视频验证 video_meta、
    extract_frames(count=2)、draw_boxes 与 workspace 越界 403 / 404 契约;
    另覆盖多允许根:POST /config/roots 注册/去重/非法根 400、
    新根内可访问、未注册根仍 403、/health 返回 roots。
上游:pytest 自动发现并执行本文件测试;视频缺失时相关用例 pytest.skip。
下游:traffic_analyzer/toolserver(被测模块)。
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.toolserver import create_app
from traffic_analyzer.web.frames import read_video_meta

WORKSPACE = Path(__file__).resolve().parents[2]
VIDEO_REL = "演示区/01-02_Event_129_1755579215119_1.mp4"
VIDEO_ABS = WORKSPACE / VIDEO_REL
SMALL_VIDEO_REL = "演示区/01-02-04_Event_2048_1750664210002_1.mp4"
SMALL_VIDEO_ABS = WORKSPACE / SMALL_VIDEO_REL

client = TestClient(create_app(WORKSPACE))

requires_video = pytest.mark.skipif(
    not VIDEO_ABS.is_file(), reason=f"demo video missing: {VIDEO_ABS}"
)
requires_small_video = pytest.mark.skipif(
    not SMALL_VIDEO_ABS.is_file(), reason=f"demo video missing: {SMALL_VIDEO_ABS}"
)
requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH"
)


@requires_video
def test_video_meta() -> None:
    resp = client.post("/tools/video_meta", json={"video_path": VIDEO_REL})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("duration_s", "fps", "width", "height", "frame_count"):
        assert key in body, f"missing key: {key}"
    assert body["frame_count"] > 0
    assert body["fps"] > 0
    assert body["width"] > 0 and body["height"] > 0
    assert body["duration_s"] == pytest.approx(
        body["frame_count"] / body["fps"], rel=1e-3
    )


@requires_video
def test_extract_frames_count_two() -> None:
    resp = client.post(
        "/tools/extract_frames", json={"video_path": VIDEO_REL, "count": 2}
    )
    assert resp.status_code == 200, resp.text
    frames = resp.json()["frames"]
    assert len(frames) == 2
    for frame in frames:
        raw = base64.b64decode(frame["jpeg_base64"])
        assert raw[:2] == b"\xff\xd8", "payload is not a JPEG"
        assert frame["width"] > 0 and frame["height"] > 0
        assert frame["timestamp"] >= 0


@requires_video
def test_extract_frames_max_frames_hard_cap() -> None:
    resp = client.post(
        "/tools/extract_frames",
        json={"video_path": VIDEO_REL, "max_frames": 100},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["frames"]) <= 8


@requires_video
def test_extract_frames_fps_mode_full_coverage() -> None:
    """fps=1 全片采样:~每秒 1 帧,时间戳间隔约 1s,不触发截断。"""
    meta = read_video_meta(VIDEO_ABS)
    assert meta is not None
    duration = float(meta["duration_sec"])
    resp = client.post(
        "/tools/extract_frames", json={"video_path": VIDEO_REL, "fps": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    frames = body["frames"]
    assert body["truncated"] is False
    assert len(frames) == pytest.approx(duration, abs=2)
    assert len(frames) >= 10
    stamps = [f["timestamp"] for f in frames]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    for gap in gaps:
        assert gap == pytest.approx(1.0, abs=1e-6)


@requires_video
def test_extract_frames_timestamps_take_priority_over_fps() -> None:
    resp = client.post(
        "/tools/extract_frames",
        json={"video_path": VIDEO_REL, "timestamps": [1.0, 2.5], "fps": 1},
    )
    assert resp.status_code == 200, resp.text
    frames = resp.json()["frames"]
    assert [f["timestamp"] for f in frames] == [1.0, 2.5]


@requires_video
def test_extract_frames_fps_mode_truncation() -> None:
    """fps 模式超过上限时截断并在响应中标记 truncated。"""
    resp = client.post(
        "/tools/extract_frames",
        json={"video_path": VIDEO_REL, "fps": 1, "max_frames": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["frames"]) == 5
    assert body["truncated"] is True
    resp = client.post(
        "/tools/extract_frames",
        json={"video_path": VIDEO_REL, "fps": 5, "max_frames": 999},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["frames"]) <= 120


@requires_video
def test_extract_frames_fps_must_be_positive() -> None:
    resp = client.post(
        "/tools/extract_frames", json={"video_path": VIDEO_REL, "fps": 0}
    )
    assert resp.status_code == 422


@requires_video
def test_draw_boxes() -> None:
    resp = client.post(
        "/tools/draw_boxes",
        json={
            "video_path": VIDEO_REL,
            "timestamp": 1.0,
            "boxes": [
                {"x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5, "label": "car"},
                {"x1": 0.6, "y1": 0.6, "x2": 0.9, "y2": 0.9},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    raw = base64.b64decode(body["jpeg_base64"])
    assert raw[:2] == b"\xff\xd8", "payload is not a JPEG"
    assert body["width"] > 0 and body["height"] > 0


def test_path_outside_workspace_returns_403() -> None:
    for video_path in ("/etc/hostname", "../outside.mp4"):
        resp = client.post("/tools/video_meta", json={"video_path": video_path})
        assert resp.status_code == 403, (video_path, resp.text)
        error = resp.json()["error"]
        assert error["code"] == "path_outside_workspace"
        assert error["message"]


def test_missing_video_returns_404() -> None:
    resp = client.post(
        "/tools/video_meta", json={"video_path": "no_such_video.mp4"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "video_not_found"


# ---------------------------------------------------------------------------
# 多允许根:POST /config/roots 热注册(web 层切换工作区时调用,免重启)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_client(tmp_path: Path) -> TestClient:
    """初始根为空 tmp 目录的独立 app(与模块级 client 互不干扰)。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return TestClient(create_app(workspace))


def test_health_lists_roots(tmp_client: TestClient, tmp_path: Path) -> None:
    body = tmp_client.get("/health").json()
    assert body["status"] == "ok"
    initial = str((tmp_path / "ws").resolve())
    assert body["workspace"] == initial  # 兼容字段:初始根
    assert body["roots"] == [initial]


def test_add_root_registers_and_dedups(
    tmp_client: TestClient, tmp_path: Path
) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    resp = tmp_client.post("/config/roots", json={"path": str(extra)})
    assert resp.status_code == 200, resp.text
    roots = resp.json()["roots"]
    assert roots == [str((tmp_path / "ws").resolve()), str(extra.resolve())]
    # 幂等:重复注册不产生重复项
    resp = tmp_client.post("/config/roots", json={"path": str(extra)})
    assert resp.json()["roots"] == roots
    assert tmp_client.get("/health").json()["roots"] == roots


def test_add_root_rejects_non_directory(tmp_client: TestClient, tmp_path: Path) -> None:
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    for bad in (str(tmp_path / "no_such_dir"), str(a_file)):
        resp = tmp_client.post("/config/roots", json={"path": bad})
        assert resp.status_code == 400, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_root"


def test_registered_root_grants_access(
    tmp_client: TestClient, tmp_path: Path
) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    inside = extra / "clip.mp4"
    inside.write_bytes(b"")  # 非视频文件:通过路径校验后才轮到 404
    resp = tmp_client.post("/tools/video_meta", json={"video_path": str(inside)})
    assert resp.status_code == 403  # 注册前:越界
    tmp_client.post("/config/roots", json={"path": str(extra)})
    resp = tmp_client.post("/tools/video_meta", json={"video_path": str(inside)})
    assert resp.status_code == 404, resp.text  # 注册后:过路径校验,元信息不可读
    assert resp.json()["error"]["code"] == "video_meta_unavailable"
    # 相对路径按允许根顺序查找
    resp = tmp_client.post("/tools/video_meta", json={"video_path": "clip.mp4"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "video_meta_unavailable"


def test_unregistered_root_still_403(
    tmp_client: TestClient, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    tmp_client.post("/config/roots", json={"path": str(other)})
    resp = tmp_client.post("/tools/video_meta", json={"video_path": "/etc/hostname"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "path_outside_workspace"


@requires_video
def test_registered_root_serves_real_video(tmp_client: TestClient) -> None:
    resp = tmp_client.post(
        "/tools/video_meta", json={"video_path": str(VIDEO_ABS)}
    )
    assert resp.status_code == 403  # 初始根不含演示区
    tmp_client.post("/config/roots", json={"path": str(WORKSPACE)})
    resp = tmp_client.post(
        "/tools/video_meta", json={"video_path": str(VIDEO_ABS)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["frame_count"] > 0


# ---------------------------------------------------------------------------
# /tools/prepare_video:大小守门,超过 max_mb 用 ffmpeg 阶梯降帧转码
# ---------------------------------------------------------------------------

_TINY_FPS = 25.0
_TINY_FRAMES = 30
_TINY_MAX_MB = 0.0005  # ~524B, tiny test clip is ~2KB -> always over the cap


@pytest.fixture()
def video_client(tmp_path: Path) -> Tuple[TestClient, Path]:
    """独立 app + tmp 根内一段 cv2 现场合成的 1.2s 小视频(转码产物落 tmp)。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    video = workspace / "clip.mp4"
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"mp4v"), _TINY_FPS, (64, 48)
    )
    assert writer.isOpened(), "cv2.VideoWriter failed to open"
    for i in range(_TINY_FRAMES):
        writer.write(np.full((48, 64, 3), i % 255, dtype=np.uint8))
    writer.release()
    return TestClient(create_app(workspace)), video


@requires_video
def test_prepare_video_passthrough() -> None:
    resp = client.post("/tools/prepare_video", json={"video_path": VIDEO_REL})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcoded"] is False
    assert body["path"] == str(VIDEO_ABS.resolve())
    assert body["size_bytes"] == VIDEO_ABS.stat().st_size
    assert body["fps"] > 0
    assert body["duration_s"] > 0


def test_prepare_video_outside_roots_403() -> None:
    resp = client.post(
        "/tools/prepare_video", json={"video_path": "/etc/hostname"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "path_outside_workspace"


def test_prepare_video_max_mb_capped_at_100() -> None:
    resp = client.post(
        "/tools/prepare_video",
        json={"video_path": "x.mp4", "max_mb": 101},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_prepare_video_ladder_success(
    video_client: Tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tc, _video = video_client
    calls: List[List[str]] = []

    def fake_run(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 1)  # 原 fps 重编码失败
        Path(cmd[-1]).write_bytes(b"x" * 100)  # 后续候选产出 < max_bytes
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(
        "traffic_analyzer.toolserver.server.subprocess.run", fake_run
    )
    resp = tc.post(
        "/tools/prepare_video",
        json={"video_path": "clip.mp4", "max_mb": _TINY_MAX_MB},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcoded"] is True
    assert body["fps"] == 12.0  # 阶梯:25 失败后命中 12
    assert body["size_bytes"] == 100
    assert body["duration_s"] == pytest.approx(_TINY_FRAMES / _TINY_FPS)
    assert body["path"].endswith(".agent/transcoded/clip_fps12.mp4")
    assert len(calls) == 2
    assert f"fps={_TINY_FPS:g}" in calls[0]
    assert "fps=12" in calls[1]


def test_prepare_video_ladder_all_fail_422(
    video_client: Tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tc, _video = video_client

    def fake_run(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(
        "traffic_analyzer.toolserver.server.subprocess.run", fake_run
    )
    resp = tc.post(
        "/tools/prepare_video",
        json={"video_path": "clip.mp4", "max_mb": _TINY_MAX_MB},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "transcode_failed"


def test_prepare_video_reuses_cached_output(
    video_client: Tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tc, video = video_client
    cached = (
        video.parent / ".agent" / "transcoded" / f"clip_fps{_TINY_FPS:g}.mp4"
    )
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x" * 100)  # 已存在且 < max_bytes:直接复用

    def fake_run(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("ffmpeg must not run when cached output fits")

    monkeypatch.setattr(
        "traffic_analyzer.toolserver.server.subprocess.run", fake_run
    )
    resp = tc.post(
        "/tools/prepare_video",
        json={"video_path": "clip.mp4", "max_mb": _TINY_MAX_MB},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcoded"] is True
    assert body["path"] == str(cached)
    assert body["fps"] == _TINY_FPS


def test_prepare_video_ffmpeg_missing_500(
    video_client: Tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tc, _video = video_client
    monkeypatch.setattr(
        "traffic_analyzer.toolserver.server.shutil.which", lambda _cmd: None
    )
    resp = tc.post(
        "/tools/prepare_video",
        json={"video_path": "clip.mp4", "max_mb": _TINY_MAX_MB},
    )
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "tool_unavailable"


@requires_small_video
@requires_ffmpeg
def test_prepare_video_real_ffmpeg_transcode(tmp_path: Path) -> None:
    """真实 ffmpeg 端到端:8.8MB 演示视频 + max_mb=2 强制走转码(1080p 下
    0.5MB 不可达,全阶梯最低档 fps=2 也有 ~1.8MB)。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = workspace / "demo.mp4"
    shutil.copyfile(SMALL_VIDEO_ABS, src)
    tc = TestClient(create_app(workspace))
    meta = read_video_meta(src)
    assert meta is not None

    resp = tc.post(
        "/tools/prepare_video", json={"video_path": "demo.mp4", "max_mb": 2}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcoded"] is True
    assert body["size_bytes"] < 2 * 1024 * 1024
    assert body["size_bytes"] == Path(body["path"]).stat().st_size
    # 产物可解码,且时长与源一致(只降帧率)
    out_meta = read_video_meta(Path(body["path"]))
    assert out_meta is not None
    assert out_meta["duration_sec"] == pytest.approx(
        meta["duration_sec"], rel=0.05
    )
    assert out_meta["fps"] == pytest.approx(body["fps"])
