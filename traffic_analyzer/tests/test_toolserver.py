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
