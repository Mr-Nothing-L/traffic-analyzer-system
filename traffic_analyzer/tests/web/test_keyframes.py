"""keyframes 智能挑选端点测试(异步化:提交后台批次,立即返回)。

G 任务验收:手动 auto_pick 不再在 API 请求线程内做 VLM 调用与帧解码——
monkeypatch 慢 perform_auto_pick(模拟 VLM 数秒),断言端点响应 <1s;
完成后状态可查询(批次条目携带 picked/file_sig),跳过/失败落条目状态。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app
from traffic_analyzer.web import keyframes as keyframes_mod

from .conftest import _make_results, _make_workspace


def _client_with_sample(tmp_path: Path, stem: str = "v1") -> TestClient:
    workspace = _make_workspace(tmp_path)
    _make_results(workspace, stem)
    (workspace / "analysis" / stem / f"{stem}.json").write_text(
        json.dumps({"chunk": 0, "action": [1], "description": "d"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return TestClient(create_app(workspace=str(workspace)))


def _wait_terminal(batch_id: str, timeout: float = 8.0) -> Dict[str, Any]:
    """轮询批次直至条目终态,返回条目(超时 pytest.fail)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = keyframes_mod.batch_snapshot(batch_id)
        if snap is not None:
            items = list(snap["items"].values())
            if items and items[0]["status"] not in ("pending", "running"):
                return items[0]
        time.sleep(0.05)
    pytest.fail(f"批次 {batch_id} 未在 {timeout}s 内到达终态")


class TestAutoPickAsync:
    def test_submit_returns_fast_and_result_queryable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """慢 VLM(2s)下端点 <1s 返回批次 id;完成后 ok 条目带 picked/file_sig。"""
        client = _client_with_sample(tmp_path)

        def slow_pick(
            workspace: Path, stem: str, *, overwrite: bool = False
        ) -> Tuple[List[int], List[Dict[str, Any]], Optional[str]]:
            time.sleep(2.0)  # 模拟慢 VLM + 帧解码
            return [1, 3], [{"filename": "01_t1.0s.jpg", "order": 1}], "abc123"

        monkeypatch.setattr(keyframes_mod, "perform_auto_pick", slow_pick)

        start = time.time()
        resp = client.post("/api/videos/v1/keyframes/auto_pick", json={"overwrite": True})
        elapsed = time.time() - start
        assert resp.status_code == 200
        assert elapsed < 1.0, f"端点应在 1s 内返回,实际 {elapsed:.2f}s"

        item = _wait_terminal(resp.json()["id"])
        assert item["status"] == "ok"
        assert item["picked"] == [1, 3]
        assert item["file_sig"] == "abc123"

    def test_failure_lands_in_batch_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM 失败不再同步 502,而是落批次条目 failed + message。"""

        def bad_pick(*_a: Any, **_k: Any) -> None:
            raise keyframes_mod.KeyframeError("模型应答不合格(解析失败)")

        monkeypatch.setattr(keyframes_mod, "perform_auto_pick", bad_pick)
        client = _client_with_sample(tmp_path)

        resp = client.post("/api/videos/v1/keyframes/auto_pick", json={"overwrite": False})
        assert resp.status_code == 200
        item = _wait_terminal(resp.json()["id"])
        assert item["status"] == "failed"
        assert "模型应答不合格" in item["message"]

    def test_existing_keyframes_skip_in_batch_item(
        self, tmp_path: Path
    ) -> None:
        """已有 2 帧且未允许覆盖:跳过语义(此前同步 409)进条目 skipped。"""
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        (workspace / "analysis" / "v1" / "v1.json").write_text(
            json.dumps(
                {
                    "chunk": 0,
                    "action": [1],
                    "description": "d",
                    "keyframes": [
                        {"filename": "01_t1.0s.jpg", "frame_index": 1, "time_sec": 1.0},
                        {"filename": "02_t2.0s.jpg", "frame_index": 2, "time_sec": 2.0},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        client = TestClient(create_app(workspace=str(workspace)))

        resp = client.post("/api/videos/v1/keyframes/auto_pick", json={"overwrite": False})
        assert resp.status_code == 200
        item = _wait_terminal(resp.json()["id"])
        assert item["status"] == "skipped"
        assert "已有关键帧" in item["message"]

    def test_schedule_after_infer_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「推理成功后自动挑选」路径不走批次,直调 perform_auto_pick。"""
        calls: List[Tuple[Path, str, bool]] = []

        def fake_pick(
            workspace: Path, stem: str, *, overwrite: bool = False
        ) -> Tuple[List[int], List[Dict[str, Any]], Optional[str]]:
            calls.append((workspace, stem, overwrite))
            return [], [], None

        monkeypatch.setattr(keyframes_mod, "perform_auto_pick", fake_pick)
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        (workspace / "analysis" / "v1" / "v1.json").write_text(
            json.dumps({"chunk": 0, "action": [1], "description": "d"}), encoding="utf-8"
        )

        keyframes_mod.schedule_after_infer(workspace, "v1")
        deadline = time.time() + 5.0
        while not calls and time.time() < deadline:
            time.sleep(0.05)
        assert calls and calls[0][1] == "v1" and calls[0][2] is False
