"""Optimistic-lock tests: file_sig on GET, base_sig conflict → 409 on PUT."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _evidence_payload, _make_results, _make_workspace, _sft_payload


def _client(tmp_path: Path) -> TestClient:
    workspace = _make_workspace(tmp_path)
    _make_results(workspace, "v1")
    # _make_results 写入的 SFT 缺字段,覆盖为契约完整的样本(与 test_evidence_sft 同)。
    (workspace / "analysis" / "v1" / "v1.json").write_text(
        json.dumps(_sft_payload(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return TestClient(create_app(workspace=str(workspace)))


class TestFileSig:
    def test_get_results_includes_file_sig(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        data = client.get("/api/results/v1").json()
        assert isinstance(data["file_sig"], str)
        assert len(data["file_sig"]) == 16

    def test_file_sig_changes_after_write(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        sig_before = client.get("/api/results/v1").json()["file_sig"]
        payload = _sft_payload()
        payload["description"] = "<think>\n改。\n</think>\n<answer>\n天气：晴天\n</answer>"
        assert client.put("/api/results/v1/sft", json=payload).status_code == 200
        sig_after = client.get("/api/results/v1").json()["file_sig"]
        assert sig_after != sig_before

    def test_file_sig_none_without_sft_file(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        client = TestClient(create_app(workspace=str(workspace)))
        assert client.get("/api/results/v1").json()["file_sig"] is None


class TestSftOptimisticLock:
    def test_put_with_matching_base_sig_ok(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        sig = client.get("/api/results/v1").json()["file_sig"]
        payload = _sft_payload()
        payload["base_sig"] = sig
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        # base_sig 不落盘
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert "base_sig" not in disk
        assert disk["last_edited_by"] == "local"

    def test_put_with_stale_base_sig_409(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        sig = client.get("/api/results/v1").json()["file_sig"]
        # 另一人先保存,file_sig 变化
        assert client.put("/api/results/v1/sft", json=_sft_payload()).status_code == 200
        payload = _sft_payload()
        payload["base_sig"] = sig
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "conflict"

    def test_put_without_base_sig_allowed(self, tmp_path: Path) -> None:
        # 旧客户端不带 base_sig → 不做冲突检查(向后兼容)。
        client = _client(tmp_path)
        assert client.put("/api/results/v1/sft", json=_sft_payload()).status_code == 200


class TestEvidenceOptimisticLock:
    def test_put_with_stale_base_sig_409(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        evidence_path = tmp_path / "analysis" / "v1" / "v1_evidence.json"
        import hashlib

        sig = hashlib.sha256(evidence_path.read_bytes()).hexdigest()[:16]
        # 另一人先保存
        assert (
            client.put("/api/results/v1/evidence", json=_evidence_payload()).status_code
            == 200
        )
        payload = _evidence_payload()
        payload["base_sig"] = sig
        resp = client.put("/api/results/v1/evidence", json=payload)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "conflict"

    def test_put_stamps_last_edited_by(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        assert (
            client.put("/api/results/v1/evidence", json=_evidence_payload()).status_code
            == 200
        )
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1_evidence.json").read_text(encoding="utf-8")
        )
        assert disk["last_edited_by"] == "local"
        # 再次编辑时磁盘上的追溯字段不触发 422(strip 时忽略)。
        assert (
            client.put("/api/results/v1/evidence", json=_evidence_payload()).status_code
            == 200
        )
