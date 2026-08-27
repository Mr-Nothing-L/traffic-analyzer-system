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
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.sft_label_rewrite import (
    _SFT_REWRITE_RESPONSE_SCHEMA,
    _normalize_attributes,
    _positive_event_details,
    _skeleton_sentence,
    _validate_attr_mentions,
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

# present=true 事件的结构化属性(键/值均取自 event_options.yaml 封闭枚举;
# 实线变道(11) 已定义属性组,此处模拟改写模型未给出属性值,归一后为 null)。
_PRESENT_ATTRS: Dict[int, Dict[str, Any]] = {
    1: {"lane_type": "应急车道", "direction": "来向", "vehicle_type": "工程车"},
    2: {"lane_type": "应急车道", "direction": "去向", "vehicle_type": "小型车"},
    3: {"lane_type": "行车道", "direction": "来向", "vehicle_type": "货车"},
    4: {"person_type": "行人", "direction": "去向"},
    5: {"direction": "来向", "non_motor_type": "摩托车"},
    6: {"direction": "去向", "scope": "多车道"},
    7: {"direction": "来向", "work_elements": ["交通锥/隔离栏", "施工车辆"]},
    8: {"lane_type": "行车道", "direction": "去向", "vehicle_type": "货车"},
    10: {"direction": "来向", "object_type": "其他"},
    11: {},
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


def _make_present_item(eid: int) -> Dict[str, Any]:
    """present=true 条目:attributes/detail/attr_mentions(detail 由属性值拼成,
    保证 attr_mentions 全部是 detail 的逐字子串)。"""
    attrs = _PRESENT_ATTRS[eid]
    flat: List[str] = []
    for value in attrs.values():
        flat.extend(value if isinstance(value, list) else [value])
    detail = "、".join(flat) + "，主体目标在原始帧中清晰可辨。"
    mentions: Dict[str, List[str]] = {}
    for key, value in attrs.items():
        values = value if isinstance(value, list) else [value]
        hits = [v for v in values if v in detail]
        if hits:
            mentions[key] = hits
    return {
        "event_id": eid,
        "present": True,
        "attributes": dict(attrs),
        "detail": detail,
        "attr_mentions": mentions,
    }


def _make_resp_data(
    present_ids: tuple = (2,),
    ungrounded: tuple = (),
) -> Dict[str, Any]:
    thoughts: List[Dict[str, Any]] = []
    for eid in _EVENT_IDS:
        if eid in present_ids:
            thoughts.append(_make_present_item(eid))
        else:
            thoughts.append(
                {
                    "event_id": eid,
                    "present": False,
                    "thinking": f"未发现{_NAME_ZH[eid]}。画面中无相关迹象。",
                }
            )
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
            "event_attributes",
            "attr_mentions",
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
        assert isinstance(sample["event_attributes"], dict)
        assert isinstance(sample["attr_mentions"], dict)

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
        # ungrounded 指向未检出事件不隔离;event 1 present=true,避免触发
        # present=false ∧ detected 的漏报隔离规则(该规则有独立测试覆盖)。
        resp_data = _make_resp_data(present_ids=(1, 2), ungrounded=(2,))
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
        # 结构化属性契约:detected 且 present 的事件进入 event_attributes/attr_mentions
        assert loaded["event_attributes"] == {
            "2": {"lane_type": "应急车道", "direction": "去向", "vehicle_type": "小型车"}
        }
        assert loaded["attr_mentions"] == {
            "2": {
                "lane_type": ["应急车道"],
                "direction": ["去向"],
                "vehicle_type": ["小型车"],
            }
        }
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
        inactive_ids = {
            c.event_id for c in config_manager.get_event_categories() if not c.is_active
        }
        assert all(v["event_id"] not in inactive_ids for v in verdicts)
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


# ---------------------------------------------------------------------------
# Response schema shape tests
# ---------------------------------------------------------------------------


class TestSchemaShape:
    def test_item_level_requires_only_event_id_and_present(self) -> None:
        """present 两个分支(阴性 thinking / 阳性 attributes+detail+attr_mentions)
        共用同一 items schema,条目级 required 只约束 event_id/present。"""
        item_schema = _SFT_REWRITE_RESPONSE_SCHEMA["properties"]["event_thoughts"][
            "items"
        ]
        assert item_schema["required"] == ["event_id", "present"]
        props = item_schema["properties"]
        for key in ("thinking", "attributes", "detail", "attr_mentions"):
            assert key in props

    def test_top_level_required_unchanged(self) -> None:
        assert _SFT_REWRITE_RESPONSE_SCHEMA["required"] == [
            "weather",
            "time_of_day",
            "scene",
            "event_thoughts",
            "ungrounded_event_ids",
        ]


# ---------------------------------------------------------------------------
# Attribute normalization tests (alias → enum, invalid → null + warning)
# ---------------------------------------------------------------------------


class TestNormalizeAttributes:
    def test_alias_maps_to_closed_enum(self) -> None:
        attrs = _normalize_attributes(
            1,
            {"lane_type": "应急车道", "direction": "对向", "vehicle_type": "工程车辆"},
        )

        assert attrs == {
            "lane_type": "应急车道",
            "direction": "来向",
            "vehicle_type": "工程车",
        }

    def test_invalid_value_becomes_none_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            attrs = _normalize_attributes(
                1,
                {"lane_type": "紧急停车带", "direction": "来向", "vehicle_type": "工程车"},
            )

        assert attrs["lane_type"] is None
        assert attrs["direction"] == "来向"
        assert "ATTR_NORMALIZE" in caplog.text

    def test_unknown_key_dropped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            attrs = _normalize_attributes(
                4, {"person_type": "行人", "direction": "去向", "color": "红色"}
            )

        assert set(attrs.keys()) == {"person_type", "direction"}
        assert "ATTR_UNKNOWN_KEY" in caplog.text

    def test_missing_required_group_defaults_to_none(self) -> None:
        attrs = _normalize_attributes(2, {"direction": "去向"})

        assert attrs == {"lane_type": None, "direction": "去向", "vehicle_type": None}

    def test_multi_normalized_deduped_and_in_option_order(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            attrs = _normalize_attributes(
                7,
                {
                    "direction": "来向",
                    "work_elements": ["锥桶", "施工车辆", "锥桶", "外星物质"],
                },
            )

        # 按 event_options.yaml options 声明顺序输出(施工车辆 在 交通锥/隔离栏 之前)
        assert attrs["work_elements"] == ["施工车辆", "交通锥/隔离栏"]
        assert "ATTR_NORMALIZE" in caplog.text  # 外星物质 被丢弃

    def test_multi_missing_defaults_to_empty_list(self) -> None:
        attrs = _normalize_attributes(7, {"direction": "去向"})

        assert attrs["work_elements"] == []


# ---------------------------------------------------------------------------
# attr_mentions substring validation tests
# ---------------------------------------------------------------------------


class TestAttrMentionsValidation:
    def test_exact_substrings_kept_others_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        detail = "来向一侧应急车道内停有一辆黄色工程作业车，车身黄色。"
        with caplog.at_level(logging.WARNING):
            mentions = _validate_attr_mentions(
                1,
                detail,
                {
                    "vehicle_type": ["黄色工程作业车", "工程作业车", "绿色工程车"],
                    "lane_type": ["应急车道"],
                    "direction": ["去向"],  # 不在 detail 中
                },
            )

        assert mentions == {
            "vehicle_type": ["黄色工程作业车", "工程作业车"],
            "lane_type": ["应急车道"],
        }
        assert "MENTION_NOT_SUBSTRING" in caplog.text

    def test_unknown_attr_key_dropped(self) -> None:
        mentions = _validate_attr_mentions(
            4, "去向一侧出现一名行人。", {"vehicle_type": ["行人"], "person_type": ["行人"]}
        )

        assert mentions == {"person_type": ["行人"]}

    def test_non_dict_or_non_list_returns_empty(self) -> None:
        assert _validate_attr_mentions(1, "detail", "bad") == {}
        assert _validate_attr_mentions(1, "detail", {"lane_type": "应急车道"}) == {}

    def test_duplicate_mentions_deduped(self) -> None:
        mentions = _validate_attr_mentions(
            2, "应急车道内有小车。", {"lane_type": ["应急车道", "应急车道"]}
        )

        assert mentions == {"lane_type": ["应急车道"]}


# ---------------------------------------------------------------------------
# Multi-select attr_mentions contract tests (nested option → substrings object)
# ---------------------------------------------------------------------------


class TestMultiAttrMentionsNested:
    _DETAIL = (
        "来向一侧道路施工，作业区内停有一辆黄色工程车，周围摆放多个锥桶，"
        "还有身穿橙色工作服的人员在指挥交通。"
    )

    def test_nested_object_accepted_for_multi_group(self) -> None:
        mentions = _validate_attr_mentions(
            7,
            self._DETAIL,
            {
                "direction": ["来向"],
                "work_elements": {
                    "施工车辆": ["黄色工程车"],
                    "交通锥/隔离栏": ["锥桶"],
                    "施工人员": ["身穿橙色工作服的人员"],
                },
            },
        )

        assert mentions == {
            "direction": ["来向"],
            "work_elements": {
                "施工车辆": ["黄色工程车"],
                "交通锥/隔离栏": ["锥桶"],
                "施工人员": ["身穿橙色工作服的人员"],
            },
        }

    def test_legacy_flat_array_still_accepted_for_multi_group(self) -> None:
        mentions = _validate_attr_mentions(
            7,
            self._DETAIL,
            {"work_elements": ["黄色工程车", "锥桶"]},
        )

        assert mentions == {"work_elements": ["黄色工程车", "锥桶"]}

    def test_bad_option_name_dropped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            mentions = _validate_attr_mentions(
                7,
                self._DETAIL,
                {
                    "work_elements": {
                        "工程车": ["黄色工程车"],  # 别名/非枚举原文,非法
                        "施工车辆": ["黄色工程车"],
                    }
                },
            )

        assert mentions == {"work_elements": {"施工车辆": ["黄色工程车"]}}
        assert "MENTION_UNKNOWN_OPTION" in caplog.text

    def test_non_substring_string_dropped_in_nested(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            mentions = _validate_attr_mentions(
                7,
                self._DETAIL,
                {
                    "work_elements": {
                        "施工车辆": ["黄色工程车", "绿色压路机"],
                        "施工标志牌": ["施工标志牌"],  # 不在 detail 中
                    }
                },
            )

        assert mentions == {"work_elements": {"施工车辆": ["黄色工程车"]}}
        assert "MENTION_NOT_SUBSTRING" in caplog.text

    def test_single_select_group_rejects_nested_object(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            mentions = _validate_attr_mentions(
                7, self._DETAIL, {"direction": {"来向": ["来向"]}}
            )

        assert mentions == {}
        assert "MENTION_BAD_SHAPE" in caplog.text

    def test_build_sample_emits_nested_shape_for_multi_group(self) -> None:
        resp_data = _make_resp_data(present_ids=(7,))
        for item in resp_data["event_thoughts"]:
            if item["event_id"] == 7:
                item["detail"] = self._DETAIL
                item["attr_mentions"] = {
                    "direction": ["来向"],
                    "work_elements": {
                        "施工车辆": ["黄色工程车"],
                        "交通锥/隔离栏": ["锥桶"],
                        "施工人员": ["身穿橙色工作服的人员"],
                    },
                }
        sample = build_sample(
            resp_data,
            _make_event_results(detected_ids=(7,)),
            _make_categories(),
            _make_video_meta(),
        )

        assert sample["attr_mentions"] == {
            "7": {
                "direction": ["来向"],
                "work_elements": {
                    "施工车辆": ["黄色工程车"],
                    "交通锥/隔离栏": ["锥桶"],
                    "施工人员": ["身穿橙色工作服的人员"],
                },
            }
        }


# ---------------------------------------------------------------------------
# Skeleton sentence tests (mirrors JS SFT_SKELETON_TEMPLATES semantics)
# ---------------------------------------------------------------------------


class TestSkeletonSentence:
    def test_full_attributes_render_all_clauses(self) -> None:
        sentence = _skeleton_sentence(
            1, {"lane_type": "应急车道", "direction": "来向", "vehicle_type": "工程车"}
        )

        assert sentence == "来向一侧应急车道内停有一辆工程车"

    def test_null_clauses_omitted(self) -> None:
        sentence = _skeleton_sentence(
            2, {"lane_type": None, "direction": "去向", "vehicle_type": None}
        )

        assert sentence == "去向一侧"

    def test_multi_values_joined(self) -> None:
        sentence = _skeleton_sentence(
            7,
            {"direction": "来向", "work_elements": ["施工车辆", "交通锥/隔离栏"]},
        )

        assert sentence == "来向一侧道路施工,现场有施工车辆、交通锥/隔离栏"

    def test_event_without_template_returns_empty(self) -> None:
        assert _skeleton_sentence(11, {}) == ""


# ---------------------------------------------------------------------------
# build_description skeleton+detail composition / negative path tests
# ---------------------------------------------------------------------------


class TestBuildDescriptionAttributes:
    def test_present_event_think_is_skeleton_plus_detail(self) -> None:
        desc = build_description(
            _make_resp_data(present_ids=(2,)),
            _make_event_results(detected_ids=(2,)),
            _make_categories(),
        )

        think = desc.split("</think>", 1)[0]
        assert (
            "应急车道占用：去向一侧应急车道内有小型车占用。"
            "应急车道、去向、小型车，主体目标在原始帧中清晰可辨。"
        ) in think

    def test_null_attributes_omit_skeleton_clauses(self) -> None:
        resp_data = _make_resp_data(present_ids=(2,))
        for item in resp_data["event_thoughts"]:
            if item["event_id"] == 2:
                item["attributes"] = {
                    "lane_type": None,
                    "direction": "去向",
                    "vehicle_type": None,
                }
        desc = build_description(
            resp_data, _make_event_results(detected_ids=(2,)), _make_categories()
        )

        assert "应急车道占用：去向一侧。" in desc

    def test_alias_in_response_normalized_before_render(self) -> None:
        resp_data = _make_resp_data(present_ids=(1,))
        for item in resp_data["event_thoughts"]:
            if item["event_id"] == 1:
                item["attributes"]["vehicle_type"] = "工程车辆"
        desc = build_description(
            resp_data, _make_event_results(detected_ids=(1,)), _make_categories()
        )

        assert "违法停车：来向一侧应急车道内停有一辆工程车。" in desc

    def test_negative_path_unchanged(self) -> None:
        """present=false 事件的 think 段仍是改写模型的 thinking 原文。"""
        desc = build_description(
            _make_resp_data(present_ids=(2,)),
            _make_event_results(detected_ids=(2,)),
            _make_categories(),
        )

        assert "违法停车：未发现违法停车。画面中无相关迹象。" in desc
        assert "交通事故：未发现交通事故。画面中无相关迹象。" in desc


# ---------------------------------------------------------------------------
# build_sample event_attributes / attr_mentions contract tests
# ---------------------------------------------------------------------------


class TestBuildSampleAttributes:
    def test_event_attributes_and_attr_mentions_shape(self) -> None:
        sample = build_sample(
            _make_resp_data(present_ids=(2,)),
            _make_event_results(detected_ids=(2,)),
            _make_categories(),
            _make_video_meta(),
        )

        assert sample["event_attributes"] == {
            "2": {"lane_type": "应急车道", "direction": "去向", "vehicle_type": "小型车"}
        }
        assert sample["attr_mentions"] == {
            "2": {
                "lane_type": ["应急车道"],
                "direction": ["去向"],
                "vehicle_type": ["小型车"],
            }
        }

    def test_present_but_not_detected_is_excluded(self) -> None:
        sample = build_sample(
            _make_resp_data(present_ids=(2, 3)),
            _make_event_results(detected_ids=(2,)),
            _make_categories(),
            _make_video_meta(),
        )

        assert set(sample["event_attributes"].keys()) == {"2"}
        assert set(sample["attr_mentions"].keys()) == {"2"}

    def test_event_11_null_attributes_included(self) -> None:
        """实线变道(11) 已有属性组:present+detected 时进结构化字段,
        改写模型未给出属性值时归一为 null;无骨架模板,think 段仍只用其 detail。"""
        sample = build_sample(
            _make_resp_data(present_ids=(11,)),
            _make_event_results(detected_ids=(11,)),
            _make_categories(),
            _make_video_meta(),
        )

        assert sample["event_attributes"] == {
            "11": {"lane_type": None, "direction": None, "vehicle_type": None}
        }
        assert sample["attr_mentions"] == {}
        assert "实线变道：，主体目标在原始帧中清晰可辨。" in sample["description"]

    def test_no_positives_emit_empty_dicts(self) -> None:
        sample = build_sample(
            _make_resp_data(present_ids=()),
            _make_event_results(),
            _make_categories(),
            _make_video_meta(),
        )

        assert sample["event_attributes"] == {}
        assert sample["attr_mentions"] == {}

    def test_invalid_mentions_dropped_from_sample(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        resp_data = _make_resp_data(present_ids=(2,))
        for item in resp_data["event_thoughts"]:
            if item["event_id"] == 2:
                item["attr_mentions"] = {
                    "vehicle_type": ["小型车", "一辆根本不存在的字符串"],
                }
        with caplog.at_level(logging.WARNING):
            sample = build_sample(
                resp_data,
                _make_event_results(detected_ids=(2,)),
                _make_categories(),
                _make_video_meta(),
            )

        assert sample["attr_mentions"]["2"]["vehicle_type"] == ["小型车"]
        assert "MENTION_NOT_SUBSTRING" in caplog.text


# ---------------------------------------------------------------------------
# B5a: present=true 但裁决未检出的事件按阴性处理(think 与结论一致)
# ---------------------------------------------------------------------------


class TestPresentButNotDetected:
    def test_think_section_uses_negative_fallback(self) -> None:
        """present=true 的事件若裁决 detected=false,不得拼装骨架+detail
        (否则 think 段给出阳性描述而最终结论不含该事件,样本自相矛盾)。"""
        desc = build_description(
            _make_resp_data(present_ids=(2, 3)),
            _make_event_results(detected_ids=(2,)),
            _make_categories(),
        )

        think = desc.split("</think>", 1)[0]
        # 检出事件 2 仍是骨架+detail。
        assert "应急车道占用：去向一侧应急车道内有小型车占用。" in think
        # 未检出事件 3:无骨架(交通事故骨架含「发生交通事故」)、无 detail,
        # 按阴性回退。
        assert "发生交通事故" not in think
        assert "交通事故：未发现。" in think
        # 结论与 think 一致:不含 class3。
        answer = desc.split("<answer>\n", 1)[1]
        assert "class2: 应急车道占用" in answer
        assert "class3" not in answer


# ---------------------------------------------------------------------------
# B5b: present=false ∧ detected=true(漏报 ungrounded_event_ids)同样隔离
# ---------------------------------------------------------------------------


class TestImplicitUngroundedQuarantine:
    def test_present_false_positive_event_triggers_quarantine(self) -> None:
        """改写模型对检出事件给出 present=false(无法锚定)却漏写
        ungrounded_event_ids 时,样本同样写入 quarantine。"""
        resp_data = _make_resp_data(present_ids=(2,))  # event 1 present=false
        event_results = _make_event_results(detected_ids=(1,))

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == [1]

    def test_present_false_negative_event_does_not_quarantine(self) -> None:
        """present=false 且裁决也未检出:正常阴性,不隔离。"""
        resp_data = _make_resp_data(present_ids=())
        event_results = _make_event_results(detected_ids=())

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == []

    def test_explicit_and_implicit_ungrounded_merged(self) -> None:
        """显式 ungrounded 列表与 present=false 漏报取并集,排序去重。"""
        resp_data = _make_resp_data(present_ids=(2,), ungrounded=(2,))
        event_results = _make_event_results(detected_ids=(1, 2))

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == [1, 2]

    def test_step_quarantines_implicit_ungrounded_sample(
        self, config_manager: ConfigManager, tmp_path: Path
    ) -> None:
        """端到端:漏报 ungrounded_event_ids 的样本写入 quarantine/ 子目录。"""
        engine = _MockVLMEngine(response=_make_resp_data(present_ids=()))
        step = SftLabelRewriteStep(config_manager, engine)
        context = _make_context(
            _SftSystemConfig(sft_label_output_dir=str(tmp_path)), detected_ids=(2,)
        )

        result = step._execute(context)

        assert result == tmp_path / "quarantine" / "02_Event_129_1748049879151_1.json"
        assert result is not None and result.exists()
        assert not (tmp_path / "02_Event_129_1748049879151_1.json").exists()


# ---------------------------------------------------------------------------
# B6: JSON true(bool)不得被当作 event_id 1(True == 1)
# ---------------------------------------------------------------------------


class TestBoolEventIdRejected:
    def test_bool_in_ungrounded_event_ids_ignored(self) -> None:
        resp_data = _make_resp_data(present_ids=(1,))  # event 1 present=true
        resp_data["ungrounded_event_ids"] = [True]
        event_results = _make_event_results(detected_ids=(1,))

        assert find_ungrounded_positive_event_ids(resp_data, event_results) == []

    def test_bool_event_id_in_thoughts_ignored(self) -> None:
        """event_thoughts 里 event_id=true 的条目不得归入 event 1。"""
        resp_data = _make_resp_data(present_ids=())
        resp_data["event_thoughts"].append(
            {
                "event_id": True,
                "present": True,
                "attributes": {},
                "detail": "幻觉细节。",
                "attr_mentions": {},
            }
        )

        assert _positive_event_details(resp_data) == {}
