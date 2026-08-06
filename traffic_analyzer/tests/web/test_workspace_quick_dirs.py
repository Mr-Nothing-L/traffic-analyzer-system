"""GET /api/workspace/quick-dirs tests(白名单根及一层子目录对前端的暴露)。

写法参照 test_workspace_fs.py 的 TestWorkspaceAllowlist:monkeypatch.setenv
注入 TRAFFIC_ANALYZER_WORKSPACE_DIRS;
未配置场景依赖 conftest 的 _isolate_env_file(delenv + 隔离 config/.env)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app


class TestQuickDirs:
    def test_two_roots_with_sorted_subs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两个根各有子目录:返回正确且 subs 按名字排序(文件不返回)。"""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "Beta").mkdir()
        (a / "alpha").mkdir()
        (a / "note.txt").write_text("x", encoding="utf-8")
        (a / ".hidden").mkdir()
        (b / "sub").mkdir()
        monkeypatch.setenv("TRAFFIC_ANALYZER_WORKSPACE_DIRS", f"{a},{b}")
        client = TestClient(create_app())
        resp = client.get("/api/workspace/quick-dirs")
        assert resp.status_code == 200
        assert resp.json() == {
            "roots": [
                {"path": str(a.resolve()), "subs": ["alpha", "Beta"]},
                {"path": str(b.resolve()), "subs": ["sub"]},
            ]
        }

    def test_missing_root_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一根路径不存在:跳过该根,另一根正常返回。"""
        ok = tmp_path / "ok"
        ok.mkdir()
        (ok / "child").mkdir()
        missing = tmp_path / "missing"
        monkeypatch.setenv(
            "TRAFFIC_ANALYZER_WORKSPACE_DIRS", f"{missing},{ok}"
        )
        client = TestClient(create_app())
        assert client.get("/api/workspace/quick-dirs").json() == {
            "roots": [{"path": str(ok.resolve()), "subs": ["child"]}]
        }

    def test_unset_returns_empty(self) -> None:
        """未配置白名单(conftest 已 delenv + 隔离 config/.env):空 roots。"""
        client = TestClient(create_app())
        assert client.get("/api/workspace/quick-dirs").json() == {"roots": []}
