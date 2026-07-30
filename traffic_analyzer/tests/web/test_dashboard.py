"""Dashboard endpoint tests (GET /api/dashboard, PUT /api/dashboard/review, raw freeze)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _sft_payload, _wait_for_job


# ---------------------------------------------------------------------------
# Fabricators
# ---------------------------------------------------------------------------


def _write_sft(workspace: Path, stem: str, action: List[int]) -> Path:
    """Fabricate analysis/<stem>/<stem>.json with the given action ids."""
    out_dir = workspace / "analysis" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    sft_path = out_dir / f"{stem}.json"
    sft_path.write_text(
        json.dumps(
            {"chunk": 0, "idx": 0, "action": action, "description": "<think>\n</think>"}
        ),
        encoding="utf-8",
    )
    return sft_path


def _make_dash_workspace(tmp_path: Path) -> Path:
    """Workspace covering all four row states.

    - 01_Event_100_1: GT {1}, pred {1}      -> consistent
    - 02_Event_200_1: GT {2}, pred {1, 2}   -> diff (extra [1])
    - 03_Event_300_1: GT {3}, no results    -> no_results
    - plain:          no GT, pred {1}       -> no_gt
    """
    for name in ("01_Event_100_1.mp4", "02_Event_200_1.mp4", "03_Event_300_1.mp4", "plain.mp4"):
        (tmp_path / name).write_bytes(b"")
    _write_sft(tmp_path, "01_Event_100_1", [1])
    _write_sft(tmp_path, "02_Event_200_1", [1, 2])
    _write_sft(tmp_path, "plain", [1])
    return tmp_path


def _rows_by_stem(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["stem"]: row for row in data["rows"]}


# ---------------------------------------------------------------------------
# GET /api/dashboard
# ---------------------------------------------------------------------------


class TestDashboardGet:
    def test_no_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/dashboard").status_code == 400

    def test_rows_four_states_and_summary(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        rows = _rows_by_stem(data)
        assert set(rows) == {
            "01_Event_100_1",
            "02_Event_200_1",
            "03_Event_300_1",
            "plain",
        }

        consistent = rows["01_Event_100_1"]
        assert consistent["rel"] == "01_Event_100_1.mp4"
        assert consistent["has_results"] is True
        assert consistent["gt_ids"] == [1]
        assert consistent["pred_ids"] == [1]
        assert consistent["status"] == "consistent"
        assert consistent["missing"] == []
        assert consistent["extra"] == []
        assert consistent["pred_raw_ids"] is None
        assert consistent["edited"] is False
        assert consistent["edit_missing"] == []
        assert consistent["edit_extra"] == []
        assert consistent["review"] == "unconfirmed"

        diff = rows["02_Event_200_1"]
        assert diff["status"] == "diff"
        assert diff["gt_ids"] == [2]
        assert diff["pred_ids"] == [1, 2]
        assert diff["missing"] == []
        assert diff["extra"] == [1]

        no_results = rows["03_Event_300_1"]
        assert no_results["has_results"] is False
        assert no_results["status"] == "no_results"
        assert no_results["gt_ids"] == [3]
        assert no_results["pred_ids"] == []

        no_gt = rows["plain"]
        assert no_gt["status"] == "no_gt"
        assert no_gt["gt_ids"] == []
        assert no_gt["pred_ids"] == [1]

        assert data["summary"] == {
            "total": 4,
            "consistent": 1,
            "diff": 1,
            "no_gt": 1,
            "no_results": 1,
            "confirmed": 0,
            "unconfirmed": 4,
            "needs_review": 0,
            "edited": 0,
        }

    def test_event_names_from_config(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        event_names = client.get("/api/dashboard").json()["event_names"]
        assert event_names["1"] == "违法停车"
        assert event_names["2"] == "应急车道占用"

    def test_metrics_values(self, tmp_path: Path) -> None:
        """Metrics use set algebra; no_gt / no_results rows do not participate."""
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        metrics = client.get("/api/dashboard").json()["metrics"]

        per_event = {ev["event_id"]: ev for ev in metrics["per_event"]}
        assert set(per_event) == {1, 2}
        # Event 1: tp from the consistent row, fp from the diff row.
        assert per_event[1]["tp"] == 1
        assert per_event[1]["fp"] == 1
        assert per_event[1]["fn"] == 0
        assert per_event[1]["precision"] == 0.5
        assert per_event[1]["recall"] == 1.0
        assert per_event[1]["f1"] == 0.6667
        # Event 2: clean tp from the diff row.
        assert per_event[2]["tp"] == 1
        assert per_event[2]["fp"] == 0
        assert per_event[2]["fn"] == 0
        assert per_event[2]["precision"] == 1.0
        assert per_event[2]["recall"] == 1.0
        assert per_event[2]["f1"] == 1.0

        assert metrics["macro"] == {"precision": 0.75, "recall": 1.0, "f1": 0.8334}
        assert metrics["micro"] == {"precision": 0.6667, "recall": 1.0, "f1": 0.8}

    def test_metrics_empty_when_nothing_evaluated(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        # Remove all evaluated rows: delete the two GT-bearing results.
        for stem in ("01_Event_100_1", "02_Event_200_1"):
            (workspace / "analysis" / stem / f"{stem}.json").unlink()
        client = TestClient(create_app(workspace=str(workspace)))
        metrics = client.get("/api/dashboard").json()["metrics"]
        assert metrics["per_event"] == []
        assert metrics["macro"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0}
        assert metrics["micro"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


# ---------------------------------------------------------------------------
# PUT /api/dashboard/review
# ---------------------------------------------------------------------------


class TestDashboardReview:
    def test_put_no_workspace_400(self) -> None:
        client = TestClient(create_app())
        resp = client.put(
            "/api/dashboard/review", json={"stem": "x", "status": "confirmed"}
        )
        assert resp.status_code == 400

    def test_put_three_states_and_file_content(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        states_path = workspace / "analysis" / "review_states.json"

        for status in ("confirmed", "needs_review", "unconfirmed"):
            resp = client.put(
                "/api/dashboard/review",
                json={"stem": "02_Event_200_1", "status": status},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == status
            states = json.loads(states_path.read_text(encoding="utf-8"))
            entry = states["02_Event_200_1"]
            assert entry["status"] == status
            assert entry["updated_at"]  # ISO timestamp persisted

        # Multiple stems accumulate in the same file.
        client.put(
            "/api/dashboard/review",
            json={"stem": "01_Event_100_1", "status": "confirmed"},
        )
        states = json.loads(states_path.read_text(encoding="utf-8"))
        assert set(states) == {"01_Event_100_1", "02_Event_200_1"}

        # GET /api/dashboard reflects the persisted states.
        data = client.get("/api/dashboard").json()
        rows = _rows_by_stem(data)
        assert rows["01_Event_100_1"]["review"] == "confirmed"
        assert rows["02_Event_200_1"]["review"] == "unconfirmed"
        assert rows["plain"]["review"] == "unconfirmed"
        assert data["summary"]["confirmed"] == 1
        assert data["summary"]["unconfirmed"] == 3

    def test_put_invalid_status_422(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.put(
            "/api/dashboard/review", json={"stem": "plain", "status": "bogus"}
        )
        assert resp.status_code == 422
        assert not (workspace / "analysis" / "review_states.json").exists()


# ---------------------------------------------------------------------------
# Raw-output freeze / discard
# ---------------------------------------------------------------------------


class TestRawFreeze:
    def _make_editable_workspace(self, tmp_path: Path) -> Path:
        (tmp_path / "01_Event_555_1.mp4").write_bytes(b"")
        out_dir = tmp_path / "analysis" / "01_Event_555_1"
        out_dir.mkdir(parents=True)
        (out_dir / "01_Event_555_1.json").write_text(
            json.dumps(_sft_payload(), ensure_ascii=False), encoding="utf-8"
        )
        return tmp_path

    def test_sft_put_freezes_raw_and_dashboard_reports_edit(
        self, tmp_path: Path
    ) -> None:
        workspace = self._make_editable_workspace(tmp_path)
        stem = "01_Event_555_1"
        client = TestClient(create_app(workspace=str(workspace)))

        edited_payload = _sft_payload()
        edited_payload["action"] = [1, 2]  # original action is [2]
        resp = client.put(f"/api/results/{stem}/sft", json=edited_payload)
        assert resp.status_code == 200

        # The pre-edit output was frozen verbatim.
        raw_path = workspace / "analysis" / stem / f"{stem}_raw.json"
        assert raw_path.is_file()
        assert json.loads(raw_path.read_text(encoding="utf-8"))["action"] == [2]

        row = _rows_by_stem(client.get("/api/dashboard").json())[stem]
        assert row["edited"] is True
        assert row["pred_raw_ids"] == [2]
        assert row["pred_ids"] == [1, 2]
        assert row["edit_missing"] == []
        assert row["edit_extra"] == [1]

        # A second PUT does not overwrite the frozen snapshot.
        again = _sft_payload()
        again["action"] = [2]
        assert client.put(f"/api/results/{stem}/sft", json=again).status_code == 200
        assert json.loads(raw_path.read_text(encoding="utf-8"))["action"] == [2]

    def test_successful_reinfer_discards_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = self._make_editable_workspace(tmp_path)
        stem = "01_Event_555_1"
        raw_path = workspace / "analysis" / stem / f"{stem}_raw.json"
        raw_path.write_text("{}", encoding="utf-8")  # stale frozen snapshot

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, rel, s: [sys.executable, "-c", "pass"],
        )
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.post("/api/infer", json={"stems": [stem]})
        assert resp.status_code == 200
        job = _wait_for_job(client, resp.json()["job_ids"][0])
        assert job["status"] == "done"

        assert not raw_path.exists()
        row = _rows_by_stem(client.get("/api/dashboard").json())[stem]
        assert row["edited"] is False
        assert row["pred_raw_ids"] is None

    def test_failed_reinfer_keeps_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = self._make_editable_workspace(tmp_path)
        stem = "01_Event_555_1"
        raw_path = workspace / "analysis" / stem / f"{stem}_raw.json"
        raw_path.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.build_infer_command",
            lambda ws, rel, s: [sys.executable, "-c", "import sys; sys.exit(1)"],
        )
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.post("/api/infer", json={"stems": [stem]})
        job = _wait_for_job(client, resp.json()["job_ids"][0])
        assert job["status"] == "failed"
        assert raw_path.is_file()
