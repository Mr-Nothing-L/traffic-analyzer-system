"""traffic_analyzer/contracts 契约模块单测:路径拼接 + 进度事件 round-trip。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from traffic_analyzer import contracts
from traffic_analyzer.contracts import (
    LaneDoneEvent,
    PhaseEvent,
    RegisterEvent,
    RunDoneEvent,
    StartEvent,
    StepEvent,
    parse_progress_line,
)


class TestAnalysisPaths:
    def test_layout_under_analysis_dir(self) -> None:
        out = Path("/ws/analysis/stem1")
        assert contracts.sft_json(out, "stem1") == out / "stem1.json"
        assert contracts.sft_quarantine_json(out, "stem1") == out / "quarantine" / "stem1.json"
        assert contracts.stem_raw_json(out, "stem1") == out / "stem1_raw.json"
        assert contracts.evidence_json(out, "stem1") == out / "stem1_evidence.json"
        assert contracts.report_md(out) == out / "report.md"
        assert contracts.keyframe_dir(out) == out / "关键帧"

    def test_non_ascii_stem(self) -> None:
        """stem 允许非 ASCII(如中文视频名),拼接不转义不变形。"""
        out = Path("/ws/analysis")
        assert contracts.sft_json(out, "视频 甲").name == "视频 甲.json"


class TestProgressEventModels:
    """发射端(utils/progress.py)真实载荷形状必须通过模型校验(round-trip)。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "register", "total": 3, "lanes": ["违法停车", "裁决"], "ts": 1.5},
            {"type": "start", "lane": "违法停车", "ts": 2.0},
            {"type": "phase", "lane": "违法停车", "fraction": 0.3, "label": "主检测分析", "ts": 2.5},
            {"type": "lane_done", "done": 1, "total": 3, "lane": "违法停车",
             "result": "detected", "ts": 3.0},
            {"type": "lane_done", "done": 2, "total": 3, "lane": "裁决",
             "result": "done", "ts": 3.5},
            {"type": "step", "step": 3.5, "total": 4, "name": "SFT", "ts": 4.0},
            {"type": "done", "status": "ok", "ts": 5.0},
        ],
    )
    def test_round_trip(self, payload: dict) -> None:
        """模型校验 → dump → JSON 序列化/反序列化 → 再校验,结果逐键一致。"""
        adapter = contracts.progress_event_adapter
        model = adapter.validate_python(payload)
        assert adapter.dump_python(model) == payload
        again = adapter.validate_python(json.loads(json.dumps(payload)))
        assert adapter.dump_python(again) == payload

    def test_lane_result_closed_enum(self) -> None:
        with pytest.raises(ValidationError):
            LaneDoneEvent.model_validate(
                {"type": "lane_done", "done": 1, "total": 1, "lane": "x",
                 "result": "nope", "ts": 1.0}
            )

    def test_missing_ts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StartEvent.model_validate({"type": "start", "lane": "x"})

    def test_event_type_sets_consistent_with_models(self) -> None:
        """类型集合与模型的 Literal 判别值一一对应(防两处漂移)。"""
        model_types = {
            m.model_fields["type"].annotation.__args__[0]
            for m in (RegisterEvent, StartEvent, PhaseEvent, LaneDoneEvent, StepEvent, RunDoneEvent)
        }
        assert model_types == set(contracts.PROGRESS_EVENT_TYPES)
        assert set(contracts.LANE_EVENT_TYPES) < set(contracts.PROGRESS_EVENT_TYPES)


class TestParseProgressLine:
    def test_valid_and_tolerated_lines(self) -> None:
        line = json.dumps({"type": "start", "lane": "x", "ts": 1.0})
        assert parse_progress_line(line) == json.loads(line)
        assert parse_progress_line("not json") is None  # 截断行
        assert parse_progress_line("[1,2]") is None  # 非对象
        assert parse_progress_line(json.dumps({"type": "unknown"})) is None  # 未知类型
