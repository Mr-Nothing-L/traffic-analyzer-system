"""Integration tests for the far-distance non-motor vehicle enhancement flow."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Union
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.expert_agent import ExpertAgent, _FAR_ENHANCEMENT_OUTPUT_DIR
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    Keyframe,
    KeyframeSequence,
    SystemConfig,
    VideoMetadata,
)


class _MockResponse:
    """Minimal stand-in for :class:`traffic_analyzer.models.schemas.LLMResponse`."""

    def __init__(self, parsed_data: Dict[str, Any]) -> None:
        self.success = True
        self.parsed_data = parsed_data
        self.raw_text = str(parsed_data)
        self.model = "mock"
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.latency_ms = 0.0
        self.retry_count = 0


class _MockVLMEngine:
    """Mock VLM engine that returns queued responses in order.

    Supports two response modes:

    1. ``responses``: a flat list consumed in call order (legacy behaviour).
    2. ``responses_by_template``: a mapping from ``template_id`` to a list of
       responses for that template.  Each list is consumed independently.

    ``errors_by_template`` can raise an exception for a specific template
    without consuming a response.
    """

    def __init__(
        self,
        responses: Optional[List[Dict[str, Any]]] = None,
        responses_by_template: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
        errors_by_template: Optional[Mapping[str, Exception]] = None,
    ) -> None:
        self._responses: Union[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]] = (
            responses if responses_by_template is None else dict(responses_by_template)
        )
        self._index = 0
        self._errors_by_template = errors_by_template or {}
        self.calls: List[Dict[str, Any]] = []

    def call(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
    ) -> _MockResponse:
        self.calls.append(
            {
                "template_id": template.template_id,
                "images": images,
                "context_vars": context_vars,
                "response_schema": response_schema,
            }
        )

        if template.template_id in self._errors_by_template:
            raise self._errors_by_template[template.template_id]

        if isinstance(self._responses, dict):
            queue = self._responses.get(template.template_id, [])
            if not queue:
                raise IndexError(
                    f"No queued response for template {template.template_id}"
                )
            response = queue.pop(0)
        else:
            if self._index >= len(self._responses):
                raise IndexError("Mock response queue exhausted")
            response = self._responses[self._index]
            self._index += 1

        return _MockResponse(response)


@pytest.fixture
def config_manager() -> ConfigManager:
    manager = ConfigManager("./traffic_analyzer/config")
    manager.load_all()
    return manager


@pytest.fixture
def non_motor_category(config_manager: ConfigManager) -> Any:
    categories = config_manager.get_event_categories()
    category = next(c for c in categories if c.event_id == 4)
    assert category.prompt_template_id == "non_motor_vehicle_detection"
    return category


def _make_analysis_context(num_frames: int = 3, vlm_max_frames: int = 6) -> AnalysisContext:
    """Build an AnalysisContext with the requested number of frames."""

    def _encode_frame(arr: np.ndarray) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return buf.getvalue()

    frames = [
        Keyframe(
            frame_id=i,
            timestamp_sec=float(i),
            image_data=_encode_frame(np.zeros((120, 160, 3), dtype=np.uint8)),
        )
        for i in range(num_frames)
    ]
    return AnalysisContext(
        config=SystemConfig(vlm_max_frames=vlm_max_frames),
        video_meta=VideoMetadata(
            file_path="/tmp/test_video.mp4",
            file_name="test_video.mp4",
            duration_sec=float(num_frames),
            fps=1.0,
            total_frames=num_frames,
            width=160,
            height=120,
        ),
        keyframes=KeyframeSequence(coarse_frames=frames, precision_frames=[]),
    )


@pytest.fixture
def analysis_context() -> AnalysisContext:
    return _make_analysis_context(num_frames=3, vlm_max_frames=6)


def _final_detected_true(frame_index: int) -> Dict[str, Any]:
    return {
        "detected": True,
        "instances": [
            {
                "start_time_sec": float(frame_index),
                "end_time_sec": float(frame_index),
                "evidence_frames": [frame_index],
                "description": "distant motorcycle",
                "reasoning": "narrow silhouette with rider posture, identified as 摩托车",
            }
        ],
        "summary": "detected a distant non-motor vehicle",
    }


def _final_detected_false() -> Dict[str, Any]:
    return {"detected": False, "instances": [], "summary": "no detection"}


def _motion_reflection(is_moving: bool, reason: str) -> Dict[str, Any]:
    return {"is_moving": is_moving, "reason": reason}


def test_far_enhancement_success(
    config_manager: ConfigManager,
    non_motor_category: Any,
    analysis_context: AnalysisContext,
) -> None:
    """Happy path: per-frame ROI -> composite -> final classification with early exit."""
    responses = [
        {
            "bbox_norm": [0.50, 0.50, 0.65, 0.70],
            "reason": "frame 0 contains a distant target",
        },
        _final_detected_true(frame_index=0),
    ]
    engine = _MockVLMEngine(responses)
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            tmpdir_path,
        ):
            candidate = agent.detect(analysis_context)

        assert candidate.detected is True
        assert "composite_image_path" in candidate.raw_vlm_response
        composite_path = Path(candidate.raw_vlm_response["composite_image_path"])
        assert composite_path.name == "test_video_frame_0_composite.jpg"
        assert composite_path.parent == tmpdir_path
        assert composite_path.exists()

        enhancement_meta = candidate.raw_vlm_response.get("far_enhancement", {})
        assert enhancement_meta.get("selected_frame_index") == 0
        assert enhancement_meta.get("bbox_norm") == [0.50, 0.50, 0.65, 0.70]

    assert len(engine.calls) == 2
    assert engine.calls[0]["template_id"] == "far_non_motor_roi_detection"
    assert len(engine.calls[0]["images"]) == 1
    assert engine.calls[1]["template_id"] == "non_motor_vehicle_detection"
    assert len(engine.calls[1]["images"]) == 1
    assert engine.calls[1]["images"][0] == str(composite_path)


def test_far_enhancement_fallback_no_candidate(
    config_manager: ConfigManager,
    non_motor_category: Any,
    analysis_context: AnalysisContext,
) -> None:
    """When no frame returns a valid ROI candidate, fall back to the original path."""
    responses = [
        {"bbox_norm": None, "reason": "no distant target in frame 0"},
        {"bbox_norm": None, "reason": "no distant target in frame 1"},
        {"bbox_norm": None, "reason": "no distant target in frame 2"},
        {
            "detected": True,
            "instances": [
                {
                    "start_time_sec": 0.0,
                    "end_time_sec": 2.0,
                    "evidence_frames": [0, 1, 2],
                    "description": "motorcycle",
                    "reasoning": "two-wheel shape visible, identified as 摩托车",
                }
            ],
            "summary": "detected via fallback",
        },
    ]
    engine = _MockVLMEngine(responses)
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    candidate = agent.detect(analysis_context)

    assert candidate.detected is True
    assert "composite_image_path" not in candidate.raw_vlm_response

    roi_calls = [
        c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"
    ]
    assert len(roi_calls) == 3
    assert all(len(c["images"]) == 1 for c in roi_calls)

    final_call = engine.calls[-1]
    assert final_call["template_id"] == "non_motor_vehicle_detection"
    assert len(final_call["images"]) == 3


def test_early_exit_on_second_frame(
    config_manager: ConfigManager,
    non_motor_category: Any,
    analysis_context: AnalysisContext,
) -> None:
    """Stop iterating frames as soon as the final classification returns detected=True."""
    responses = [
        {
            "bbox_norm": [0.50, 0.50, 0.60, 0.65],
            "reason": "frame 0 contains a possible target",
        },
        _final_detected_false(),
        _motion_reflection(False, "frame 0 is static"),
        {
            "bbox_norm": [0.50, 0.50, 0.65, 0.70],
            "reason": "frame 1 contains a distant target",
        },
        _final_detected_true(frame_index=1),
    ]
    engine = _MockVLMEngine(responses)
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            tmpdir_path,
        ):
            candidate = agent.detect(analysis_context)

        assert candidate.detected is True
        composite_path = Path(candidate.raw_vlm_response["composite_image_path"])
        assert composite_path.name == "test_video_frame_1_composite.jpg"

    roi_calls = [
        c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"
    ]
    final_calls = [
        c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"
    ]
    motion_calls = [
        c
        for c in engine.calls
        if c["template_id"] == "far_non_motor_motion_verification"
    ]
    assert len(roi_calls) == 2
    assert len(final_calls) == 2
    assert len(motion_calls) == 1
    # Frame 2 must not be examined.
    assert len(engine.calls) == 5


def test_all_frames_no_detection(
    config_manager: ConfigManager,
    non_motor_category: Any,
    analysis_context: AnalysisContext,
) -> None:
    """If every final classification returns False, the enhancement path returns detected=False."""
    responses = [
        {
            "bbox_norm": [0.50, 0.50, 0.65, 0.70],
            "reason": "frame 0 candidate",
        },
        _final_detected_false(),
        _motion_reflection(False, "frame 0 is static"),
        {"bbox_norm": None, "reason": "frame 1 no candidate"},
        {
            "bbox_norm": [0.50, 0.50, 0.60, 0.65],
            "reason": "frame 2 candidate",
        },
        _final_detected_false(),
        _motion_reflection(False, "frame 2 is static"),
    ]
    engine = _MockVLMEngine(responses)
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            Path(tmpdir),
        ):
            candidate = agent.detect(analysis_context)

    assert candidate.detected is False

    roi_calls = [
        c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"
    ]
    final_calls = [
        c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"
    ]
    motion_calls = [
        c
        for c in engine.calls
        if c["template_id"] == "far_non_motor_motion_verification"
    ]
    assert len(roi_calls) == 3
    assert len(final_calls) == 2
    assert len(motion_calls) == 2
    assert all(len(c["images"]) == 1 for c in roi_calls)


def test_area_filter_skips_small_bbox(
    config_manager: ConfigManager,
    non_motor_category: Any,
) -> None:
    """A frame whose ROI bbox is too small (< 80 px) is skipped; the next valid frame is used."""
    context = _make_analysis_context(num_frames=3, vlm_max_frames=6)
    responses = [
        {
            # 0.001 * 160 * 0.001 * 120 < 1 px, well below the 80 px threshold.
            "bbox_norm": [0.50, 0.50, 0.501, 0.501],
            "reason": "small candidate in frame 0",
        },
        {
            "bbox_norm": [0.50, 0.50, 0.65, 0.70],
            "reason": "large candidate in frame 1",
        },
        _final_detected_true(frame_index=1),
    ]
    engine = _MockVLMEngine(responses)
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            tmpdir_path,
        ):
            candidate = agent.detect(context)

        assert candidate.detected is True
        composite_path = Path(candidate.raw_vlm_response["composite_image_path"])
        assert composite_path.name == "test_video_frame_1_composite.jpg"

    assert len(engine.calls) == 3
    assert engine.calls[0]["template_id"] == "far_non_motor_roi_detection"
    assert len(engine.calls[0]["images"]) == 1
    assert engine.calls[1]["template_id"] == "far_non_motor_roi_detection"
    assert len(engine.calls[1]["images"]) == 1
    assert engine.calls[2]["template_id"] == "non_motor_vehicle_detection"
    assert len(engine.calls[2]["images"]) == 1
    assert engine.calls[2]["images"][0] == str(composite_path)


def test_motion_reflection_accepts_when_moving(
    config_manager: ConfigManager,
    non_motor_category: Any,
) -> None:
    """Motion reflection upgrades a negative classifier when the target is moving."""
    # Provide two frames so the adjacent frame for frame 0 is frame 1.
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    engine = _MockVLMEngine(
        responses_by_template={
            "far_non_motor_roi_detection": [
                {
                    "bbox_norm": [0.50, 0.50, 0.65, 0.70],
                    "reason": "frame 0 contains a distant target",
                },
            ],
            "non_motor_vehicle_detection": [
                _final_detected_false(),
            ],
            "far_non_motor_motion_verification": [
                _motion_reflection(True, "目标在移动"),
            ],
        }
    )
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            tmpdir_path,
        ):
            candidate = agent.detect(context)

        assert candidate.detected is True
        assert "motion_verification" in candidate.raw_vlm_response
        assert "motion_composite_image_path" in candidate.raw_vlm_response
        motion_path = Path(candidate.raw_vlm_response["motion_composite_image_path"])
        assert "frame_0_motion_1.jpg" in str(motion_path)
        assert motion_path.parent == tmpdir_path
        assert motion_path.exists()
        # Motion-accept must produce concrete instances for adjudication.
        assert len(candidate.instances) == 1
        assert candidate.instances[0].evidence_frames == [0, 1]
        assert "摩托车/非机动车" in candidate.summary

    motion_calls = [
        c
        for c in engine.calls
        if c["template_id"] == "far_non_motor_motion_verification"
    ]
    assert len(motion_calls) == 1
    assert len(motion_calls[0]["images"]) == 1


def test_motion_reflection_rejects_when_static(
    config_manager: ConfigManager,
    non_motor_category: Any,
) -> None:
    """A static reflection candidate is rejected and the next frame is processed."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    engine = _MockVLMEngine(
        responses_by_template={
            "far_non_motor_roi_detection": [
                {
                    "bbox_norm": [0.50, 0.50, 0.65, 0.70],
                    "reason": "frame 0 contains a distant target",
                },
                {"bbox_norm": None, "reason": "frame 1 no candidate"},
            ],
            "non_motor_vehicle_detection": [
                _final_detected_false(),
            ],
            "far_non_motor_motion_verification": [
                _motion_reflection(False, "静态物体"),
            ],
        }
    )
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            Path(tmpdir),
        ):
            candidate = agent.detect(context)

    assert candidate.detected is False

    motion_calls = [
        c
        for c in engine.calls
        if c["template_id"] == "far_non_motor_motion_verification"
    ]
    assert len(motion_calls) == 1


def test_motion_reflection_uses_previous_frame_at_last_index(
    config_manager: ConfigManager,
    non_motor_category: Any,
) -> None:
    """At the last frame the motion reflection uses the previous adjacent frame."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    engine = _MockVLMEngine(
        responses_by_template={
            "far_non_motor_roi_detection": [
                {"bbox_norm": None, "reason": "frame 0 no candidate"},
                {
                    "bbox_norm": [0.50, 0.50, 0.65, 0.70],
                    "reason": "frame 1 contains a distant target",
                },
            ],
            "non_motor_vehicle_detection": [
                _final_detected_false(),
            ],
            "far_non_motor_motion_verification": [
                _motion_reflection(True, "目标在移动"),
            ],
        }
    )
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            tmpdir_path,
        ):
            candidate = agent.detect(context)

        assert candidate.detected is True
        motion_path = Path(candidate.raw_vlm_response["motion_composite_image_path"])
        assert "frame_1_motion_0.jpg" in str(motion_path)
        assert motion_path.parent == tmpdir_path
        assert motion_path.exists()
        assert len(candidate.instances) == 1
        assert candidate.instances[0].evidence_frames == [1, 0]


def test_motion_reflection_error_continues_to_next_frame(
    config_manager: ConfigManager,
    non_motor_category: Any,
) -> None:
    """A non-fatal motion verification error is logged and the next frame is checked."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    engine = _MockVLMEngine(
        responses_by_template={
            "far_non_motor_roi_detection": [
                {
                    "bbox_norm": [0.50, 0.50, 0.65, 0.70],
                    "reason": "frame 0 contains a distant target",
                },
                {
                    "bbox_norm": [0.50, 0.50, 0.65, 0.70],
                    "reason": "frame 1 contains a distant target",
                },
            ],
            "non_motor_vehicle_detection": [
                _final_detected_false(),
                _final_detected_true(frame_index=1),
            ],
        },
        errors_by_template={
            "far_non_motor_motion_verification": RuntimeError("motion API error"),
        },
    )
    agent = ExpertAgent(
        category=non_motor_category,
        vlm_engine=engine,
        config_manager=config_manager,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            Path(tmpdir),
        ):
            candidate = agent.detect(context)

    assert candidate.detected is True

    roi_calls = [
        c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"
    ]
    final_calls = [
        c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"
    ]
    motion_calls = [
        c
        for c in engine.calls
        if c["template_id"] == "far_non_motor_motion_verification"
    ]
    assert len(roi_calls) == 2
    assert len(final_calls) == 2
    assert len(motion_calls) == 1
