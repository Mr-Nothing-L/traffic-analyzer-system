"""Tests for the agent chat upload endpoints (traffic_analyzer.web.agentproxy.uploads).

POST /api/agent/uploads 把对话里粘贴/选择的视频/图片落盘到
<workspace>/.agent/uploads/(文件名清洗防路径穿越、MIME 限定 video/* 与
image/*、大小上限 AGENT_UPLOAD_MAX_MB 可覆盖);GET /api/agent/uploads/{name}
流式返回(FileResponse 自带 Range)。全部本地文件操作,不起任何子进程/服务。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web import app as app_mod
from traffic_analyzer.web.agentproxy import uploads as uploads_mod


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
    monkeypatch.delenv(uploads_mod.MAX_UPLOAD_MB_ENV_VAR, raising=False)
    app = app_mod.create_app(workspace=str(workspace))
    return TestClient(app)


def _upload(client: TestClient, name: str, data: bytes, content_type: str) -> Any:
    return client.post(
        "/api/agent/uploads",
        files={"file": (name, data, content_type)},
    )


# ---------------------------------------------------------------------------
# POST /api/agent/uploads
# ---------------------------------------------------------------------------


class TestUpload:
    def test_upload_success(self, client: TestClient, workspace: Path) -> None:
        payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 100
        resp = _upload(client, "clip.mp4", payload, "video/mp4")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "clip.mp4"
        assert body["size"] == len(payload)
        assert body["contentType"] == "video/mp4"
        saved = Path(body["path"])
        assert saved.is_file()
        assert saved.read_bytes() == payload
        # 落盘位置:<workspace>/.agent/uploads/<yyyyMMdd_HHmmss>_clip.mp4
        assert saved.parent == (workspace / ".agent" / "uploads").resolve()
        assert re.fullmatch(r"\d{8}_\d{6}_clip\.mp4", saved.name)

    def test_upload_image_allowed(self, client: TestClient) -> None:
        resp = _upload(client, "frame.png", b"\x89PNG\r\n\x1a\n", "image/png")
        assert resp.status_code == 200
        assert resp.json()["contentType"] == "image/png"

    def test_mime_rejected(self, client: TestClient) -> None:
        resp = _upload(client, "notes.txt", b"hello", "text/plain")
        assert resp.status_code == 415
        assert resp.json()["error"]["code"] == "unsupported_media_type"
        # 被拒时不产生任何落盘文件
        assert list(Path(client.app.state.workspace.get()).rglob("notes*")) == []

    def test_size_limit(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(uploads_mod.MAX_UPLOAD_MB_ENV_VAR, "0.001")  # ≈1048 字节
        resp = _upload(client, "big.mp4", b"x" * 2048, "video/mp4")
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "file_too_large"
        # 超限的残文件必须删除
        uploads_dir = Path(client.app.state.workspace.get()) / ".agent" / "uploads"
        assert list(uploads_dir.glob("*big*")) == []

    def test_filename_sanitized(self, client: TestClient, workspace: Path) -> None:
        # 路径成分被剥掉,空格等非法字符折叠为 _;文件仍在 uploads 目录内。
        resp = _upload(client, "../evil dir/my clip.mp4", b"v", "video/mp4")
        assert resp.status_code == 200
        saved = Path(resp.json()["path"])
        assert saved.parent == (workspace / ".agent" / "uploads").resolve()
        assert re.fullmatch(r"\d{8}_\d{6}_my_clip\.mp4", saved.name)
        # 响应的 name 保留原始文件名
        assert resp.json()["name"] == "../evil dir/my clip.mp4"

    def test_no_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(app_mod.WORKSPACE_ENV_VAR, raising=False)
        app = app_mod.create_app()
        resp = TestClient(app).post(
            "/api/agent/uploads",
            files={"file": ("a.mp4", b"v", "video/mp4")},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_workspace"


# ---------------------------------------------------------------------------
# GET /api/agent/uploads/{name}
# ---------------------------------------------------------------------------


@pytest.fixture()
def uploaded(client: TestClient) -> str:
    """上传一个小文件,返回落盘后的文件名(含时间戳前缀)。"""
    payload = b"0123456789abcdef"
    resp = _upload(client, "clip.mp4", payload, "video/mp4")
    assert resp.status_code == 200
    return Path(resp.json()["path"]).name


class TestGetUpload:
    def test_full_download(self, client: TestClient, uploaded: str) -> None:
        resp = client.get(f"/api/agent/uploads/{uploaded}")
        assert resp.status_code == 200
        assert resp.content == b"0123456789abcdef"
        assert resp.headers["content-type"].startswith("video/mp4")

    def test_range_request(self, client: TestClient, uploaded: str) -> None:
        resp = client.get(
            f"/api/agent/uploads/{uploaded}", headers={"Range": "bytes=4-9"}
        )
        assert resp.status_code == 206
        assert resp.content == b"456789"
        assert resp.headers["content-range"] == "bytes 4-9/16"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/agent/uploads/20990101_000000_ghost.mp4")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "upload_not_found"

    def test_path_traversal_forbidden(self, client: TestClient, uploaded: str) -> None:
        # 解码后为 ".."(httpx 会规范化裸 "..",故用 %2E%2E):命中路由,必须 403。
        resp = client.get("/api/agent/uploads/%2E%2E")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_workspace_escape_forbidden(
        self, client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # uploads 目录内的符号链接指向工作区外 → 越出 uploads 目录,403。
        outside = workspace.parent / "secret.mp4"
        outside.write_bytes(b"secret")
        uploads_dir = workspace / ".agent" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        (uploads_dir / "link.mp4").symlink_to(outside)
        resp = client.get("/api/agent/uploads/link.mp4")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"
