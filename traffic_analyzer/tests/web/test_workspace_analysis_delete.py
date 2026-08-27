"""分析报告删除路由测试(DELETE /api/workspace/analysis/{stem} 与批量 POST
/api/workspace/analysis/delete,见 web/workspace/videos.py)。

写法参照 test_workspace_fs.py:create_app(workspace=preset)+ conftest 的
_make_workspace/_make_results;缓存行为依赖 videos._videos_cache 的长 TTL,
删除后徽标重算即验证 invalidate_caches 生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _make_results, _make_tree_workspace, _make_workspace


def _has_results(client: TestClient, rel: str) -> bool:
    video = next(v for v in client.get("/api/workspace/videos").json() if v["rel"] == rel)
    return video["has_results"]


class TestDeleteAnalysisSingle:
    def test_deletes_whole_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        out_dir = _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.delete("/api/workspace/analysis/v1")
        assert resp.status_code == 200
        assert resp.json() == {"stem": "v1", "ok": True, "existed": True}
        assert not out_dir.exists()

    def test_status_flag_follows_disk(self, tmp_path: Path) -> None:
        """删除后 has_results(已完成徽标的依据)随之消失:验证缓存同步失效。"""
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        _make_results(workspace, "v2")
        client = TestClient(create_app(workspace=str(workspace)))
        # 先读一次填满长 TTL 缓存,再删 v2:缓存必须被主动失效而非等 TTL 过期。
        assert _has_results(client, "v2.avi") is True
        assert client.delete("/api/workspace/analysis/v2").status_code == 200
        assert _has_results(client, "v2.avi") is False
        assert _has_results(client, "v1.mp4") is True  # 其余视频不受影响

    def test_missing_stem_idempotent(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.delete("/api/workspace/analysis/v1")
        assert resp.status_code == 200
        assert resp.json() == {"stem": "v1", "ok": True, "existed": False}
        # 幂等语义:不得凭空创建 analysis/<stem>/ 目录。
        assert not (workspace / "analysis").exists()

    def test_traversal_stems_rejected(self, tmp_path: Path) -> None:
        """越界 stem(..、反斜杠等经 validate_stem 白名单校验)一律 404。

        「/」分隔符场景见批量用例(URL 路径里的 %2F 会被客户端归一化,
        无法作为单删路径参数传达)。
        """
        workspace = _make_workspace(tmp_path)
        out_dir = _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        for stem in ("a..b", "%2E%2E", "a%5Cb"):
            resp = client.delete(f"/api/workspace/analysis/{stem}")
            assert resp.status_code == 404, stem
            assert resp.json()["detail"] == "Unknown video stem"
        # 非法请求不落盘。
        assert out_dir.exists()

    def test_requires_workspace(self) -> None:
        client = TestClient(create_app())
        assert client.delete("/api/workspace/analysis/v1").status_code == 400


class TestDeleteAnalysisBatch:
    def test_batch_mixed_items(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        out_v1 = _make_results(workspace, "v1")
        out_v2 = _make_results(workspace, "v2")
        (out_v2 / "quarantine").mkdir()
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.post(
            "/api/workspace/analysis/delete",
            json={"stems": ["v1", "nope", "a/b", "v2"]},
        )
        assert resp.status_code == 200
        assert resp.json() == [
            {"stem": "v1", "ok": True, "existed": True},
            {"stem": "nope", "ok": True, "existed": False},
            {"stem": "a/b", "ok": False, "existed": False},  # 越界项拒绝但不中断批次
            {"stem": "v2", "ok": True, "existed": True},
        ]
        assert not out_v1.exists()
        assert not out_v2.exists()
        # 视频列表缓存同步失效:两个「已完成」徽标一并消失。
        videos = {v["rel"]: v["has_results"] for v in client.get("/api/workspace/videos").json()}
        assert videos == {"v1.mp4": False, "v2.avi": False}

    def test_batch_empty_list(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(_make_workspace(tmp_path))))
        resp = client.post("/api/workspace/analysis/delete", json={"stems": []})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_nested_video_report_deleted(self, tmp_path: Path) -> None:
        """嵌套视频按扁平 analysis/<stem>/ 契约删同名报告目录。"""
        workspace = _make_tree_workspace(tmp_path)
        out_dir = _make_results(workspace, "nested")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.post("/api/workspace/analysis/delete", json={"stems": ["nested"]})
        assert resp.status_code == 200
        assert resp.json() == [{"stem": "nested", "ok": True, "existed": True}]
        assert not out_dir.exists()

    def test_requires_workspace(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace/analysis/delete", json={"stems": ["v1"]})
        assert resp.status_code == 400
