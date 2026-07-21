"""Unit tests for the SFT label rewrite step.

Covers:
- :data:`traffic_analyzer.core.sft_label_rewrite.EVENT_ID_TO_ACTION`.
- Pure helpers: :func:`build_description`, :func:`build_sample`,
  :func:`find_ungrounded_positive_event_ids`, :func:`write_sample`.
- :class:`traffic_analyzer.core.sft_label_rewrite.SftLabelRewriteStep`
  (guards, success path, quarantine gate, fail-open semantics).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.sft_label_rewrite import (
    EVENT_ID_TO_ACTION,
    SftLabelRewriteStep,
    build_description,
    build_sample,
    find_ungrounded_positive_event_ids,
    write_sample,
)
from traffic_analyzer.core.vlm_exceptions import FatalAPIError
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCategory,
    EventInstance,
    EventResult,
    Keyframe,
    KeyframeSequence,
    SystemConfig,
    VideoMetadata,
)


# ---------------------------------------------------------------------------
# Helpers / mocks
# ---------------------------------------------------------------------------

_NAME_ZH: Dict[int, str] = {
    0: "违法停车",
    1: "应急车道占用",
    2: "交通事故",
    3: "高速公路行人出现",
    4: "摩托车出现",
    5: "拥堵",
    6: "道路施工",
    7: "车辆逆行/倒车",
    8: "抛洒物",
    9: "实线变道",
}


class _SftSystemConfig(SystemConfig):
    """SystemConfig plus the sft_label fields (added to the model by another package)."""

    sft_label_enabled: bool = True
    sft_label_output_dir: str = "output/sft_labels"


class _MockResponse:
    """Minimal stand-in for :class:`traffic_analyzer.models.schemas.LLMResponse`."""

    def __init__(self, parsed_data: Dict[str, Any], success: bool = True) -> None:
        self.success = success
        self.parsed_data = parsed_data
        self.raw_text = str(parsed_data)


class _MockVLMEngine:
    """Mock VLM engine that returns a fixed response or raises a fixed error."""

    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        success: bool = True,
        error: Optional[Exception] = None,
    ) -> None:
        self._response = response
        self._success = success
        self._error = error
        self.calls: List[Dict[str, Any]] = []

    def call(
        self,
        template: Any,
        images: List[Any],
        context_vars: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> _MockResponse:
        self.calls.append(
            {
                "template_id": getattr(template, "template_id", "unknown"),
                "images": images,
                "context_vars": context_vars,
                "response_schema": response_schema,
            }
        )
        if self._error is not None:
            raise self._error
        return _MockResponse(self._response or {}, success=self._success)


def _make_categories() -> List[EventCategory]:
    return [
        EventCategory(
            event_id=eid,
            event_code=chr(ord("A") + eid),
            name=f"Event {eid}",
            name_zh=_NAME_ZH[eid],
            description=f"desc {eid}",
            definition=f"定义 {eid}",
        )
        for eid in range(10)
    ]


def _make_event_results(detected_ids: tuple = ()) -> Dict[int, EventResult]:
    results: Dict[int, EventResult] = {}
    for eid in range(10):
        detected = eid in detected_ids
        instances = (
            [
                EventInstance(
                    event_id=eid,
                    event_name=_NAME_ZH[eid],
                    start_time_sec=1.0,
                    end_time_sec=5.0,
                    description=f"{_NAME_ZH[eid]}实例描述",
                    reasoning=f"{_NAME_ZH[eid]}实例推理",
                )
            ]
            if detected
            else []
        )
        results[eid] = EventResult(
            event_id=eid,
            event_name=_NAME_ZH[eid],
            detected=detected,
            summary=f"{_NAME_ZH[eid]}总结",
            instances=instances,
        )
    return results


def _make_resp_data(
    present_ids: tuple = (1,),
    ungrounded: tuple = (),
) -> Dict[str, Any]:
    thoughts: List[Dict[str, Any]] = []
    for eid in range(10):
        present = eid in present_ids
        thinking = (
            "应急车道区域：画面最右侧白色实线以外为应急车道，无导流区；"
            "占用应急车道车辆类型：一辆白色小车；位置：去向一侧应急车道内静止。"
            if present
            else f"未发现{_NAME_ZH[eid]}。画面中无相关迹象。"
        )
        thoughts.append({"event_id": eid, "present": present, "thinking": thinking})
    return {
        "weather": "晴天",
        "time_of_day": "白天",
        "scene": "高速公路双向主路场景，无匝道/导流区/收费口，有来向车道与去向车道，车流量中等。",
        "event_thoughts": thoughts,
        "ungrounded_event_ids": list(ungrounded),
    }


def _make_video_meta(
    chunk_name: str = "02_Event_129_1748049879151_1.mp4",
    duration: float = 19.734,
    num_frames: int = 6,
) -> VideoMetadata:
    return VideoMetadata(
        file_path=f"/tmp/{chunk_name}",
        file_name=chunk_name,
        duration_sec=duration,
        fps=1.0,
        total_frames=num_frames,
        width=1280,
        height=720,
    )


def _make_context(
    config: Optional[SystemConfig],
    detected_ids: tuple = (1,),
    num_frames: int = 6,
    with_keyframes: bool = True,
) -> AnalysisContext:
    keyframes = (
        KeyframeSequence(
            coarse_frames=[
                Keyframe(frame_id=i, timestamp_sec=float(i), image_data=b"fake")
                for i in range(num_frames)
            ],
            precision_frames=[],
        )
        if with_keyframes
        else None
    )
    return AnalysisContext(
        config=config,
        video_meta=_make_video_meta(num_frames=num_frames),
        keyframes=keyframes,
        event_results=_make_event_results(detected_ids),
    )


@pytest.fixture
def config_manager() -> ConfigManager:
    manager = ConfigManager("./traffic_analyzer/config")
    manager.load_all()
    return manager


# ---------------------------------------------------------------------------
# EVENT_ID_TO_ACTION mapping tests
# ---------------------------------------------------------------------------


class TestEventIdToAction:
    def test_full_mapping_matches_annotation_doc_v45(self) -> None:
        """标注文档 v4.5：action 1-11 共 10 个事件，action 9 = 正常占位跳过。"""
        assert EVENT_ID_TO_ACTION == {
            0: 1,
            1: 2,
            2: 3,
            3: 4,
            4: 5,
            5: 6,
            6: 7,
            7: 8,
            8: 10,
            9: 11,
        }

    def test_mapping_has_ten_entries_and_skips_action_9(self) -> None:
        assert len(EVENT_ID_TO_ACTION) == 10
        assert sorted(EVENT_ID_TO_ACTION.values()) == [1, 2, 3, 4, 5, 6, 7, 8, 10, 11]


# ---------------------------------------------------------------------------
# build_description tests
# ---------------------------------------------------------------------------


class TestBuildDescription:
    def test_think_covers_all_ten_categories_in_fixed_order(self) -> None:
        categories = _make_categories()
        desc = build_description(
            _make_resp_data(), _make_event_results(detected_ids=(1,)), categories
        )

        positions = [desc.index(f"【{_NAME_ZH[eid]}】") for eid in range(10)]
        assert positions == sorted(positions)

    def test_answer_contains_conclusion_class_lines_and_scene_elements(self) -> None:
        categories = _make_categories()
        desc = build_description(
            _make_resp_data(), _make_event_results(detected_ids=(1, 2)), categories
        )

        assert desc.startswith("<think>\n")
        assert "\n</think>\n<answer>\n" in desc
        assert desc.endswith("\n</answer>")
        answer = desc.split("<answer>\n", 1)[1]
        assert "最终结论" in answer
        assert "class2: 应急车道占用" in answer
        assert "class3: 交通事故" in answer
        assert "天气：晴天" in answer
        assert "时间：白天" in answer
        assert "场景：高速公路双向主路场景" in answer

    def test_no_detection_conclusion_and_missing_thought_fallback(self) -> None:
        categories = _make_categories()
        resp_data = _make_resp_data(present_ids=())
        resp_data["event_thoughts"] = []  # VLM omitted all thoughts
        desc = build_description(resp_data, _make_event_results(), categories)

        assert "未检出任何事件" in desc
        assert "class" not in desc.split("<answer>\n", 1)[1]
        # Missing thinking for an undetected event falls back to 未发现。
        assert desc.count("未发现。") == 10


# ---------------------------------------------------------------------------
# build_sample tests
# ---------------------------------------------------------------------------


class TestBuildSample:
    def test_field_types_and_values_per_key(self) -> None:
        sample = build_sample(
            _make_resp_data(),
            _make_event_results(detected_ids=(1, 8)),
            _make_categories(),
            _make_video_meta(),
        )

        assert set(sample.keys()) == {
            "chunk",
            "idx",
            "action",
            "description",
            "start_timestamp",
            "end_timestamp",
            "chunk_name",
        }
        assert sample["chunk"] == "chunk #1"
        assert isinstance(sample["idx"], int) and sample["idx"] == 1
        assert sample["action"] == [2, 10]  # event_id 1→2, 8→10
        assert all(isinstance(a, int) for a in sample["action"])
        assert isinstance(sample["description"], str)
        assert isinstance(sample["start_timestamp"], float)
        assert sample["start_timestamp"] == 0.0
        assert isinstance(sample["end_timestamp"], float)
        assert sample["end_timestamp"] == 19.734
        assert sample["chunk_name"] == "02_Event_129_1748049879151_1.mp4"

    def test_empty_action_is_normal_sample(self) -> None:
        sample = build_sample(
            _make_resp_data(present_ids=()),
            _make_event_results(),
            _make_categories(),
            _make_video_meta(),
        )

        assert sample["action"] == []
        assert "未检出任何事件" in sample["description"]

    def test_missing_video_meta_falls_back_to_defaults(self) -> None:
        sample = build_sample(
            _make_resp_data(), _make_event_results(), _make_categories(), None
        )

        assert sample["end_timestamp"] == 0.0
        assert sample["chunk_name"] == ""

    def test_chunk_name_falls_back_to_file_path_basename(self) -> None:
        meta = _make_video_meta()
        meta = meta.model_copy(update={"file_name": ""})
        sample = build_sample(
            _make_resp_data(), _make_event_results(), _make_categories(), meta
        )

        assert sample["chunk_name"] == "02_Event_129_1748049879151_1.mp4"


# ---------------------------------------------------------------------------
# Anchoring gate tests (both directions)
# ---------------------------------------------------------------------------


class TestAnchoringGate:
    def test_ungrounded_positive_event_triggers_quarantine(self) -> None:
        resp_data = _make_resp_data(ungrounded=(1,))
        event_results = _make_event_results(detected_ids=(1,))

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == [1]

    def test_ungrounded_negative_event_does_not_quarantine(self) -> None:
        resp_data = _make_resp_data(ungrounded=(2,))
        event_results = _make_event_results(detected_ids=(1,))

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == []

    def test_malformed_ungrounded_entries_are_ignored(self) -> None:
        resp_data = _make_resp_data()
        resp_data["ungrounded_event_ids"] = ["1", None, 1.5, 1]
        event_results = _make_event_results(detected_ids=(1,))

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == [1]


# ---------------------------------------------------------------------------
# write_sample tests
# ---------------------------------------------------------------------------


class TestWriteSample:
    def test_writes_json_round_trip(self, tmp_path: Path) -> None:
        sample = build_sample(
            _make_resp_data(),
            _make_event_results(detected_ids=(1,)),
            _make_categories(),
            _make_video_meta(),
        )

        file_path = write_sample(sample, tmp_path, "02_Event_129_1748049879151_1")

        assert file_path == tmp_path / "02_Event_129_1748049879151_1.json"
        loaded = json.loads(file_path.read_text(encoding="utf-8"))
        assert loaded == sample


# ---------------------------------------------------------------------------
# SftLabelRewriteStep tests
# ---------------------------------------------------------------------------


class TestSftLabelRewriteStep:
    def test_success_writes_sample_with_correct_content(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data())
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(1,)
        )

        result = step._execute(context)

        assert result == tmp_path / "02_Event_129_1748049879151_1.json"
        assert result.exists()
        loaded = json.loads(result.read_text(encoding="utf-8"))
        assert loaded["chunk"] == "chunk #1"
        assert loaded["idx"] == 1
        assert loaded["action"] == [2]
        assert loaded["start_timestamp"] == 0.0
        assert loaded["end_timestamp"] == 19.734
        assert loaded["chunk_name"] == "02_Event_129_1748049879151_1.mp4"
        assert "<think>" in loaded["description"]
        assert "class2: 应急车道占用" in loaded["description"]
        # No quarantine file for a fully grounded sample.
        assert not (tmp_path / "quarantine").exists()

    def test_success_call_uses_raw_frames_template_and_schema(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data())
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(1,)
        )

        step._execute(context)

        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call["template_id"] == "sft_label_rewrite"
        assert call["images"]  # raw frames selected, non-empty
        assert call["response_schema"] is not None
        context_vars = call["context_vars"]
        verdicts = json.loads(context_vars["verdicts_json"])
        assert len(verdicts) == 10
        assert verdicts[1]["detected"] is True
        assert verdicts[1]["instances"][0]["start_time_sec"] == 1.0
        assert verdicts[0]["detected"] is False
        definitions = json.loads(context_vars["event_definitions_json"])
        assert len(definitions) == 10

    def test_quarantine_writes_to_subdirectory(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data(ungrounded=(1,)))
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(1,)
        )

        result = step._execute(context)

        assert result == tmp_path / "quarantine" / "02_Event_129_1748049879151_1.json"
        assert result.exists()
        assert not (tmp_path / "02_Event_129_1748049879151_1.json").exists()

    def test_vlm_call_failure_is_fail_open(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(error=RuntimeError("API down"))
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(_SftSystemConfig(sft_label_output_dir=str(tmp_path)))

        result = step._execute(context)

        assert result is None
        assert list(tmp_path.rglob("*.json")) == []

    def test_unparseable_response_is_fail_open(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data(), success=False)
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(_SftSystemConfig(sft_label_output_dir=str(tmp_path)))

        result = step._execute(context)

        assert result is None
        assert list(tmp_path.rglob("*.json")) == []

    def test_fatal_api_error_propagates(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(error=FatalAPIError("quota exhausted"))
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(_SftSystemConfig(sft_label_output_dir=str(tmp_path)))

        with pytest.raises(FatalAPIError):
            step._execute(context)

        assert list(tmp_path.rglob("*.json")) == []

    def test_guard_skips_when_no_event_results(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data())
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(_SftSystemConfig(sft_label_output_dir=str(tmp_path)))
        context.event_results = {}

        assert step._execute(context) is None
        assert engine.calls == []

    def test_guard_skips_when_no_keyframes(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data())
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), with_keyframes=False
        )

        assert step._execute(context) is None
        assert engine.calls == []

    def test_guard_skips_when_output_dir_missing(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        """A config object lacking sft_label_output_dir (legacy/partial) skips."""
        engine = _MockVLMEngine(response=_make_resp_data())
        step = SftLabelRewriteStep(config_manager, engine)
        config = SystemConfig()
        del config.__dict__["sft_label_output_dir"]
        context = _make_context(config)

        assert step._execute(context) is None
        assert engine.calls == []
        assert list(tmp_path.rglob("*.json")) == []

    def test_missing_template_is_fail_open(self, tmp_path: Path) -> None:
        config_manager = ConfigManager("./traffic_analyzer/config")
        config_manager.load_all()
        engine = _MockVLMEngine(response=_make_resp_data())
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(1,)
        )

        with patch.object(
            config_manager,
            "get_prompt_template",
            side_effect=KeyError("sft_label_rewrite"),
        ):
            result = step._execute(context)

        assert result is None
        assert engine.calls == []
