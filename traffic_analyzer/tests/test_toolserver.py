"""Unit tests for the toolserver /tools/* endpoints.

[文件说明]
作用:用 FastAPI TestClient + 演示区真实视频验证 video_meta、
    extract_frames(count=2)、draw_boxes 与 workspace 越界 403 / 404 契约。
上游:pytest 自动发现并执行本文件测试;视频缺失时相关用例 pytest.skip。
下游:traffic_analyzer/toolserver(被测模块)。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.toolserver import create_app

WORKSPACE = Path(__file__).resolve().parents[2]
VIDEO_REL = "演示区/01-02_Event_129_1755579215119_1.mp4"
VIDEO_ABS = WORKSPACE / VIDEO_REL

client = TestClient(create_app(WORKSPACE))

requires_video = pytest.mark.skipif(
    not VIDEO_ABS.is_file(), reason=f"demo video missing: {VIDEO_ABS}"
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
