"""Dashboard endpoint tests.

GET /api/dashboard (summary/event_names/metrics), GET /api/dashboard/rows
(filtering + pagination), PUT /api/dashboard/review, raw freeze, and the
list_videos pruning of output directories.
"""

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


def _get_rows(client: TestClient, query: str = "") -> Dict[str, Any]:
    resp = client.get(f"/api/dashboard/rows{query}")
    assert resp.status_code == 200
    return resp.json()


def _rows_by_stem(client: TestClient) -> Dict[str, Dict[str, Any]]:
    return {row["stem"]: row for row in _get_rows(client, "?size=200")["rows"]}


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

        # GET /api/dashboard 只回汇总视图,不再携带 rows。
        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) == {"summary", "event_names", "metrics"}

        rows = _rows_by_stem(client)
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

        # 持久化的复核状态反映在 rows 与 summary 上。
        rows = _rows_by_stem(client)
        assert rows["01_Event_100_1"]["review"] == "confirmed"
        assert rows["02_Event_200_1"]["review"] == "unconfirmed"
        assert rows["plain"]["review"] == "unconfirmed"
        summary = client.get("/api/dashboard").json()["summary"]
        assert summary["confirmed"] == 1
        assert summary["unconfirmed"] == 3

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

        row = _rows_by_stem(client)[stem]
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
        row = _rows_by_stem(client)[stem]
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


# ---------------------------------------------------------------------------
# list_videos pruning (output dirs not walked)
# ---------------------------------------------------------------------------


class TestListVideosPruning:
    def _make_pruned_workspace(self, tmp_path: Path) -> Path:
        """Videos inside pruned output dirs plus legit top-level/nested videos."""
        (tmp_path / "top.mp4").write_bytes(b"")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "ok.mp4").write_bytes(b"")
        for pruned in ("analysis/junk", "tmp_img", "output", "__pycache__"):
            d = tmp_path / pruned
            d.mkdir(parents=True, exist_ok=True)
            (d / "inner.mp4").write_bytes(b"")
        return tmp_path

    def test_pruned_dir_videos_not_listed(self, tmp_path: Path) -> None:
        workspace = self._make_pruned_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        rels = {v["rel"] for v in client.get("/api/workspace/videos").json()}
        assert rels == {"top.mp4", "sub/ok.mp4"}

    def test_pruned_dir_videos_not_in_dashboard_rows(self, tmp_path: Path) -> None:
        workspace = self._make_pruned_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        rows = _get_rows(client)["rows"]
        assert {r["rel"] for r in rows} == {"top.mp4", "sub/ok.mp4"}

    def test_has_results_unaffected_by_pruning(self, tmp_path: Path) -> None:
        """has_results probes analysis/<stem>/ by path, not via the walk."""
        workspace = self._make_pruned_workspace(tmp_path)
        _write_sft(workspace, "top", [1])
        client = TestClient(create_app(workspace=str(workspace)))
        videos = {v["rel"]: v for v in client.get("/api/workspace/videos").json()}
        assert videos["top.mp4"]["has_results"] is True
        assert videos["sub/ok.mp4"]["has_results"] is False

    def test_tree_still_lists_output_dirs(self, tmp_path: Path) -> None:
        """list_tree (single level) is not pruned: output dirs stay visible."""
        workspace = self._make_pruned_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        entries = client.get("/api/workspace/tree").json()["entries"]
        dirs = {e["name"] for e in entries if e["type"] == "dir"}
        assert {"analysis", "tmp_img", "output", "__pycache__", "sub"} <= dirs


# ---------------------------------------------------------------------------
# GET /api/dashboard/rows (filtering + pagination)
# ---------------------------------------------------------------------------


class TestDashboardRows:
    def test_no_workspace_400(self) -> None:
        client = TestClient(create_app())
        assert client.get("/api/dashboard/rows").status_code == 400

    def test_default_pagination_and_row_shape(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        data = _get_rows(client)
        assert data["page"] == 1
        assert data["size"] == 50
        assert data["total"] == 4
        assert data["total_pages"] == 1
        assert len(data["rows"]) == 4
        # 行结构与原 /api/dashboard 行完全一致。
        assert set(data["rows"][0]) == {
            "rel",
            "stem",
            "has_results",
            "gt_ids",
            "pred_ids",
            "status",
            "missing",
            "extra",
            "pred_raw_ids",
            "edited",
            "edited_at",
            "edit_missing",
            "edit_extra",
            "review",
        }

    def test_pagination_slices_and_out_of_range_page(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        page1 = _get_rows(client, "?page=1&size=2")
        page2 = _get_rows(client, "?page=2&size=2")
        assert len(page1["rows"]) == 2
        assert len(page2["rows"]) == 2
        assert page1["total"] == 4
        assert page1["total_pages"] == 2
        assert [r["stem"] for r in page1["rows"]] != [r["stem"] for r in page2["rows"]]

        # page 越界:空 rows,total/total_pages 仍按过滤后全量返回。
        page3 = _get_rows(client, "?page=3&size=2")
        assert page3["rows"] == []
        assert page3["total"] == 4
        assert page3["total_pages"] == 2

    def test_page_and_size_validation(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/dashboard/rows?page=0").status_code == 422
        assert client.get("/api/dashboard/rows?size=0").status_code == 422
        # size 上限 200。
        assert client.get("/api/dashboard/rows?size=201").status_code == 422
        assert client.get("/api/dashboard/rows?size=200").status_code == 200

    def test_consistency_filter_single_and_multi(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        data = _get_rows(client, "?consistency=diff")
        assert [r["stem"] for r in data["rows"]] == ["02_Event_200_1"]
        assert data["total"] == 1

        # 逗号分隔多值。
        data = _get_rows(client, "?consistency=diff,no_gt")
        assert {r["stem"] for r in data["rows"]} == {"02_Event_200_1", "plain"}
        assert data["total"] == 2

    def test_review_filter(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        client.put(
            "/api/dashboard/review",
            json={"stem": "01_Event_100_1", "status": "confirmed"},
        )
        client.put(
            "/api/dashboard/review",
            json={"stem": "02_Event_200_1", "status": "needs_review"},
        )

        data = _get_rows(client, "?review=confirmed")
        assert [r["stem"] for r in data["rows"]] == ["01_Event_100_1"]

        data = _get_rows(client, "?review=confirmed,needs_review")
        assert {r["stem"] for r in data["rows"]} == {
            "01_Event_100_1",
            "02_Event_200_1",
        }

    def test_edited_filter(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        # 冻结快照存在 → edited=true。
        raw_path = (
            workspace / "analysis" / "01_Event_100_1" / "01_Event_100_1_raw.json"
        )
        raw_path.write_text(
            json.dumps({"chunk": 0, "idx": 0, "action": [], "description": ""}),
            encoding="utf-8",
        )
        client = TestClient(create_app(workspace=str(workspace)))

        data = _get_rows(client, "?edited=1")
        assert [r["stem"] for r in data["rows"]] == ["01_Event_100_1"]
        assert data["rows"][0]["edited"] is True
        assert data["rows"][0]["edit_extra"] == [1]

        # 不带 edited 参数不过滤。
        assert _get_rows(client)["total"] == 4

    def test_q_search_rel_and_stem_case_insensitive(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        data = _get_rows(client, "?q=PLAIN")
        assert [r["stem"] for r in data["rows"]] == ["plain"]

        data = _get_rows(client, "?q=event_100")
        assert [r["stem"] for r in data["rows"]] == ["01_Event_100_1"]

        # 子串命中全部含 "_event_" 的三条 GT 行(plain 不含)。
        data = _get_rows(client, "?q=_EVENT_")
        assert data["total"] == 3

        data = _get_rows(client, "?q=nothing-matches")
        assert data["rows"] == []
        assert data["total"] == 0
        assert data["total_pages"] == 0

    def test_filter_before_pagination(self, tmp_path: Path) -> None:
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        # 过滤后只剩 3 行,size=2 → 2 页;若先分页再过滤会得到错误的 total。
        data = _get_rows(client, "?consistency=consistent,diff,no_gt&size=2&page=2")
        assert data["total"] == 3
        assert data["total_pages"] == 2
        assert len(data["rows"]) == 1

    def test_summary_and_metrics_unaffected_by_row_filters(
        self, tmp_path: Path
    ) -> None:
        """GET /api/dashboard 汇总/指标始终基于全量未过滤数据。"""
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))

        filtered = _get_rows(client, "?consistency=diff&q=200")
        assert filtered["total"] == 1

        data = client.get("/api/dashboard").json()
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
        # 指标仍覆盖 consistent + diff 两行(event 1 与 event 2)。
        assert {ev["event_id"] for ev in data["metrics"]["per_event"]} == {1, 2}
