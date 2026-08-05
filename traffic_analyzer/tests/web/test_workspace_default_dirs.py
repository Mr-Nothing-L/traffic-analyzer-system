"""GET /api/workspace/default-dirs tests(白名单解析结果对前端的暴露)。

写法参照 test_workspace_fs.py 的 TestWorkspaceAllowlist:monkeypatch.setenv
注入 TRAFFIC_ANALYZER_WORKSPACE_DIRS;未配置场景依赖 conftest 的
_isolate_env_file(delenv + 隔离 config/.env)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app


class TestDefaultDirs:
    def test_configured_returns_resolved_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已配置白名单:返回解析后的绝对路径列表(逗号分隔、去空白)。"""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("TRAFFIC_ANALYZER_WORKSPACE_DIRS", f" {a} , {b} ,")
        client = TestClient(create_app())
        resp = client.get("/api/workspace/default-dirs")
        assert resp.status_code == 200
        assert resp.json() == {"dirs": [str(a.resolve()), str(b.resolve())]}

    def test_unset_returns_empty(self) -> None:
        """未配置白名单(conftest 已 delenv + 隔离 config/.env):空数组。"""
        client = TestClient(create_app())
        assert client.get("/api/workspace/default-dirs").json() == {"dirs": []}

    def test_tilde_expands_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """白名单支持 ~:展开并 resolve 为用户主目录。"""
        monkeypatch.setenv("TRAFFIC_ANALYZER_WORKSPACE_DIRS", "~")
        client = TestClient(create_app())
        assert client.get("/api/workspace/default-dirs").json() == {
            "dirs": [str(Path.home().resolve())]
        }
