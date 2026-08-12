"""Evidence/SFT editing, event-config and yaml-cache tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from fastapi.testclient import TestClient

from traffic_analyzer.web.app import create_app

from .conftest import _evidence_payload, _make_results, _make_workspace, _sft_payload


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
class TestConfigEvents:
    def test_events_match_yaml(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/config/events")
        assert resp.status_code == 200

        config_dir = Path(__file__).resolve().parents[3] / "traffic_analyzer" / "config"
        data = yaml.safe_load((config_dir / "event_categories.yaml").read_text(encoding="utf-8"))
        options_data = yaml.safe_load(
            (config_dir / "event_options.yaml").read_text(encoding="utf-8")
        )
        options_index = {
            int(ev["event_id"]): [
                {
                    "key": str(g["key"]),
                    "label": str(g.get("label") or g["key"]),
                    "options": [str(o) for o in g.get("options") or []],
                    "required": bool(g.get("required", False)),
                    "multi": bool(g.get("multi", False)),
                }
                for g in ev.get("groups") or []
            ]
            for ev in options_data.get("event_options") or []
        }
        expected = sorted(
            (
                {
                    "event_id": c["event_id"],
                    "name_zh": c["name_zh"],
                    "is_active": c.get("is_active", True),
                    "options": options_index.get(int(c["event_id"]), []),
                }
                for c in data["event_categories"]
            ),
            key=lambda e: e["event_id"],
        )
        assert resp.json() == expected
        # 当前配置:10 个类别全部激活(event_id 1-8、10 抛洒物、11 实线变道)。
        assert [e["is_active"] for e in resp.json()] == [True] * 10

    def test_options_closed_enums(self) -> None:
        client = TestClient(create_app())
        events = {e["event_id"]: e for e in client.get("/api/config/events").json()}
        # 10 个激活事件均有非空属性组(含抛洒物(10) 与实线变道(11))
        for ev_id in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11):
            assert events[ev_id]["options"], f"event {ev_id} missing options"
        # 属性组契约:key/label/封闭 options/required;施工要素为多选
        for ev in events.values():
            for g in ev["options"]:
                assert g["key"] and g["label"] and g["options"]
                assert isinstance(g["required"], bool)
        work = next(g for g in events[7]["options"] if g["key"] == "work_elements")
        assert work["multi"] is True
        # 违停基准样例:lane_type/direction/vehicle_type 三组必填
        keys = [g["key"] for g in events[1]["options"]]
        assert keys == ["lane_type", "direction", "vehicle_type"]
        assert "应急车道" in events[1]["options"][0]["options"]
        # 实线变道(11):方向 + 车辆类型两组必填(v4.5「只针对机动车」)
        keys_11 = [g["key"] for g in events[11]["options"]]
        assert keys_11 == ["direction", "vehicle_type"]
        assert events[11]["options"][0]["options"] == ["来向", "去向"]
        assert events[11]["options"][1]["options"] == ["小型车", "大客车", "货车", "工程车"]


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
        body = resp.json()
        assert body.pop("file_sig") is not None
        assert body == payload

        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk.pop("last_edited_by") == "local"
        disk.pop("last_edited_at", None)
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

    def test_put_event_attributes_ok(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["event_attributes"] = {
            "1": {"lane_type": "应急车道", "direction": "来向", "vehicle_type": "工程车"},
            "7": {"direction": "去向", "work_elements": ["施工车辆", "施工人员"]},
            "11": {"direction": "来向", "vehicle_type": "小型车"},
        }
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body.pop("file_sig") is not None
        assert body == payload
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk.pop("last_edited_by") == "local"
        disk.pop("last_edited_at", None)
        assert disk == payload

    @pytest.mark.parametrize(
        "attrs",
        [
            {"1": {"vehicle_type": "跑车"}},                # 枚举外取值
            {"1": {"color": "红"}},                         # 未定义的属性键
            {"99": {"direction": "来向"}},                  # 未定义的事件
            {"11": {"vehicle_type": "警车"}},               # 实线变道枚举外取值
            {"1": {"direction": ""}},                       # 空串不是合法选项
            {"7": {"work_elements": "施工车辆"}},           # 多选组必须是列表
            {"7": {"work_elements": ["施工车辆", "烟花"]}},  # 列表内含枚举外取值
            {"1": {"direction": ["来向"]}},                 # 单选组必须是字符串
        ],
    )
    def test_put_event_attributes_invalid_422(
        self, tmp_path: Path, attrs: Dict[str, Any]
    ) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["event_attributes"] = attrs
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 422
        # 磁盘文件未被改动。
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk == _sft_payload()

    def test_put_without_event_attributes_keeps_legacy_shape(
        self, tmp_path: Path
    ) -> None:
        # 旧格式(无 event_attributes)编辑保存后仍是 7 键,不引入 null 字段。
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["description"] = "<think>\n仅文字编辑。\n</think>\n<answer>\n天气：晴天\n</answer>"
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk.pop("last_edited_by") == "local"
        disk.pop("last_edited_at", None)
        assert disk == payload
        assert "event_attributes" not in disk

    def test_put_attr_mentions_ok(self, tmp_path: Path) -> None:
        # 声明提及合法:键为已知事件/属性组,值为字符串数组(空数组允许),
        # 每个提及串出现在对应事件的 think 段落正文中。
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["attr_mentions"] = {
            "2": {
                "vehicle_type": ["白色小车", "小车"],
                "lane_type": ["应急车道"],
                "direction": [],
            }
        }
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body.pop("file_sig") is not None
        assert body == payload
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk.pop("last_edited_by") == "local"
        disk.pop("last_edited_at", None)
        assert disk == payload

    def _roadwork_payload(self) -> Dict[str, Any]:
        """事件 7(道路施工,含多选组 work_elements)的 SFT 样本。"""
        payload = _sft_payload()
        payload["action"] = [7]
        payload["description"] = (
            "<think>\n道路施工：来向一侧道路施工,现场有黄色工程车与锥桶。\n"
            "</think>\n<answer>\n天气：晴天\n时间：白天\n场景：高速公路主路。\n"
            "最终结论：本视频块检出以下事件。\nclass7: 道路施工\n</answer>"
        )
        return payload

    def test_put_attr_mentions_nested_multi_ok(self, tmp_path: Path) -> None:
        # 多选组新格式:嵌套「选项名 → 提及串数组」;选项名须在该组 options 内,
        # 提及串仍按事件 think 段落子串校验。
        client = self._client(tmp_path)
        payload = self._roadwork_payload()
        payload["attr_mentions"] = {
            "7": {
                "work_elements": {
                    "施工车辆": ["黄色工程车"],
                    "交通锥/隔离栏": ["锥桶"],
                }
            }
        }
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body.pop("file_sig") is not None
        assert body == payload
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk.pop("last_edited_by") == "local"
        disk.pop("last_edited_at", None)
        assert disk == payload

    def test_put_attr_mentions_flat_multi_still_ok(self, tmp_path: Path) -> None:
        # 旧扁平数组的多选组提及仍然合法(向后兼容)。
        client = self._client(tmp_path)
        payload = self._roadwork_payload()
        payload["attr_mentions"] = {
            "7": {"work_elements": ["黄色工程车", "锥桶"]}
        }
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body.pop("file_sig") is not None
        assert body == payload

    def test_put_attr_mentions_nested_bad_option_422(self, tmp_path: Path) -> None:
        # 嵌套对象的选项名不在该组 options 内 → 422,磁盘不改动。
        client = self._client(tmp_path)
        payload = self._roadwork_payload()
        payload["attr_mentions"] = {
            "7": {"work_elements": {"烟花": ["烟花"]}}
        }
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 422
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk == _sft_payload()

    def test_put_event_attributes_null_single_ok(self, tmp_path: Path) -> None:
        """契约允许单选属性为 null(VLM 看不清时);多选空数组允许。"""
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["event_attributes"] = {
            "2": {"lane_type": "应急车道", "direction": None, "vehicle_type": None}
        }
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk["event_attributes"]["2"]["direction"] is None

    @pytest.mark.parametrize(
        "mentions",
        [
            {"2": {"vehicle_type": ["黄色工程作业车"]}},  # 提及串不在事件 2 正文
            {"2": {"color": ["红"]}},                     # 未定义的属性键
            {"2": {"vehicle_type": "小车"}},              # 值必须是数组
            {"2": {"vehicle_type": ["小车", 3]}},         # 数组内必须都是字符串
            {"2": {"vehicle_type": {"小型车": ["小车"]}}},  # 单选组不允许嵌套对象
            {"7": {"work_elements": {"施工车辆": "黄色工程车"}}},  # 嵌套值必须是数组
            {"99": {"direction": ["来向"]}},              # 未定义的事件
        ],
    )
    def test_put_attr_mentions_invalid_422(
        self, tmp_path: Path, mentions: Dict[str, Any]
    ) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["attr_mentions"] = mentions
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 422
        # 磁盘文件未被改动。
        disk = json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )
        assert disk == _sft_payload()

    def test_put_attr_mentions_not_found_detail_names_mention(
        self, tmp_path: Path
    ) -> None:
        client = self._client(tmp_path)
        payload = _sft_payload()
        payload["attr_mentions"] = {"2": {"vehicle_type": ["黄色工程作业车"]}}
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 422
        assert "黄色工程作业车" in resp.text
# ---------------------------------------------------------------------------
# F3: atomic JSON writes + PUT hardening
# ---------------------------------------------------------------------------


class TestAtomicWriteJson:
    def test_replaces_original_and_removes_tmp(self, tmp_path: Path) -> None:
        from traffic_analyzer.web.evidence_api import _atomic_write_json

        target = tmp_path / "x.json"
        target.write_text('{"old": 1}', encoding="utf-8")
        _atomic_write_json(target, {"new": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": 2}
        assert not (tmp_path / "x.json.tmp").exists()

    def test_put_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        client = TestClient(create_app(workspace=str(workspace)))
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 200
        assert list((workspace / "analysis" / "v1").glob("*.tmp")) == []
class TestPutGuards:
    def _client(self, tmp_path: Path) -> TestClient:
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        (workspace / "analysis" / "v1" / "v1.json").write_text(
            json.dumps(_sft_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return TestClient(create_app(workspace=str(workspace)))

    def test_put_evidence_non_dict_disk_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        (tmp_path / "analysis" / "v1" / "v1_evidence.json").write_text(
            "[1, 2, 3]", encoding="utf-8"
        )
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 422  # not a 500 AttributeError

    def test_put_rejected_while_infer_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        state = {"status": "running"}

        def _fake_list_jobs(self: Any) -> List[Dict[str, Any]]:
            return [
                {
                    "id": 1,
                    "kind": "infer",
                    "stem": "v1",
                    "rel": "v1.mp4",
                    "status": state["status"],
                    "progress": {},
                    "log_tail": [],
                    "returncode": None,
                }
            ]

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.JobManager.list_jobs", _fake_list_jobs
        )

        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 409
        assert "v1" in resp.json()["detail"]
        assert client.put("/api/results/v1/sft", json=_sft_payload()).status_code == 409

        # Once the job finishes, both PUTs go through again.
        state["status"] = "done"
        assert client.put("/api/results/v1/evidence", json=_evidence_payload()).status_code == 200
        assert client.put("/api/results/v1/sft", json=_sft_payload()).status_code == 200

    def test_put_other_stem_unaffected_by_infer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)

        def _fake_list_jobs(self: Any) -> List[Dict[str, Any]]:
            return [
                {
                    "id": 1,
                    "kind": "infer",
                    "stem": "v2",
                    "rel": "v2.avi",
                    "status": "running",
                    "progress": {},
                    "log_tail": [],
                    "returncode": None,
                }
            ]

        monkeypatch.setattr(
            "traffic_analyzer.web.jobs.JobManager.list_jobs", _fake_list_jobs
        )
        resp = client.put("/api/results/v1/evidence", json=_evidence_payload())
        assert resp.status_code == 200
class TestConfigEventsFailures:
    def test_missing_yaml_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.web.evidence_api._EVENT_CATEGORIES_YAML",
            tmp_path / "nope.yaml",
        )
        client = TestClient(create_app())
        resp = client.get("/api/config/events")
        assert resp.status_code == 500
        assert "event categories" in resp.json()["detail"]

    def test_corrupt_yaml_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("event_categories: [unclosed", encoding="utf-8")
        monkeypatch.setattr(
            "traffic_analyzer.web.evidence_api._EVENT_CATEGORIES_YAML", bad
        )
        client = TestClient(create_app())
        resp = client.get("/api/config/events")
        assert resp.status_code == 500
        assert "event categories" in resp.json()["detail"]

    def test_entries_without_ids_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "partial.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "event_categories": [
                        {"event_id": 1, "name_zh": "违法停车"},
                        {"name_zh": "缺 id"},
                        {"event_id": 2},
                    ]
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "traffic_analyzer.web.evidence_api._EVENT_CATEGORIES_YAML", cfg
        )
        # options 也用受控的临时 yaml,避免依赖仓库真实 event_options.yaml 的内容。
        opts = tmp_path / "event_options.yaml"
        opts.write_text(
            yaml.safe_dump(
                {
                    "event_options": [
                        {
                            "event_id": 1,
                            "groups": [{"key": "lane_type", "options": ["应急车道"]}],
                        }
                    ]
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "traffic_analyzer.web.evidence_api._EVENT_OPTIONS_YAML", opts
        )
        client = TestClient(create_app())
        resp = client.get("/api/config/events")
        assert resp.status_code == 200
        assert resp.json() == [
            {
                "event_id": 1,
                "name_zh": "违法停车",
                "is_active": True,
                "options": [
                    {
                        "key": "lane_type",
                        "label": "lane_type",
                        "options": ["应急车道"],
                        "required": False,
                        "multi": False,
                    }
                ],
            }
        ]

    def test_empty_yaml_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "empty.yaml"
        cfg.write_text("event_categories: []\n", encoding="utf-8")
        monkeypatch.setattr(
            "traffic_analyzer.web.evidence_api._EVENT_CATEGORIES_YAML", cfg
        )
        client = TestClient(create_app())
        assert client.get("/api/config/events").status_code == 500
# ---------------------------------------------------------------------------
# B1: yaml 缓存按 (path, mtime) 失效
# ---------------------------------------------------------------------------


class TestYamlCacheInvalidation:
    def test_event_options_cache_follows_mtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """编辑 event_options.yaml 后,下一次读取反映新内容(无需重启)。"""
        from traffic_analyzer.web import evidence_api

        cfg = tmp_path / "event_options.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "event_options": [
                        {"event_id": 1, "groups": [{"key": "a", "options": ["x"]}]}
                    ]
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(evidence_api, "_EVENT_OPTIONS_YAML", cfg)

        first = evidence_api._event_options_index()
        assert [g["key"] for g in first[1]] == ["a"]

        cfg.write_text(
            yaml.safe_dump(
                {
                    "event_options": [
                        {"event_id": 1, "groups": [{"key": "b", "options": ["y"]}]}
                    ]
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        future = time.time() + 10
        os.utime(cfg, (future, future))  # 保证 mtime 变化(同秒写可能撞 mtime)

        second = evidence_api._event_options_index()
        assert [g["key"] for g in second[1]] == ["b"]

    def test_event_name_cache_follows_mtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """编辑 event_categories.yaml 后,_event_name_index 同样自动失效。"""
        from traffic_analyzer.web import evidence_api

        cfg = tmp_path / "event_categories.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {"event_categories": [{"event_id": 1, "name_zh": "违法停车"}]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(evidence_api, "_EVENT_CATEGORIES_YAML", cfg)

        assert evidence_api._event_name_index() == {"违法停车": 1}

        cfg.write_text(
            yaml.safe_dump(
                {"event_categories": [{"event_id": 1, "name_zh": "违规停车"}]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        future = time.time() + 10
        os.utime(cfg, (future, future))

        assert evidence_api._event_name_index() == {"违规停车": 1}
# ---------------------------------------------------------------------------
# B2: PUT 区分「字段未提交」(保留磁盘)与「显式 null」(删除键)
# ---------------------------------------------------------------------------


class TestSftPutOptionalFieldSemantics:
    def _client_with_attributes(self, tmp_path: Path) -> TestClient:
        """磁盘样本带 event_attributes / attr_mentions 的工作区客户端。"""
        workspace = _make_workspace(tmp_path)
        _make_results(workspace, "v1")
        payload = _sft_payload()
        payload["event_attributes"] = {
            "2": {"lane_type": "应急车道", "direction": "去向", "vehicle_type": "小型车"}
        }
        payload["attr_mentions"] = {"2": {"lane_type": ["应急车道"]}}
        (workspace / "analysis" / "v1" / "v1.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return TestClient(create_app(workspace=str(workspace)))

    def _read_disk(self, tmp_path: Path) -> Dict[str, Any]:
        return json.loads(
            (tmp_path / "analysis" / "v1" / "v1.json").read_text(encoding="utf-8")
        )

    def test_omitted_event_attributes_preserves_disk_value(
        self, tmp_path: Path
    ) -> None:
        """磁盘已有结构化标注时,不带 event_attributes 的 PUT 不得将其擦除。"""
        client = self._client_with_attributes(tmp_path)
        payload = _sft_payload()  # 不带 event_attributes / attr_mentions
        payload["description"] = (
            "<think>\n应急车道占用：一辆白色小车静止于应急车道。\n</think>\n"
            "<answer>\n天气：晴天\n</answer>"
        )
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200

        disk = self._read_disk(tmp_path)
        assert disk["event_attributes"] == {
            "2": {"lane_type": "应急车道", "direction": "去向", "vehicle_type": "小型车"}
        }
        assert disk["attr_mentions"] == {"2": {"lane_type": ["应急车道"]}}
        # 响应体同样带回保留的标注(前端可直接刷新)。
        assert resp.json()["event_attributes"] == disk["event_attributes"]

    def test_explicit_null_event_attributes_deletes_key(self, tmp_path: Path) -> None:
        """显式提交 event_attributes=null 是显式清除语义:键从磁盘样本中删除。"""
        client = self._client_with_attributes(tmp_path)
        payload = _sft_payload()
        payload["event_attributes"] = None
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200

        disk = self._read_disk(tmp_path)
        assert "event_attributes" not in disk
        # attr_mentions 未提交:磁盘值保留。
        assert disk["attr_mentions"] == {"2": {"lane_type": ["应急车道"]}}

    def test_explicit_null_attr_mentions_deletes_key(self, tmp_path: Path) -> None:
        """attr_mentions 与 event_attributes 同一契约。"""
        client = self._client_with_attributes(tmp_path)
        payload = _sft_payload()
        payload["attr_mentions"] = None
        resp = client.put("/api/results/v1/sft", json=payload)
        assert resp.status_code == 200

        disk = self._read_disk(tmp_path)
        assert "attr_mentions" not in disk
        assert "event_attributes" in disk
