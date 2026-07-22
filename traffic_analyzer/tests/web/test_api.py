"""Tests for the web UI backend (traffic_analyzer.web) and the web CLI."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from traffic_analyzer.cli import build_parser, main
from traffic_analyzer.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Workspace with two (empty) video files."""
    (tmp_path / "v1.mp4").write_bytes(b"")
    (tmp_path / "v2.avi").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("not a video", encoding="utf-8")
    return tmp_path


def _make_results(workspace: Path, stem: str = "v1") -> Path:
    """Fabricate analysis/<stem>/ with report, SFT sample, evidence, images."""
    out_dir = workspace / "analysis" / stem
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("# 报告\n内容", encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"chunk": 0, "idx": 0, "action": [1], "description": "<think>\n</think>"}),
        encoding="utf-8",
    )
    (out_dir / f"{stem}_evidence.json").write_text(
        json.dumps(_evidence_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "images" / "zoom_1.jpg").write_bytes(b"\xff\xd8jpeg")
    return out_dir


def _evidence_payload() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "video": {
            "file_name": "v1.mp4",
            "duration_sec": 15.0,
            "fps": 25.0,
            "width": 1920,
            "height": 1080,
        },
        "events": [
            {
                "event_id": 0,
                "name": "违法停车",
                "detected": False,
                "calibration": {
                    "frame_index": None,
                    "emergency_polygon_rel": None,
                    "chevron_polygon_rel": None,
                },
                "evidence_regions": [],
                "gallery_images": [],
            },
            {
                "event_id": 1,
                "name": "应急车道占用",
                "detected": True,
                "calibration": {
                    "frame_index": 4,
                    "emergency_polygon_rel": [[0.1, 0.2], [0.3, 0.2], [0.3, 0.8], [0.1, 0.8]],
                    "chevron_polygon_rel": [[0.4, 0.4], [0.5, 0.4], [0.5, 0.6], [0.4, 0.6]],
                },
                "evidence_regions": [
                    {
                        "frame_index": 4,
                        "box_rel": [0.12, 0.3, 0.2, 0.5],
                        "label": "白色轿车",
                        "image": "images/zoom_1.jpg",
                    }
                ],
                "gallery_images": ["images/overlay.jpg"],
            },
        ],
    }


def _wait_for_job(client: TestClient, job_id: int, timeout: float = 15.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {job["id"]: job for job in client.get("/api/jobs").json()}
        job = jobs[job_id]
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


_FAKE_INFER_SCRIPT = (
    "print('[1/4] Preprocessing video...');"
    "print('[2/4] Expert Agent Layer...');"
    "print('[3/4] Adjudication...');"
    "print('[3.5/4] SFT label rewrite...');"
    "print('[4/4] Generating report...')"
)


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


def _make_tree_workspace(tmp_path: Path) -> Path:
    """Workspace with nested dirs, videos at two levels, dotfiles, plain files."""
    (tmp_path / "v1.mp4").write_bytes(b"")
    (tmp_path / "v2.avi").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("doc", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.mp4").write_bytes(b"")
    (sub / "readme.md").write_text("hi", encoding="utf-8")
    (tmp_path / ".hiddendir").mkdir()
    return tmp_path


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


def _make_tree(root: Path) -> Path:
    """Directory tree: two visible dirs, one hidden dir and a plain file."""
    (root / "beta").mkdir()
    (root / "Alpha").mkdir()
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("not a dir", encoding="utf-8")
    return root


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
# Results reading
# ---------------------------------------------------------------------------


class TestResults:
    def test_get_results(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/results/v1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["report_md"] == "# 报告\n内容"
        assert data["sft_label"]["action"] == [1]
        assert data["evidence"]["schema_version"] == 1
        assert len(data["evidence"]["events"]) == 2

    def test_get_results_unknown_stem_returns_nulls(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        data = client.get("/api/results/v2").json()
        assert data == {"report_md": None, "sft_label": None, "evidence": None}


# ---------------------------------------------------------------------------
# Evidence editing
# ---------------------------------------------------------------------------


class TestEvidencePut:
    def _client(self, tmp_path: Path) -> TestClient:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        return TestClient(create_app(workspace=str(workspace)))

    def test_put_unchanged_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 200

    def test_put_polygon_edit_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["calibration"]["emergency_polygon_rel"][0] = [0.15, 0.25]
        payload["events"][1]["calibration"]["chevron_polygon_rel"] = None
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 200

        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1_evidence.json").read_text(encoding="utf-8")
        )
        assert disk["events"][1]["calibration"]["emergency_polygon_rel"][0] == [0.15, 0.25]
        assert disk["events"][1]["calibration"]["chevron_polygon_rel"] is None

    def test_put_box_and_label_edit_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["evidence_regions"][0]["box_rel"] = [0.1, 0.1, 0.9, 0.9]
        payload["events"][1]["evidence_regions"][0]["label"] = "黑色货车"
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 200

        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1_evidence.json").read_text(encoding="utf-8")
        )
        region = disk["events"][1]["evidence_regions"][0]
        assert region["box_rel"] == [0.1, 0.1, 0.9, 0.9]
        assert region["label"] == "黑色货车"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["events"][1].update({"name": "改名"}),
            lambda p: p["events"][1].update({"detected": False}),
            lambda p: p["events"][1].update({"event_id": 9}),
            lambda p: p["events"][1]["calibration"].update({"frame_index": 7}),
            lambda p: p["events"][1].update({"gallery_images": []}),
            lambda p: p["events"][1]["evidence_regions"][0].update({"frame_index": 1}),
            lambda p: p["events"][1]["evidence_regions"][0].update({"image": None}),
            lambda p: p["video"].update({"duration_sec": 99.0}),
            lambda p: p.update({"events": p["events"][:1]}),
        ],
    )
    def test_put_non_editable_change_422(self, tmp_path: Path, mutate: Any) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        mutate(payload)
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 422

    def test_put_coordinate_out_of_range_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["evidence_regions"][0]["box_rel"] = [0.1, 0.1, 1.5, 0.9]
        assert client.put("/api/results/v1/evidence", json=payload).status_code == 422

    def test_put_wrong_schema_version_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["schema_version"] = 2
        assert client.put("/api/results/v1/evidence", json=payload).status_code == 422

    def test_put_malformed_box_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _evidence_payload()
        payload["events"][1]["evidence_regions"][0]["box_rel"] = [0.1, 0.2]
        assert client.put("/api/results/v1/evidence", json=payload).status_code == 422

    def test_put_missing_evidence_file_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Event config + SFT editing
# ---------------------------------------------------------------------------


def _sft_payload() -> Dict[str, Any]:
    """完整的 SFT 样本(与 <stem>.json 契约的 7 个键一致)。"""
    return {
        "chunk": "chunk #1",
        "idx": 1,
        "action": [2],
        "description": (
            "<think>\n违法停车：未发现。\n\n应急车道占用：一辆白色小车静止于应急车道。\n"
            "</think>\n<answer>\n天气：晴天\n时间：白天\n场景：高速公路主路。\n"
            "最终结论：本视频块检出以下事件。\nclass2: 应急车道占用\n</answer>"
        ),
        "start_timestamp": 0.0,
        "end_timestamp": 15.0,
        "chunk_name": "v1.mp4",
    }


class TestConfigEvents:
    def test_events_match_yaml(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/config/events")
        assert resp.status_code == 200

        yaml_path = (
            Path(__file__).resolve().parents[3]
            / "traffic_analyzer"
            / "config"
            / "event_categories.yaml"
        )
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        expected = sorted(
            (
                {
                    "event_id": c["event_id"],
                    "name_zh": c["name_zh"],
                    "is_active": c.get("is_active", True),
                }
                for c in data["event_categories"]
            ),
            key=lambda e: e["event_id"],
        )
        assert resp.json() == expected
        # 当前配置:0-7 激活,8 抛洒物 / 9 实线变道 未激活。
        assert [e["is_active"] for e in resp.json()] == [True] * 8 + [False] * 2


class TestSftPut:
    def _client(self, tmp_path: Path) -> TestClient:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        # _make_results 写入的 SFT 缺字段,覆盖为契约完整的样本。
        (workspace / "analysis" / "v1" / "v1.json").write_text(
            json.dumps(_sft_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return TestClient(create_app(workspace=str(workspace)))

    def test_put_description_and_action_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["action"] = [2, 3]
        payload["description"] = "<think>\n改写过。\n</think>\n<answer>\n天气：阴天\n</answer>"
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        assert resp.json() == payload

        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk == payload

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update({"chunk": "chunk #2"}),
            lambda p: p.update({"idx": 2}),
            lambda p: p.update({"start_timestamp": 1.0}),
            lambda p: p.update({"end_timestamp": 20.0}),
            lambda p: p.update({"chunk_name": "other.mp4"}),
        ],
    )
    def test_put_non_editable_change_422(self, tmp_path: Path, mutate: Any) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        mutate(payload)
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 422
        # 磁盘文件未被改动。
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk == _sft_payload()

    def test_put_missing_file_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.put("/api/results/v1/sft", json=_sft_payload())
        assert resp.status_code == 404

    def test_put_bad_stem_404(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        resp = client.put("/api/results/a..b/sft", json=_sft_payload())
        assert resp.status_code == 404

    @pytest.mark.parametrize("action", [[9], [0], [12], [-1], [1.5], [2, 9]])
    def test_put_invalid_action_422(self, tmp_path: Path, action: List[Any]) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["action"] = action
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Result images
# ---------------------------------------------------------------------------


class TestResultImages:
    def test_get_image_ok(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/results/v1/images/zoom_1.jpg")
        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8jpeg"

    def test_get_image_missing_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/results/v1/images/nope.jpg").status_code == 404

    def test_get_image_path_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        # URL-encoded "../" — must never escape the images/ directory.
        resp = client.get("/api/results/v1/images/..%2F..%2Fv1_evidence.json")
        assert resp.status_code == 404
        assert client.get("/api/results/v1/images/..%2Fzoom_1.jpg").status_code == 404


# ---------------------------------------------------------------------------
# Jobs: infer queue + progress parsing
# ---------------------------------------------------------------------------


class TestJobs:
    def test_infer_progress_and_done(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", _FAKE_INFER_SCRIPT],
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"stems": ["v1"]})
        assert resp.status_code == 200
        job_id = resp.json()["job_ids"][0]

        job = _wait_for_job(client, job_id)
        assert job["kind"] == "infer"
        assert job["stem"] == "v1"
        assert job["status"] == "done"
        assert job["returncode"] == 0
        assert job["progress"]["step_index"] == 5
        assert job["progress"]["step_label"] == "报告"
        assert job["progress"]["total_steps"] == 5
        assert job["progress"]["fraction"] == 1.0
        assert any("[3.5/4]" in line for line in job["log_tail"])

        # Output directory for the analysis was created up front.
        assert (workspace / "analysis" / "v1").is_dir()

    def test_step_marker_mapping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """[3.5/4] maps to step 4 (SFT), not to step 3."""
        from traffic_analyzer.web.jobs import Job, JobManager

        manager = JobManager()
        manager.submit(
            "infer",
            [sys.executable, "-c", "print('[3.5/4] SFT label rewrite...')"],
            stem="x",
        )
        job = manager._jobs[1]
        deadline = time.time() + 15
        while job.status not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.05)
        assert job.step_index == 4
        assert job.step_label == "SFT"

    def test_infer_serial_queue_two_videos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", f"print('{stem}')"],
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"stems": ["v1", "v2"]})
        job_ids = resp.json()["job_ids"]
        assert len(job_ids) == 2
        assert job_ids[1] == job_ids[0] + 1
        for job_id in job_ids:
            assert _wait_for_job(client, job_id)["status"] == "done"

    def test_infer_unknown_stem_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.post("/api/infer", json={"stems": ["ghost"]})
        assert resp.status_code == 404

    def test_build_infer_command_nested_rel(self, tmp_path: Path) -> None:
        """argv points at workspace/<rel> video and flat analysis/<stem>/ output."""
        from traffic_analyzer.web.jobs import build_infer_command

        argv = build_infer_command(tmp_path, "sub/nested.mp4", "nested")
        assert argv[argv.index("--video") + 1] == str(tmp_path / "sub" / "nested.mp4")
        assert argv[argv.index("--output") + 1] == str(
            tmp_path / "analysis" / "nested" / "report.md"
        )
        assert argv[argv.index("--sft-output-dir") + 1] == str(
            tmp_path / "analysis" / "nested"
        )

    def test_infer_nested_rel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = _make_tree_workspace(tmp_path)
        captured: Dict[str, Any] = {}

        def _spy(ws: Path, rel: str, stem: str) -> List[str]:
            captured["rel"] = rel
            captured["stem"] = stem
            return [sys.executable, "-c", "print('ok')"]

        monkeypatch.setattr("traffic_analyzer.web.jobs.build_infer_command", _spy)
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/infer", json={"rels": ["sub/nested.mp4"]})
        assert resp.status_code == 200
        assert captured == {"rel": "sub/nested.mp4", "stem": "nested"}

        job = _wait_for_job(client, resp.json()["job_ids"][0])
        assert job["status"] == "done"
        assert job["stem"] == "nested"
        assert job["rel"] == "sub/nested.mp4"
        # Flat results contract: analysis/<stem>/ was created up front.
        assert (workspace / "analysis" / "nested").is_dir()

    def test_infer_duplicate_stem_rels_400(self, tmp_path: Path) -> None:
        """Two rels sharing a stem would collide in analysis/<stem>/."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "a" / "dup.mp4").write_bytes(b"")
        (tmp_path / "b" / "dup.mp4").write_bytes(b"")
        client = TestClient(create_app(workspace=str(tmp_path)))

        resp = client.post("/api/infer", json={"rels": ["a/dup.mp4", "b/dup.mp4"]})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "dup" in detail
        assert "a/dup.mp4" in detail
        assert "b/dup.mp4" in detail
        # Whole request rejected: no jobs were queued.
        assert client.get("/api/jobs").json() == []

    def test_infer_rel_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.post("/api/infer", json={"rels": ["../v1.mp4"]}).status_code == 404
        assert client.post(
            "/api/infer", json={"rels": ["sub/../../etc/passwd.mp4"]}
        ).status_code == 404

    def test_infer_rel_not_a_video_404(self, tmp_path: Path) -> None:
        workspace = _make_tree_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.post("/api/infer", json={"rels": ["notes.txt"]}).status_code == 404
        assert client.post("/api/infer", json={"rels": ["ghost.mp4"]}).status_code == 404

    def test_infer_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/api/infer", json={"stems": ["v1"]})
        assert resp.status_code == 400

    def test_failed_job_records_returncode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, name, stem: [sys.executable, "-c", "import sys; sys.exit(3)"],
        )
        client = TestClient(create_app(workspace=str(workspace)))
        job_id = client.post("/api/infer", json={"stems": ["v1"]}).json()["job_ids"][0]
        job = _wait_for_job(client, job_id)
        assert job["status"] == "failed"
        assert job["returncode"] == 3

    def test_log_tail_capped(self, tmp_path: Path) -> None:
        from traffic_analyzer.web.jobs import JobManager

        manager = JobManager()
        manager.submit("infer", [sys.executable, "-c", "print(*range(100), sep='\\n')"], stem="x")
        job = manager._jobs[1]
        deadline = time.time() + 15
        while job.status not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.05)
        assert len(job.log_tail) == 30
        assert job.log_tail[-1] == "99"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_evaluate_without_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.post("/api/evaluate", json={}).status_code == 400

    def test_evaluate_without_results_400(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.post("/api/evaluate", json={}).status_code == 400

        # Empty analysis directory is still a 400.
        (workspace / "analysis").mkdir()
        assert client.post("/api/evaluate", json={}).status_code == 400

    def test_evaluate_latest_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/evaluate/latest").status_code == 404

    def test_evaluate_run_and_read_latest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        metrics = {"macro": {"precision": 1.0}, "total_videos": 1}

        def _fake_command(ws: Path) -> List[str]:
            latest = ws / "analysis" / "evaluation" / "latest.json"
            script = (
                "import json, pathlib;"
                f"p = pathlib.Path(r'{latest}');"
                "p.parent.mkdir(parents=True, exist_ok=True);"
                f"p.write_text(json.dumps({metrics!r}), encoding='utf-8')"
            )
            return [sys.executable, "-c", script]

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_evaluate_command", _fake_command
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/evaluate", json={})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        job = _wait_for_job(client, job_id)
        assert job["kind"] == "evaluate"
        assert job["status"] == "done"
        assert job["progress"]["fraction"] == 1.0

        latest = client.get("/api/evaluate/latest")
        assert latest.status_code == 200
        assert latest.json()["total_videos"] == 1


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


class _FakeCapture:
    instances = 0

    def __init__(self, path: str) -> None:
        type(self).instances += 1
        self._index = 0

    def get(self, prop: int) -> float:
        return 10.0

    def set(self, prop: int, value: float) -> bool:
        self._index = int(value)
        return True

    def read(self) -> Any:
        return True, f"frame-{self._index}"

    def release(self) -> None:
        pass


class _FakeBuf:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def tobytes(self) -> bytes:
        return self._data


def _install_fake_cv2(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCapture.instances = 0
    fake = SimpleNamespace(
        VideoCapture=_FakeCapture,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_POS_FRAMES=1,
        imencode=lambda ext, frame: (True, _FakeBuf(f"jpeg:{frame}".encode())),
    )
    monkeypatch.setattr("traffic_analyzer.web.frames.cv2", fake)


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
# CLI: web subcommand
# ---------------------------------------------------------------------------


class TestCliWeb:
    def test_web_help(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["web", "--help"])
        assert exc_info.value.code == 0

    def test_web_defaults(self) -> None:
        args = build_parser().parse_args(["web"])
        assert args.command == "web"
        assert args.host == "127.0.0.1"
        assert args.port == 8600
        assert args.workspace is None

    def test_web_custom_args(self) -> None:
        args = build_parser().parse_args(["web", "--host", "0.0.0.0", "--port", "9000", "-w", "/tmp/ws"])
        assert args.host == "0.0.0.0"
        assert args.port == 9000
        assert args.workspace == "/tmp/ws"

    def test_cmd_web_invalid_workspace(self, tmp_path: Path) -> None:
        from traffic_analyzer.cli import cmd_web

        args = build_parser().parse_args(["web", "--workspace", str(tmp_path / "nope")])
        assert cmd_web(args) == 1


# ---------------------------------------------------------------------------
# Video streaming (/api/videos/{stem}/stream)
# ---------------------------------------------------------------------------

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None or _FFPROBE is None, reason="ffmpeg/ffprobe not installed"
)


def _make_tiny_video(path: Path, frames: int = 8) -> Path:
    """Tiny MPEG-4 Part 2 (mp4v) clip — not browser-native, needs transcode."""
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for i in range(frames):
        writer.write(np.full((48, 64, 3), i * 20, dtype=np.uint8))
    writer.release()
    assert path.stat().st_size > 0
    return path


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
            stdout = io.BytesIO(b"")

            def poll(self) -> int:
                return 0

            def wait(self) -> int:
                return 0

            def kill(self) -> None:
                pass

        def _fake_popen(argv: List[str], **kwargs: Any) -> Any:
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(
            "traffic_analyzer.web.video_stream.subprocess.Popen", _fake_popen
        )
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/videos/v1/stream", params={"ss": 12.5})
        assert resp.status_code == 200
        argv = captured["argv"]
        assert argv[argv.index("-ss") + 1] == "12.500"
        assert argv[-2:] == ["mp4", "-"]

    @requires_ffmpeg
    def test_stream_mp4v_transcodes_to_mp4(self, tmp_path: Path) -> None:
        _make_tiny_video(tmp_path / "clip.mp4")
        client = TestClient(create_app(workspace=str(tmp_path)))
        resp = client.get("/api/videos/clip/stream")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        assert len(resp.content) > 0
        assert b"ftyp" in resp.content[:32]

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


def _make_stream_workspace(tmp_path: Path) -> Path:
    """Workspace with a nested (dummy-content) video plus a plain file."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.mp4").write_bytes(b"\x00" * 2048)
    (sub / "notes.txt").write_text("not a video", encoding="utf-8")
    return tmp_path


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
# Static asset cache headers
# ---------------------------------------------------------------------------


class TestStaticCacheHeaders:
    """SPA assets must always revalidate so upgrades don't break on stale cache."""

    def test_index_and_assets_send_no_cache(self) -> None:
        client = TestClient(create_app())
        for path in ("/", "/app.js", "/style.css"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers.get("cache-control") == "no-cache", path

    def test_api_responses_not_affected(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert "cache-control" not in resp.headers
