"""Unit tests for the ExpertAgent reflection/consistency-check layer.

Covers:
- :func:`traffic_analyzer.utils.event_detection.reflect_expert_candidate`.
- :class:`traffic_analyzer.core.expert_agent.ExpertAgent` wiring when reflection
  is enabled or disabled.

[文件说明]
作用:测试专家反思/一致性检查层 reflect_expert_candidate 及 ExpertAgent 在反思开关启停下的接线行为。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/utils/event_detection.py、traffic_analyzer/core/expert_agent.py(被测模块)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.expert_agent import ExpertAgent
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    Keyframe,
    KeyframeSequence,
    SystemConfig,
    VideoMetadata,
)
from traffic_analyzer.utils.event_detection import (
    parse_expert_response,
    reflect_expert_candidate,
    select_event_images,
)


# ---------------------------------------------------------------------------
# Helpers / mocks
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal stand-in for :class:`traffic_analyzer.models.schemas.LLMResponse`."""

    def __init__(
        self,
        parsed_data: Dict[str, Any],
        success: bool = True,
        raw_text: Optional[str] = None,
    ) -> None:
        self.success = success
        self.parsed_data = parsed_data
        self.raw_text = raw_text if raw_text is not None else str(parsed_data)
        self.model = "mock"
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.latency_ms = 0.0
        self.retry_count = 0


class _MockVLMEngine:
    """Mock VLM engine that returns queued responses and records calls."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        self._responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []

    def render_prompt(
        self,
        template: Any,
        context_vars: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        return (
            getattr(template, "system_prompt", ""),
            getattr(template, "user_prompt", ""),
        )

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
        if not self._responses:
            raise IndexError("No queued response")
        response = self._responses.pop(0)
        return _MockResponse(response)


class _MockTemplate:
    def __init__(self, template_id: str = "expert_response_reflection") -> None:
        self.template_id = template_id
        self.system_prompt = "system"
        self.user_prompt = "user"


def _make_category(event_id: int = 2) -> EventCategory:
    return EventCategory(
        event_id=event_id,
        event_code="B",
        name="Emergency Lane Occupancy",
        name_zh="应急车道占用",
        description="Vehicle in emergency lane.",
        definition="机动车占用高速公路应急车道或路肩的行为。",
    )


def _make_candidate(
    detected: bool,
    summary: str,
    instances: Optional[List[EventInstance]] = None,
) -> EventCandidate:
    return EventCandidate(
        event_id=2,
        event_name="应急车道占用",
        detected=detected,
        summary=summary,
        instances=instances or [],
    )


# ---------------------------------------------------------------------------
# reflect_expert_candidate tests
# ---------------------------------------------------------------------------


class TestReflectExpertCandidate:
    def test_reflection_corrects_false_to_true_when_summary_describes_event(
        self,
    ) -> None:
        """When detected=false but summary describes a detection, reflection flips it."""
        category = _make_category()
        candidate = _make_candidate(
            detected=False,
            summary="检测到白色SUV占用应急车道。",
            instances=[
                EventInstance(
                    event_id=2,
                    event_name="应急车道占用",
                    start_time_sec=1.0,
                    end_time_sec=2.0,
                    evidence_frames=[1],
                    description="白色SUV在应急车道内静止。",
                    reasoning="车辆完全位于应急车道实线外侧，构成占用。",
                )
            ],
        )
        engine = _MockVLMEngine(
            responses=[
                {
                    "detected": True,
                    "summary": "检测到白色SUV占用应急车道。",
                    "instances": [
                        {
                            "start_time_sec": 1.0,
                            "end_time_sec": 2.0,
                            "evidence_frames": [1],
                            "description": "白色SUV在应急车道内静止。",
                            "reasoning": "车辆完全位于应急车道实线外侧，构成占用。",
                        }
                    ],
                }
            ]
        )
        template = _MockTemplate()

        result = reflect_expert_candidate(candidate, category, engine, template)

        assert result.detected is True
        assert result.summary == "检测到白色SUV占用应急车道。"
        assert len(result.instances) == 1
        assert result.instances[0].event_id == category.event_id
        assert result.instances[0].event_name == category.name_zh
        assert engine.calls[0]["images"] == []
        assert engine.calls[0]["template_id"] == "expert_response_reflection"

    def test_reflection_keeps_consistent_candidate(self) -> None:
        """When the candidate is already consistent, reflection returns it unchanged."""
        category = _make_category()
        candidate = _make_candidate(
            detected=False,
            summary="未检测到应急车道占用，所有车辆均在正常车道内行驶。",
        )
        engine = _MockVLMEngine(
            responses=[
                {
                    "detected": False,
                    "summary": "未检测到应急车道占用，所有车辆均在正常车道内行驶。",
                    "instances": [],
                }
            ]
        )
        template = _MockTemplate()

        result = reflect_expert_candidate(candidate, category, engine, template)

        assert result.detected is False
        assert result.summary == candidate.summary
        assert result.instances == []

    def test_reflection_fail_open_on_vlm_error(self) -> None:
        """If the reflection VLM call fails, the original candidate is returned."""
        category = _make_category()
        candidate = _make_candidate(detected=False, summary="summary")
        engine = _MockVLMEngine(responses=[])
        template = _MockTemplate()

        result = reflect_expert_candidate(candidate, category, engine, template)

        assert result is candidate
        assert result.detected is False

    def test_reflection_fail_open_on_invalid_parsed_data(self) -> None:
        """If the reflection response parses to invalid data, return original."""
        category = _make_category()
        candidate = _make_candidate(detected=False, summary="summary")
        engine = _MockVLMEngine(responses=[{}])  # missing "detected"
        template = _MockTemplate()

        result = reflect_expert_candidate(candidate, category, engine, template)

        assert result is candidate
        assert result.detected is False

    def test_reflection_string_false_overrides_detected_to_false(self) -> None:
        """Regression: bool('false') is True; a string detected field must be
        normalized strictly so 'false' does not keep the candidate positive."""
        category = _make_category()
        candidate = _make_candidate(detected=True, summary="检测到白色SUV占用应急车道。")
        engine = _MockVLMEngine(
            responses=[{"detected": "false", "summary": "未检测到应急车道占用。"}]
        )
        template = _MockTemplate()

        result = reflect_expert_candidate(candidate, category, engine, template)

        assert result.detected is False

    def test_reflection_tolerates_malformed_instance_fields(self) -> None:
        """Malformed reflection instances are dropped; null numeric fields fall
        back to defaults instead of raising TypeError."""
        category = _make_category()
        candidate = _make_candidate(detected=True, summary="检测到白色SUV占用应急车道。")
        engine = _MockVLMEngine(
            responses=[
                {
                    "detected": True,
                    "summary": "检测到白色SUV占用应急车道。",
                    "instances": [
                        None,
                        {
                            "start_time_sec": None,
                            "end_time_sec": None,
                            "evidence_frames": None,
                            "description": "白色SUV在应急车道内静止。",
                            "reasoning": "车辆完全位于应急车道实线外侧，构成占用。",
                        },
                    ],
                }
            ]
        )
        template = _MockTemplate()

        result = reflect_expert_candidate(candidate, category, engine, template)

        assert result.detected is True
        assert len(result.instances) == 1
        assert result.instances[0].start_time_sec == 0.0
        assert result.instances[0].end_time_sec == 0.0
        assert result.instances[0].evidence_frames == []


# ---------------------------------------------------------------------------
# parse_expert_response tests
# ---------------------------------------------------------------------------


class TestParseExpertResponse:
    def test_malformed_instances_are_dropped_not_fatal(self) -> None:
        """A single malformed instance must not crash parsing or drop the event."""
        category = _make_category()
        response = _MockResponse(
            {
                "detected": True,
                "summary": "检测到白色SUV占用应急车道。",
                "instances": [
                    None,
                    "garbage",
                    {
                        "start_time_sec": None,
                        "end_time_sec": "abc",
                        "evidence_frames": None,
                        "description": "时间字段畸形",
                        "reasoning": "车辆占用应急车道。",
                    },
                    {
                        "start_time_sec": 1.0,
                        "end_time_sec": 2.0,
                        "evidence_frames": [1, "x", 2.0],
                        "description": "白色SUV在应急车道内静止。",
                        "reasoning": "车辆完全位于应急车道实线外侧，构成占用。",
                    },
                ],
            }
        )

        candidate = parse_expert_response(response, category)

        assert candidate.detected is True
        assert len(candidate.instances) == 2
        # Malformed numeric fields fall back to defaults; null evidence list → [].
        assert candidate.instances[0].start_time_sec == 0.0
        assert candidate.instances[0].end_time_sec == 0.0
        assert candidate.instances[0].evidence_frames == []
        # Non-numeric entries are filtered out of evidence_frames.
        assert candidate.instances[1].evidence_frames == [1, 2]

    @pytest.mark.parametrize(
        ("raw_detected", "expected"),
        [
            (True, True),
            ("true", True),
            (" TRUE ", True),
            ("false", False),
            ("False", False),
            ("yes", False),
            (1, False),
            (None, False),
        ],
    )
    def test_detected_strict_bool_normalization(
        self, raw_detected: Any, expected: bool
    ) -> None:
        """Only True/'true' (case-insensitive, stripped) counts as detected."""
        category = _make_category()
        response = _MockResponse(
            {
                "detected": raw_detected,
                "summary": "检测到白色SUV占用应急车道。",
                "instances": [],
            }
        )

        candidate = parse_expert_response(response, category)

        assert candidate.detected is expected


# ---------------------------------------------------------------------------
# select_event_images tests
# ---------------------------------------------------------------------------


class TestSelectEventImages:
    def test_max_frames_one_returns_single_frame(self) -> None:
        """VLM_MAX_FRAMES=1 must not raise ZeroDivisionError."""
        context = _make_analysis_context(num_frames=5)

        images = select_event_images(context, 1)

        assert len(images) == 1


# ---------------------------------------------------------------------------
# ExpertAgent wiring tests
# ---------------------------------------------------------------------------


@pytest.fixture
def config_manager() -> ConfigManager:
    manager = ConfigManager("./traffic_analyzer/config")
    manager.load_all()
    return manager


@pytest.fixture
def illegal_parking_category(config_manager: ConfigManager) -> EventCategory:
    """Use event_id=1 because it does not enable far-distance enhancement."""
    categories = config_manager.get_event_categories()
    return next(c for c in categories if c.event_id == 1)


def _make_analysis_context(
    num_frames: int = 1,
    reflection_enabled: bool = True,
) -> AnalysisContext:
    return AnalysisContext(
        config=SystemConfig(expert_enable_reflection=reflection_enabled),
        video_meta=VideoMetadata(
            file_path="/tmp/test_video.mp4",
            file_name="test_video.mp4",
            duration_sec=float(num_frames),
            fps=1.0,
            total_frames=num_frames,
            width=1280,
            height=720,
        ),
        keyframes=KeyframeSequence(
            coarse_frames=[
                Keyframe(frame_id=i, timestamp_sec=float(i), image_data=b"fake")
                for i in range(num_frames)
            ],
            precision_frames=[],
        ),
    )


def _make_agent(
    config_manager: ConfigManager,
    category: EventCategory,
    responses: List[Dict[str, Any]],
) -> tuple[ExpertAgent, _MockVLMEngine]:
    engine = _MockVLMEngine(responses=responses)
    agent = ExpertAgent(
        category=category,
        vlm_engine=engine,
        config_manager=config_manager,
    )
    return agent, engine


class TestExpertAgentReflectionWiring:
    def test_reflection_disabled_skips_reflection_call(
        self,
        config_manager: ConfigManager,
        illegal_parking_category: EventCategory,
    ) -> None:
        """When expert_enable_reflection=false, no reflection VLM call is made."""
        template_id = illegal_parking_category.prompt_template_id
        agent, engine = _make_agent(
            config_manager,
            illegal_parking_category,
            responses=[
                {
                    "detected": False,
                    "summary": "未检测到违法停车。",
                    "instances": [],
                }
            ],
        )
        context = _make_analysis_context(reflection_enabled=False)

        candidate = agent.detect(context)

        assert candidate.detected is False
        assert all(
            c["template_id"] != "expert_response_reflection" for c in engine.calls
        )

    def test_reflection_enabled_makes_reflection_call(
        self,
        config_manager: ConfigManager,
        illegal_parking_category: EventCategory,
    ) -> None:
        """When expert_enable_reflection=true, the reflection template is called."""
        template_id = illegal_parking_category.prompt_template_id
        agent, engine = _make_agent(
            config_manager,
            illegal_parking_category,
            responses=[
                {
                    "detected": False,
                    "summary": "检测到白色轿车违停在应急车道。",
                    "instances": [
                        {
                            "start_time_sec": 1.0,
                            "end_time_sec": 2.0,
                            "evidence_frames": [1],
                            "description": "白色轿车静止在应急车道。",
                            "reasoning": "车辆静止且位于应急车道内。",
                        }
                    ],
                },
                {
                    "detected": True,
                    "summary": "检测到白色轿车违停在应急车道。",
                    "instances": [
                        {
                            "start_time_sec": 1.0,
                            "end_time_sec": 2.0,
                            "evidence_frames": [1],
                            "description": "白色轿车静止在应急车道。",
                            "reasoning": "车辆静止且位于应急车道内。",
                        }
                    ],
                },
            ],
        )
        context = _make_analysis_context(reflection_enabled=True)

        candidate = agent.detect(context)

        assert candidate.detected is True
        reflection_calls = [
            c for c in engine.calls if c["template_id"] == "expert_response_reflection"
        ]
        assert len(reflection_calls) == 1
        assert reflection_calls[0]["images"] == []

    def test_reflection_template_missing_returns_original(
        self,
        config_manager: ConfigManager,
        illegal_parking_category: EventCategory,
    ) -> None:
        """If the reflection template is missing, the original candidate is returned."""
        template_id = illegal_parking_category.prompt_template_id
        agent, engine = _make_agent(
            config_manager,
            illegal_parking_category,
            responses=[
                {
                    "detected": False,
                    "summary": "未检测到违法停车。",
                    "instances": [],
                }
            ],
        )
        context = _make_analysis_context(reflection_enabled=True)

        with patch.object(
            config_manager,
            "get_prompt_template",
            side_effect=KeyError("expert_response_reflection"),
        ):
            candidate = agent.detect(context)

        assert candidate.detected is False
