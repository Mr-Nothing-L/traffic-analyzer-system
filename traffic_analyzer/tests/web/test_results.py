"""Results-reading and result-file endpoint tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _make_results, _make_workspace


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


class TestResultFile:
    def test_file_ok_nested_tmp_img(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        out_dir = _make_results(workspace, "v1")
        nested = out_dir / "tmp_img" / "v1" / "v1_event_1_occupancy"
        nested.mkdir(parents=True)
        (nested / "02_masks_overlay.jpg").write_bytes(b"\xff\xd8mask")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get(
            "/api/results/v1/file",
            params={"path": "tmp_img/v1/v1_event_1_occupancy/02_masks_overlay.jpg"},
        )
        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8mask"

    def test_file_ok_images_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.get("/api/results/v1/file", params={"path": "images/zoom_1.jpg"})
        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8jpeg"

    def test_file_traversal_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/results/v1/file", params={"path": "../v1/v1_evidence.json"}).status_code == 404
        assert client.get("/api/results/v1/file", params={"path": "/etc/passwd"}).status_code == 404

    def test_file_missing_404(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/results/v1/file", params={"path": "tmp_img/nope.jpg"}).status_code == 404
