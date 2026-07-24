"""Unit tests for the SFT label rewrite step.

Covers:
- "action 即 event_id" 语义（event_id 全局采用标注文档 v4.5 编号，无映射表）。
- Pure helpers: :func:`build_description`, :func:`build_sample`,
  :func:`find_ungrounded_positive_event_ids`, :func:`write_sample`.
- :class:`traffic_analyzer.core.sft_label_rewrite.SftLabelRewriteStep`
  (guards, success path, quarantine gate, fail-open semantics).

[文件说明]
作用:测试 SFT 标签重写步骤及纯函数,覆盖 "action 即 event_id" 语义、样本构造、隔离闸门与 fail-open 行为。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/core/sft_label_rewrite.py(被测模块)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.sft_label_rewrite import (
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

# 全局 event_id = 标注文档 v4.5 的 action 编号（9 = 正常占位，无对应事件）。
_NAME_ZH: Dict[int, str] = {
    1: "违法停车",
    2: "应急车道占用",
    3: "交通事故",
    4: "高速公路行人出现",
    5: "摩托车出现",
    6: "拥堵",
    7: "道路施工",
    8: "车辆逆行/倒车",
    10: "抛洒物",
    11: "实线变道",
}

_EVENT_IDS: List[int] = sorted(_NAME_ZH)


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
        for eid in _EVENT_IDS
    ]


def _make_event_results(detected_ids: tuple = ()) -> Dict[int, EventResult]:
    results: Dict[int, EventResult] = {}
    for eid in _EVENT_IDS:
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
    present_ids: tuple = (2,),
    ungrounded: tuple = (),
) -> Dict[str, Any]:
    thoughts: List[Dict[str, Any]] = []
    for eid in _EVENT_IDS:
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
    detected_ids: tuple = (2,),
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
# "action 即 event_id" tests
# ---------------------------------------------------------------------------


class TestActionEqualsEventId:
    def test_action_list_equals_sorted_detected_ids(self) -> None:
        """event_id 全局采用 v4.5 编号，action 列表即排序后的 detected event_id。"""
        sample = build_sample(
            _make_resp_data(present_ids=(2, 10, 11)),
            _make_event_results(detected_ids=(11, 2, 10)),
            _make_categories(),
            _make_video_meta(),
        )

        assert sample["action"] == [2, 10, 11]

    def test_class_lines_use_event_id_directly(self) -> None:
        """classN 行的 N 直接等于 event_id；9 为正常占位，永不出现在结论中。"""
        desc = build_description(
            _make_resp_data(),
            _make_event_results(detected_ids=(10, 11)),
            _make_categories(),
        )

        answer = desc.split("<answer>\n", 1)[1]
        assert "class10: 抛洒物" in answer
        assert "class11: 实线变道" in answer
        assert "class9" not in answer


# ---------------------------------------------------------------------------
# build_description tests
# ---------------------------------------------------------------------------


class TestBuildDescription:
    def test_think_covers_all_ten_categories_in_fixed_order(self) -> None:
        categories = _make_categories()
        desc = build_description(
            _make_resp_data(), _make_event_results(detected_ids=(2,)), categories
        )

        positions = [desc.index(f"{_NAME_ZH[eid]}：") for eid in _EVENT_IDS]
        assert positions == sorted(positions)
        assert "【" not in desc

    def test_answer_scene_elements_first_conclusion_last(self) -> None:
        categories = _make_categories()
        desc = build_description(
            _make_resp_data(), _make_event_results(detected_ids=(2, 3)), categories
        )

        assert desc.startswith("<think>\n")
        assert "\n</think>\n<answer>\n" in desc
        assert desc.endswith("\n</answer>")
        answer = desc.split("<answer>\n", 1)[1]
        assert "class2: 应急车道占用" in answer
        assert "class3: 交通事故" in answer
        # Order: 天气/时间/场景 first, 最终结论 last.
        i_weather = answer.index("天气：晴天")
        i_time = answer.index("时间：白天")
        i_scene = answer.index("场景：高速公路双向主路场景")
        i_conclusion = answer.index("最终结论")
        assert i_weather < i_time < i_scene < i_conclusion

    def test_no_detection_conclusion_and_missing_thought_fallback(self) -> None:
        categories = _make_categories()
        resp_data = _make_resp_data(present_ids=())
        resp_data["event_thoughts"] = []  # VLM omitted all thoughts
        desc = build_description(resp_data, _make_event_results(), categories)

        assert "未检出任何事件" in desc
        assert "class" not in desc.split("<answer>\n", 1)[1]
        # Missing thinking for an undetected event falls back to 未发现。
        assert desc.count("未发现。") == 10

    def test_inactive_category_produces_no_think_section(self) -> None:
        """is_active=false 的类别不在 <think> 中生成段落(其余类别不受影响)。"""
        categories = _make_categories()
        categories[8] = categories[8].model_copy(update={"is_active": False})
        desc = build_description(
            _make_resp_data(), _make_event_results(), categories
        )

        think = desc.split("</think>", 1)[0]
        assert f"{_NAME_ZH[10]}：" not in think
        assert f"{_NAME_ZH[8]}：" in think
        assert f"{_NAME_ZH[11]}：" in think


# ---------------------------------------------------------------------------
# build_sample tests
# ---------------------------------------------------------------------------


class TestBuildSample:
    def test_field_types_and_values_per_key(self) -> None:
        sample = build_sample(
            _make_resp_data(),
            _make_event_results(detected_ids=(2, 10)),
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
        assert sample["action"] == [2, 10]  # action 即 event_id：应急车道占用、抛洒物
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
            _make_event_results(detected_ids=(2,)),
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
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(2,)
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
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(2,)
        )

        step._execute(context)

        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call["template_id"] == "sft_label_rewrite"
        assert call["images"]  # raw frames selected, non-empty
        assert call["response_schema"] is not None
        context_vars = call["context_vars"]
        active_count = sum(
            1 for c in config_manager.get_event_categories() if c.is_active
        )
        verdicts = json.loads(context_vars["verdicts_json"])
        assert len(verdicts) == active_count  # 未激活类别不进入 prompt
        assert all(v["event_id"] not in (10, 11) for v in verdicts)
        assert verdicts[1]["detected"] is True
        assert verdicts[1]["instances"][0]["start_time_sec"] == 1.0
        assert verdicts[0]["detected"] is False
        definitions = json.loads(context_vars["event_definitions_json"])
        assert len(definitions) == active_count

    def test_quarantine_writes_to_subdirectory(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data(ungrounded=(2,)))
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(2,)
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
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(2,)
        )

        with patch.object(
            config_manager,
            "get_prompt_template",
            side_effect=KeyError("sft_label_rewrite"),
        ):
            result = step._execute(context)

        assert result is None
        assert engine.calls == []
