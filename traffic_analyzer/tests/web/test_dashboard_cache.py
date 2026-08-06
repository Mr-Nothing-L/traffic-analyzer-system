"""进程内 TTL 缓存测试(dashboard 聚合 + workspace videos)。

覆盖:缓存命中不重算(spy 计数)、TTL 过期重算、review PUT 失效、
workspace 变更失效、/api/workspace/videos 命中与 SFT PUT 失效。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web import dashboard as dashboard_mod
from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.app import create_app

from .conftest import _sft_payload
from .test_dashboard import _make_dash_workspace


def _count_spy(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> Callable[[], int]:
    """Wrap module.<name> with a call counter; return the count getter."""
    original = getattr(module, name)
    calls = {"n": 0}

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return lambda: calls["n"]


class TestDashboardCache:
    def test_hit_does_not_recompute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_dash_workspace(tmp_path)
        count = _count_spy(monkeypatch, dashboard_mod, "_build_dashboard")
        client = TestClient(create_app(workspace=str(workspace)))

        first = client.get("/api/dashboard")
        assert first.status_code == 200
        assert count() == 1
        # 缓存命中:aggregate 与 rows 共用同一份构建结果,均不重算
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/dashboard/rows?page=1&size=50").status_code == 200
        assert client.get("/api/dashboard/rows?consistency=diff").status_code == 200
        assert count() == 1

    def test_ttl_expiry_recomputes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 看板缓存为实例级长 TTL(_LongTTLCache,主动失效主导,不再跟随全局
        # _CACHE_TTL_SEC);把本实例 TTL 调短,验证过期后仍会重算。
        monkeypatch.setattr(dashboard_mod._dashboard_cache, "_ttl_sec", 0.05)
        workspace = _make_dash_workspace(tmp_path)
        count = _count_spy(monkeypatch, dashboard_mod, "_build_dashboard")
        client = TestClient(create_app(workspace=str(workspace)))

        assert client.get("/api/dashboard").status_code == 200
        assert count() == 1
        time.sleep(0.1)  # 超过 TTL
        assert client.get("/api/dashboard").status_code == 200
        assert count() == 2

    def test_review_put_invalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_dash_workspace(tmp_path)
        count = _count_spy(monkeypatch, dashboard_mod, "_build_dashboard")
        client = TestClient(create_app(workspace=str(workspace)))

        assert client.get("/api/dashboard").status_code == 200
        assert count() == 1
        resp = client.put(
            "/api/dashboard/review",
            json={"stem": "02_Event_200_1", "status": "confirmed"},
        )
        assert resp.status_code == 200
        # PUT 落盘 → 缓存失效 → 下一 GET 重算且反映最新审核态
        rows = {
            r["stem"]: r
            for r in client.get("/api/dashboard/rows?size=200").json()["rows"]
        }
        assert count() == 2
        assert rows["02_Event_200_1"]["review"] == "confirmed"
        # 再 GET:新结果已缓存,不重复重算
        assert client.get("/api/dashboard").status_code == 200
        assert count() == 2

    def test_workspace_change_invalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "ws1").mkdir()
        workspace = _make_dash_workspace(tmp_path / "ws1")
        other = tmp_path / "ws2"
        other.mkdir()
        (other / "only.mp4").write_bytes(b"")
        count = _count_spy(monkeypatch, dashboard_mod, "_build_dashboard")
        client = TestClient(create_app(workspace=str(workspace)))

        assert client.get("/api/dashboard").json()["summary"]["total"] == 4
        assert count() == 1
        resp = client.post("/api/workspace", json={"path": str(other)})
        assert resp.status_code == 200
        # workspace 变更 → 缓存失效 → 重算且内容来自新工作区
        assert client.get("/api/dashboard").json()["summary"]["total"] == 1
        assert count() == 2

    def test_filtered_queries_do_not_pollute_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只读契约:过滤/分页查询(走缓存)不就地修改缓存,后续全量结果不受影响。"""
        workspace = _make_dash_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/dashboard").json()["summary"]["total"] == 4  # 填充缓存

        filtered = client.get("/api/dashboard/rows?consistency=diff").json()
        assert filtered["total"] == 1
        paged = client.get("/api/dashboard/rows?page=1&size=2").json()
        assert len(paged["rows"]) == 2 and paged["total"] == 4
        # 缓存未被过滤/切片污染
        assert client.get("/api/dashboard").json()["summary"]["total"] == 4
        assert client.get("/api/dashboard/rows?size=200").json()["total"] == 4


class TestWorkspaceVideosCache:
    def test_hit_does_not_rewalk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_dash_workspace(tmp_path)
        count = _count_spy(monkeypatch, workspace_mod, "list_videos")
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.get("/api/workspace/videos")
        assert resp.status_code == 200
        assert len(resp.json()) == 4
        assert count() == 1
        assert client.get("/api/workspace/videos").status_code == 200
        assert count() == 1

    def test_sft_put_invalidates_videos_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SFT PUT 落盘(可能改写 action)→ videos/dashboard 缓存失效重算。"""
        workspace = _make_dash_workspace(tmp_path)
        count = _count_spy(monkeypatch, workspace_mod, "list_videos")
        client = TestClient(create_app(workspace=str(workspace)))

        assert client.get("/api/workspace/videos").status_code == 200
        assert count() == 1
        # 先把磁盘样本覆盖为契约完整的 7 键格式(与 TestSftPut 同一前置),
        # 再以它为基线只改 action(其余字段不动,否则 422 非可编辑字段)
        stem = "01_Event_100_1"
        sft_path = workspace / "analysis" / stem / f"{stem}.json"
        sft_path.write_text(
            json.dumps(_sft_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload = _sft_payload()
        assert payload["action"] == [2]
        payload["action"] = [1, 2]
        resp = client.put(f"/api/results/{stem}/sft", json=payload)
        assert resp.status_code == 200
        rows = {
            r["stem"]: r
            for r in client.get("/api/dashboard/rows?size=200").json()["rows"]
        }
        assert count() == 2  # 失效后重走 list_videos
        assert rows["01_Event_100_1"]["pred_ids"] == [1, 2]
        assert rows["01_Event_100_1"]["edited"] is True  # 首次编辑冻结了 raw 快照
