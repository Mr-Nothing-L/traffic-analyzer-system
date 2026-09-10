"""keyframes 端点的嵌套目录视频(rel 参数)回归测试(P2-6)。

嵌套视频此前经 find_video 只试顶层扩展名,候选帧/加帧/智能挑选一律 404;
rel 参数统一走 workspace 受限解析(同 /api/infer)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _install_fake_cv2


@pytest.fixture()
def nested_workspace(tmp_path: Path) -> Path:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.mp4").write_bytes(b"")
    return tmp_path


class TestKeyframesRelParam:
    def test_candidates_with_rel(self, nested_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_cv2(monkeypatch)
        client = TestClient(create_app(workspace=str(nested_workspace)))
        # 不带 rel:顶层找不到 → 404(旧行为)
        resp = client.get("/api/videos/nested/keyframes/candidates")
        assert resp.status_code == 404
        # 带 rel:workspace 受限解析成功
        resp = client.get(
            "/api/videos/nested/keyframes/candidates", params={"rel": "sub/nested.mp4"}
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json(), list) and resp.json()

    def test_candidates_rel_traversal_rejected(self, nested_workspace: Path) -> None:
        client = TestClient(create_app(workspace=str(nested_workspace)))
        resp = client.get(
            "/api/videos/nested/keyframes/candidates", params={"rel": "../outside.mp4"}
        )
        assert resp.status_code == 404

    def test_candidates_rel_non_video_rejected(self, nested_workspace: Path) -> None:
        (nested_workspace / "sub" / "notes.txt").write_text("x", encoding="utf-8")
        client = TestClient(create_app(workspace=str(nested_workspace)))
        resp = client.get(
            "/api/videos/nested/keyframes/candidates", params={"rel": "sub/notes.txt"}
        )
        assert resp.status_code == 404
