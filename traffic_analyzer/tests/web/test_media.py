"""Frame extraction, video meta and streaming/transcode tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import (
    _FFMPEG,
    _MP4TEST_MPEG4,
    _FakeCapture,
    _install_fake_cv2,
    _make_stream_workspace,
    _make_tiny_video,
    _make_workspace,
    requires_ffmpeg,
    requires_mp4test,
)


@pytest.fixture(autouse=True)
def _clear_transcode_cache() -> Any:
    """Transcode LRU is module-level: wipe it (and its temp files) per test."""
    from traffic_analyzer.web.video_stream import _cleanup_transcode_cache

    _cleanup_transcode_cache()
    yield
    _cleanup_transcode_cache()


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------
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


class TestVideoMeta:
    """Meta endpoints (cv2-based) + workspace-rel frame endpoint."""

    def test_meta_real_video(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_tiny_video(workspace / "v1.mp4", frames=8)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/videos/v1/meta")
        assert resp.status_code == 200
        meta = resp.json()
        assert meta["frame_count"] == 8
        assert meta["fps"] == pytest.approx(5.0)
        assert meta["duration_sec"] == pytest.approx(1.6)
        assert meta["width"] == 64
        assert meta["height"] == 48

    def test_meta_invalid_video_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)  # v1.mp4 is an empty file
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/videos/v1/meta").status_code == 404

    def test_meta_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/videos/a..b/meta").status_code == 404
        assert client.get("/api/workspace/meta", params={"path": "../v1.mp4"}).status_code == 404
        assert client.get("/api/workspace/frame", params={"path": "../v1.mp4", "index": 0}).status_code == 404

    def test_meta_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/videos/v1/meta").status_code == 400
        assert client.get("/api/workspace/meta", params={"path": "v1.mp4"}).status_code == 400
        assert client.get("/api/workspace/frame", params={"path": "v1.mp4", "index": 0}).status_code == 400

    def test_workspace_meta_non_video_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/workspace/meta", params={"path": "notes.txt"}).status_code == 404
        assert client.get("/api/workspace/frame", params={"path": "notes.txt", "index": 0}).status_code == 404

    def test_workspace_meta_and_frame_real_video(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        sub = workspace / "sub"
        sub.mkdir()
        _make_tiny_video(sub / "nested.mp4", frames=8)
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/workspace/meta", params={"path": "sub/nested.mp4"})
        assert resp.status_code == 200
        assert resp.json()["frame_count"] == 8

        resp = client.get("/api/workspace/frame", params={"path": "sub/nested.mp4", "index": 0})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content[:2] == b"\xff\xd8"  # JPEG SOI

        # 越界帧 404
        resp = client.get("/api/workspace/frame", params={"path": "sub/nested.mp4", "index": 99})
        assert resp.status_code == 404
# ---------------------------------------------------------------------------
# Video streaming (/api/videos/{stem}/stream)
# ---------------------------------------------------------------------------
class TestVideoStream:
    def test_stream_unknown_stem_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/videos/ghost/stream").status_code == 404

    def test_stream_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/videos/v1/stream").status_code == 400

    def test_stream_ffprobe_missing_501(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("traffic_analyzer.web.video_stream._FFPROBE", None)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/videos/v1/stream")
        assert resp.status_code == 501
        assert "ffprobe" in resp.json()["detail"]

    def test_browser_native_matrix(self) -> None:
        from traffic_analyzer.web.video_stream import is_browser_native

        assert is_browser_native("h264", ".mp4")
        assert is_browser_native("h264", ".mov")
        assert is_browser_native("vp9", ".webm")
        assert is_browser_native("av1", ".mkv")
        assert not is_browser_native("hevc", ".mp4")
        assert not is_browser_native("mpeg4", ".mp4")
        assert not is_browser_native("h264", ".avi")

    def test_probe_branch_h264_serves_file_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.probe_video",
            lambda path: ("mov,mp4,m4a,3gp,3g2,mj2", "h264"),
        )
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/videos/v1/stream")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"

    def test_probe_branch_hevc_goes_to_transcode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ffmpeg removed => transcode branch is taken and reports 501.
        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.probe_video", lambda path: ("mp4", "hevc")
        )
        monkeypatch.setattr("traffic_analyzer.web.video_stream._FFMPEG", None)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/videos/v1/stream")
        assert resp.status_code == 501
        assert "ffmpeg" in resp.json()["detail"]

    def test_stream_ss_param_forwarded_to_ffmpeg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.probe_video", lambda path: ("mp4", "hevc")
        )
        captured: Dict[str, Any] = {}

        class _FakeProc:
            def __init__(self, argv: List[str]) -> None:
                # faststart 输出到临时文件(可 seek),不再走 stdout 管道。
                Path(argv[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)

            def wait(self) -> int:
                return 0

        def _fake_popen(argv: List[str], **kwargs: Any) -> Any:
            captured["argv"] = argv
            return _FakeProc(argv)

        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.subprocess.Popen", _fake_popen
        )
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/videos/v1/stream", params={"ss": 12.5})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        argv = captured["argv"]
        assert argv[argv.index("-ss") + 1] == "12.500"
        assert "+faststart" in argv
        assert argv[-2] == "mp4"
        assert argv[-1].endswith(".mp4") and argv[-1] != "-"

    @requires_ffmpeg
    def test_stream_mp4v_transcodes_to_mp4(self, tmp_path: Path) -> None:
        _make_tiny_video(tmp_path / "clip.mp4")
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.get("/api/videos/clip/stream")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        assert len(resp.content) > 0
        assert b"ftyp" in resp.content[:64]
        # faststart:moov 必须在 mdat 之前(Safari <video> 可播的前提)。
        assert resp.content.index(b"moov") < resp.content.index(b"mdat")
        # FileResponse 自带 Range 支持,转码产物同样可拖动进度。
        assert resp.headers["accept-ranges"] == "bytes"

    @requires_ffmpeg
    def test_stream_h264_range_request_206(self, tmp_path: Path) -> None:
        clip = tmp_path / "h264clip.mp4"
        subprocess.run(
            [
                _FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc=duration=1:size=64x48:rate=5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
            ],
            check=True,
        )
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.get("/api/videos/h264clip/stream", headers={"Range": "bytes=0-99"})
        assert resp.status_code == 206
        assert resp.headers["content-type"] == "video/mp4"
        assert len(resp.content) == 100
# ---------------------------------------------------------------------------
# Workspace-relative streaming (/api/workspace/stream)
# ---------------------------------------------------------------------------
class TestWorkspaceStream:
    def test_stream_full_200(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.probe_video",
            lambda path: ("mov,mp4,m4a,3gp,3g2,mj2", "h264"),
        )
        workspace = _make_stream_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/workspace/stream", params={"path": "sub/nested.mp4"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        assert resp.headers["accept-ranges"] == "bytes"
        assert len(resp.content) == 2048

    def test_stream_range_206(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.probe_video",
            lambda path: ("mov,mp4,m4a,3gp,3g2,mj2", "h264"),
        )
        workspace = _make_stream_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get(
            "/api/workspace/stream",
            params={"path": "sub/nested.mp4"},
            headers={"Range": "bytes=0-99"},
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 0-99/2048"
        assert len(resp.content) == 100

    def test_stream_path_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_stream_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get(
            "/api/workspace/stream", params={"path": "../../etc/passwd"}
        ).status_code == 404
        assert client.get(
            "/api/workspace/stream", params={"path": "sub/../../outside.mp4"}
        ).status_code == 404

    def test_stream_symlink_escape_404(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside_stream_test"
        outside.mkdir(exist_ok=True)
        try:
            (outside / "secret.mp4").write_bytes(b"\x00" * 8)
            workspace = _make_stream_workspace(tmp_path)
            (workspace / "sub" / "link.mp4").symlink_to(outside / "secret.mp4")
            client = TestClient(create_app(workspace=str(workspace)))
            assert client.get(
                "/api/workspace/stream", params={"path": "sub/link.mp4"}
            ).status_code == 404
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_stream_non_video_404(self, tmp_path: Path) -> None:
        workspace = _make_stream_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get(
            "/api/workspace/stream", params={"path": "sub/notes.txt"}
        ).status_code == 404

    def test_stream_missing_file_404(self, tmp_path: Path) -> None:
        workspace = _make_stream_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get(
            "/api/workspace/stream", params={"path": "sub/ghost.mp4"}
        ).status_code == 404

    def test_stream_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.get(
            "/api/workspace/stream", params={"path": "sub/nested.mp4"}
        ).status_code == 400
# ---------------------------------------------------------------------------
# F2: frame cache keyed by mtime
# ---------------------------------------------------------------------------


class TestFrameCacheMtime:
    def test_replaced_video_not_served_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cv2(monkeypatch)
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/videos/v1/frame", params={"index": 3})
        assert resp.status_code == 200
        assert _FakeCapture.instances == 1

        # Same path, new content with a bumped mtime: stale entry must miss.
        video = workspace / "v1.mp4"
        video.write_bytes(b"replaced")
        future = time.time() + 10
        os.utime(video, (future, future))

        resp = client.get("/api/videos/v1/frame", params={"index": 3})
        assert resp.status_code == 200
        assert _FakeCapture.instances == 2

        # Unchanged mtime keeps hitting the cache.
        client.get("/api/videos/v1/frame", params={"index": 3})
        assert _FakeCapture.instances == 2
# ---------------------------------------------------------------------------
# F1: faststart transcode robustness + LRU cache
# ---------------------------------------------------------------------------
class TestTranscodeRobustness:
    @requires_ffmpeg
    def test_garbage_input_501(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from traffic_analyzer.web import video_stream

        procs: List[Any] = []
        real_popen = subprocess.Popen

        def _spy_popen(argv: List[str], **kwargs: Any) -> Any:
            proc = real_popen(argv, **kwargs)
            procs.append(proc)
            return proc

        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.subprocess.Popen", _spy_popen
        )
        garbage = tmp_path / "garbage.mp4"
        garbage.write_bytes(b"not a real video" * 100)
        with pytest.raises(HTTPException) as exc_info:
            video_stream._transcode_faststart(garbage, None)
        assert exc_info.value.status_code == 501
        # The dead ffmpeg was reaped: every spawned proc is gone.
        assert procs, "transcode should have spawned ffmpeg"
        assert all(proc.poll() is not None for proc in procs)
        # 失败产物不入缓存,临时文件已删。
        assert len(video_stream._transcode_cache) == 0

    @requires_ffmpeg
    def test_moov_before_mdat(self, tmp_path: Path) -> None:
        from traffic_analyzer.web.video_stream import _transcode_faststart

        _make_tiny_video(tmp_path / "clip.mp4")
        out = _transcode_faststart(tmp_path / "clip.mp4", None)
        head = out.read_bytes()[:64]
        assert b"ftyp" in head
        data = out.read_bytes()
        assert data.index(b"moov") < data.index(b"mdat")

    @requires_ffmpeg
    def test_lru_hit_skips_retranscode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from traffic_analyzer.web import video_stream

        calls: List[List[str]] = []
        real_popen = subprocess.Popen

        def _spy_popen(argv: List[str], **kwargs: Any) -> Any:
            calls.append(argv)
            return real_popen(argv, **kwargs)

        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.subprocess.Popen", _spy_popen
        )
        _make_tiny_video(tmp_path / "clip.mp4")
        first = video_stream._transcode_faststart(tmp_path / "clip.mp4", None)
        second = video_stream._transcode_faststart(tmp_path / "clip.mp4", None)
        assert first == second
        assert len(calls) == 1  # 命中缓存,不重转

    @requires_ffmpeg
    def test_lru_evicts_oldest_beyond_three(self, tmp_path: Path) -> None:
        from traffic_analyzer.web import video_stream

        outs: List[Path] = []
        for i in range(4):
            src = tmp_path / f"clip{i}.mp4"
            _make_tiny_video(src)
            outs.append(video_stream._transcode_faststart(src, None))
        assert len(video_stream._transcode_cache) == 3
        assert outs[0].exists()  # 已淘汰但仍在途:转 pending,不立即删
        for out in outs:  # 直接调用的调用方负责归还在途引用
            video_stream._transcode_release(out)
        assert not outs[0].exists()  # 引用归零后补删
        assert all(p.exists() for p in outs[1:])

    def test_semaphore_full_503(self, tmp_path: Path) -> None:
        from traffic_analyzer.web import video_stream

        src = tmp_path / "clip.mp4"
        src.write_bytes(b"whatever")
        for _ in range(video_stream._MAX_TRANSCODES):
            assert video_stream._transcode_slots.acquire(blocking=False)
        try:
            with pytest.raises(HTTPException) as exc_info:
                video_stream._transcode_faststart(src, None)
            assert exc_info.value.status_code == 503
        finally:
            for _ in range(video_stream._MAX_TRANSCODES):
                video_stream._transcode_slots.release()

    @requires_ffmpeg
    @requires_mp4test
    def test_nostdin_and_faststart_in_ffmpeg_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from traffic_analyzer.web import video_stream

        captured: Dict[str, Any] = {}
        real_popen = subprocess.Popen

        def _spy_popen(argv: List[str], **kwargs: Any) -> Any:
            captured["argv"] = argv
            return real_popen(argv, **kwargs)

        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.subprocess.Popen", _spy_popen
        )
        out = video_stream._transcode_faststart(_MP4TEST_MPEG4, None)
        assert out.exists()
        argv = captured["argv"]
        assert "-nostdin" in argv
        assert "+faststart" in argv
# ---------------------------------------------------------------------------
# 任务B:转码 LRU 在途引用保护(淘汰不得删除响应尚未发完的临时文件)
# ---------------------------------------------------------------------------
class TestTranscodeInflight:
    def test_eviction_defers_unlink_until_release(self, tmp_path: Path) -> None:
        from traffic_analyzer.web import video_stream

        # put 即登记一次在途引用;上限 3,第 4 次 put 触发 LRU 淘汰。
        first = tmp_path / "t0.mp4"
        first.write_bytes(b"x")
        video_stream._transcode_cache_put(("a", 1.0, None), first)
        rest = []
        for i in range(3):
            p = tmp_path / f"t{i + 1}.mp4"
            p.write_bytes(b"x")
            video_stream._transcode_cache_put((f"b{i}", 1.0, None), p)
            rest.append(p)
        assert len(video_stream._transcode_cache) == 3
        assert first.exists()  # 在途引用未归还:淘汰只入 pending,不删文件
        video_stream._transcode_release(first)
        assert not first.exists()  # 引用归零后补删
        for p in rest:
            video_stream._transcode_release(p)
            assert p.exists()  # 未被淘汰的项归还后仍在
        assert not video_stream._inflight and not video_stream._pending_delete

    def test_concurrent_same_video_both_playable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from traffic_analyzer.web import video_stream

        # hevc 走转码分支;假 ffmpeg 瞬间产出,双 miss 时两请求各自转码,
        # 后到的 put 会把先到的 tmp 当 previous 淘汰 —— 修复前会立刻 unlink。
        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.probe_video", lambda path: ("mp4", "hevc")
        )
        monkeypatch.setattr("traffic_analyzer.web.video_stream._FFMPEG", "ffmpeg")
        payload = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64

        class _FakeProc:
            def __init__(self, argv: List[str]) -> None:
                Path(argv[-1]).write_bytes(payload)

            def wait(self) -> int:
                return 0

        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.subprocess.Popen",
            lambda argv, **kwargs: _FakeProc(argv),
        )
        workspace = _make_workspace(tmp_path)
        responses: Dict[int, Any] = {}

        def _request(i: int) -> None:  # 每线程自建 TestClient(portal 绑定本线程)
            responses[i] = TestClient(create_app(workspace=str(workspace))).get(
                "/api/videos/v1/stream"
            )

        threads = [threading.Thread(target=_request, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(responses) == 2
        for resp in responses.values():
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "video/mp4"
            assert resp.content == payload
        # 两个响应都发完后,在途引用全部归还,pending 补删也应清空。
        assert not video_stream._inflight and not video_stream._pending_delete
