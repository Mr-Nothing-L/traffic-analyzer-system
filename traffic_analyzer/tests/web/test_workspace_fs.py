"""Workspace / workspace-tree / filesystem-listing endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _make_results, _make_tree, _make_tree_workspace, _make_workspace


# ---------------------------------------------------------------------------
# Workspace endpoints
# ---------------------------------------------------------------------------


class TestWorkspace:
    def test_get_workspace_empty(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/workspace").json() == {"path": None}

    def test_set_and_get_workspace(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(tmp_path)})
        assert resp.status_code == 200
        assert resp.json() == {"path": str(tmp_path)}
        assert client.get("/api/workspace").json() == {"path": str(tmp_path)}

    def test_set_workspace_missing_path(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(tmp_path / "nope")})
        assert resp.status_code == 400

    def test_set_workspace_not_a_directory(self, tmp_path: Path) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(file_path)})
        assert resp.status_code == 400

    def test_preset_workspace_via_factory(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        assert client.get("/api/workspace").json() == {"path": str(tmp_path)}

    def test_list_videos_requires_workspace(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/workspace/videos").status_code == 400

    def test_list_videos(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))

        videos = client.get("/api/workspace/videos").json()
        by_name = {v["name"]: v for v in videos}
        assert set(by_name) == {"v1.mp4", "v2.avi"}

        v1 = by_name["v1.mp4"]
        assert v1["stem"] == "v1"
        assert v1["rel"] == "v1.mp4"
        assert v1["size"] == 0
        assert isinstance(v1["mtime"], float)
        assert v1["has_results"] is True
        assert by_name["v2.avi"]["has_results"] is False

    def test_list_videos_recursive(self, tmp_path: Path) -> None:
        """Nested videos are listed with their workspace-relative path."""
        workspace = _make_tree_workspace(tmp_path)
        _make_results(workspace, "nested")
        client = TestClient(create_app(workspace=str(workspace)))

        videos = client.get("/api/workspace/videos").json()
        by_rel = {v["rel"]: v for v in videos}
        assert set(by_rel) == {"sub/nested.mp4", "v1.mp4", "v2.avi"}
        nested = by_rel["sub/nested.mp4"]
        assert nested["name"] == "nested.mp4"
        assert nested["stem"] == "nested"
        assert nested["has_results"] is True
        # Sorted by rel.
        assert [v["rel"] for v in videos] == sorted(by_rel)
# ---------------------------------------------------------------------------
# Workspace file tree
# ---------------------------------------------------------------------------
class TestWorkspaceTree:
    def test_tree_requires_workspace(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/workspace/tree").status_code == 400

    def test_tree_root_listing(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))

        data = client.get("/api/workspace/tree").json()
        assert data["path"] == ""
        # Dirs first, then files, case-insensitive; dotfiles skipped.
        # (analysis/ comes from _make_results.)
        assert [e["name"] for e in data["entries"]] == [
            "analysis", "sub", "notes.txt", "v1.mp4", "v2.avi"
        ]
        by_name = {e["name"]: e for e in data["entries"]}
        assert by_name["sub"]["type"] == "dir"
        assert by_name["sub"]["rel"] == "sub"
        assert by_name["notes.txt"]["is_video"] is False
        assert by_name["v1.mp4"]["is_video"] is True
        assert by_name["v1.mp4"]["stem"] == "v1"
        assert by_name["v1.mp4"]["has_results"] is True
        assert by_name["v2.avi"]["has_results"] is False
        assert isinstance(by_name["v1.mp4"]["size"], int)
        assert isinstance(by_name["v1.mp4"]["mtime"], float)

    def test_tree_nested_listing(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        data = client.get("/api/workspace/tree", params={"path": "sub"}).json()
        assert data["path"] == "sub"
        assert [e["name"] for e in data["entries"]] == ["nested.mp4", "readme.md"]
        nested = data["entries"][0]
        assert nested["rel"] == "sub/nested.mp4"
        assert nested["is_video"] is True
        assert nested["stem"] == "nested"
        assert nested["has_results"] is False

    def test_tree_nested_has_results(self, tmp_path: Path) -> None:
        """Nested videos report has_results via the flat analysis/<stem>/ contract."""
        workspace = _make_tree_workspace(tmp_path)
        _make_results(workspace, "nested")
        client = TestClient(create_app(workspace=str(workspace)))

        data = client.get("/api/workspace/tree", params={"path": "sub"}).json()
        nested = data["entries"][0]
        assert nested["stem"] == "nested"
        assert nested["has_results"] is True

    def test_tree_path_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/workspace/tree", params={"path": ".."}).status_code == 404
        assert client.get("/api/workspace/tree", params={"path": "../.."}).status_code == 404
        assert client.get("/api/workspace/tree", params={"path": "sub/../../etc"}).status_code == 404

    def test_tree_symlink_escape_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        outside = tmp_path.parent / "outside_tree_test"
        outside.mkdir(exist_ok=True)
        (workspace / "link").symlink_to(outside)
        client = TestClient(create_app(workspace=str(workspace)))
        # The symlink itself is listed as a dir but cannot be opened.
        assert client.get("/api/workspace/tree", params={"path": "link"}).status_code == 404

    def test_tree_file_path_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/workspace/tree", params={"path": "notes.txt"}).status_code == 404
        assert client.get("/api/workspace/tree", params={"path": "nope"}).status_code == 404
# ---------------------------------------------------------------------------
# Filesystem listing (in-page directory navigator)
# ---------------------------------------------------------------------------
class TestFsList:
    def test_list_dirs_only_sorted(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        client = TestClient(create_app())
        resp = client.get("/api/fs/list", params={"path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == str(tmp_path.resolve())
        assert data["parent"] == str(tmp_path.resolve().parent)
        # Files and dot-dirs are excluded; sorting is case-insensitive.
        assert [d["name"] for d in data["dirs"]] == ["Alpha", "beta"]
        assert [d["path"] for d in data["dirs"]] == [
            str(tmp_path.resolve() / "Alpha"),
            str(tmp_path.resolve() / "beta"),
        ]

    def test_hidden_dirs_included_with_flag(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        client = TestClient(create_app())
        default = client.get("/api/fs/list", params={"path": str(tmp_path)}).json()["dirs"]
        assert [d["name"] for d in default] == ["Alpha", "beta"]
        shown = client.get(
            "/api/fs/list", params={"path": str(tmp_path), "hidden": 1}
        ).json()["dirs"]
        assert [d["name"] for d in shown] == [".hidden", "Alpha", "beta"]

    def test_tilde_expands_to_home(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/fs/list", params={"path": "~"})
        assert resp.status_code == 200
        assert resp.json()["path"] == str(Path.home().resolve())

    def test_relative_path_400(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/fs/list", params={"path": "some/relative/dir"})
        assert resp.status_code == 400

    def test_nonexistent_dir_404(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/fs/list", params={"path": str(tmp_path / "nope")})
        assert resp.status_code == 404

    def test_plain_file_404(self, tmp_path: Path) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        client = TestClient(create_app())
        resp = client.get("/api/fs/list", params={"path": str(file_path)})
        assert resp.status_code == 404

    def test_symlinked_dir_resolves(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        (real / "sub").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        client = TestClient(create_app())
        data = client.get("/api/fs/list", params={"path": str(link)}).json()
        assert data["path"] == str(real.resolve())
        assert data["parent"] == str(real.resolve().parent)
        assert [d["name"] for d in data["dirs"]] == ["sub"]

    def test_permission_denied_subdir_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "ok").mkdir()
        (tmp_path / "denied").mkdir()
        real_is_dir = Path.is_dir

        def _fake_is_dir(self: Path) -> bool:
            if self.name == "denied":
                raise PermissionError("mock: access denied")
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", _fake_is_dir)
        client = TestClient(create_app())
        data = client.get("/api/fs/list", params={"path": str(tmp_path)}).json()
        assert [d["name"] for d in data["dirs"]] == ["ok"]

    def test_default_no_workspace_uses_home(self) -> None:
        client = TestClient(create_app())
        data = client.get("/api/fs/list").json()
        assert data["path"] == str(Path.home().resolve())

    def test_default_uses_current_workspace(self, tmp_path: Path) -> None:
        client = TestClient(create_app(workspace=str(tmp_path)))
        data = client.get("/api/fs/list").json()
        assert data["path"] == str(tmp_path.resolve())

    def test_root_has_no_parent(self) -> None:
        client = TestClient(create_app())
        data = client.get("/api/fs/list", params={"path": "/"}).json()
        assert data["path"] == "/"
        assert data["parent"] is None


# ---------------------------------------------------------------------------
# Workspace allowlist (TRAFFIC_ANALYZER_WORKSPACE_DIRS)
# ---------------------------------------------------------------------------


class TestWorkspaceAllowlist:
    @pytest.fixture()
    def allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "allowed"
        (root / "sub").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        monkeypatch.setenv("TRAFFIC_ANALYZER_WORKSPACE_DIRS", str(root))
        return root

    def test_set_workspace_allowed_and_subpath(
        self, tmp_path: Path, allowed: Path
    ) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(allowed)})
        assert resp.status_code == 200
        resp = client.post("/api/workspace", json={"path": str(allowed / "sub")})
        assert resp.status_code == 200
        assert resp.json() == {"path": str((allowed / "sub").resolve())}

    def test_set_workspace_outside_403(self, tmp_path: Path, allowed: Path) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/workspace", json={"path": str(tmp_path / "outside")})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "workspace not in allowed list"}
        # 名单的父目录同样越界。
        resp = client.post("/api/workspace", json={"path": str(tmp_path)})
        assert resp.status_code == 403

    def test_fs_list_confined_to_allowed(self, tmp_path: Path, allowed: Path) -> None:
        client = TestClient(create_app())
        assert client.get("/api/fs/list", params={"path": str(allowed)}).status_code == 200
        assert (
            client.get("/api/fs/list", params={"path": str(allowed / "sub")}).status_code
            == 200
        )
        resp = client.get("/api/fs/list", params={"path": str(tmp_path / "outside")})
        assert resp.status_code == 403
        assert client.get("/api/fs/list", params={"path": "/"}).status_code == 403

    def test_fs_list_default_is_first_allowed(
        self, tmp_path: Path, allowed: Path
    ) -> None:
        client = TestClient(create_app())
        data = client.get("/api/fs/list").json()
        assert data["path"] == str(allowed.resolve())

    def test_comma_separated_and_tilde(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from traffic_analyzer.web import workspace as workspace_mod

        a = tmp_path / "a"
        b = tmp_path / "b"
        monkeypatch.setenv(
            "TRAFFIC_ANALYZER_WORKSPACE_DIRS", f" {a} ,~/,{b} ,"
        )
        dirs = workspace_mod.allowed_workspace_dirs()
        assert dirs == [a.resolve(), Path.home().resolve(), b.resolve()]

    def test_unset_means_unrestricted(self, tmp_path: Path) -> None:
        # conftest 已 delenv;未配置时维持现状(任意目录可用)。
        client = TestClient(create_app())
        assert client.post("/api/workspace", json={"path": str(tmp_path)}).status_code == 200
        assert client.get("/api/fs/list", params={"path": str(tmp_path)}).status_code == 200

    def test_config_env_file_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.environ 没有时兜底读 config/.env(_CONFIG_ENV_PATH)。"""
        from traffic_analyzer.web import workspace as workspace_mod

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        env_file = tmp_path / "config.env"
        env_file.write_text(
            f"TRAFFIC_ANALYZER_WORKSPACE_DIRS={allowed}\n", encoding="utf-8"
        )
        monkeypatch.setattr(workspace_mod, "_CONFIG_ENV_PATH", env_file)
        monkeypatch.delenv("TRAFFIC_ANALYZER_WORKSPACE_DIRS", raising=False)
        client = TestClient(create_app())
        assert client.post("/api/workspace", json={"path": str(tmp_path)}).status_code == 403
        assert client.post("/api/workspace", json={"path": str(allowed)}).status_code == 200
