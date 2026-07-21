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
    EventInstance,
    Keyframe,
    KeyframeSequence,
    SystemConfig,
    VideoMetadata,
)


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
    """Mock VLM engine that returns queued responses per template."""

    def __init__(
        self,
        responses_by_template: Mapping[str, List[Dict[str, Any]]],
    ) -> None:
        self._responses: Dict[str, List[Dict[str, Any]]] = {
            k: list(v) for k, v in responses_by_template.items()
        }
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

        queue = self._responses.get(template.template_id, [])
        if not queue:
            raise IndexError(f"No queued response for template {template.template_id}")
        response = queue.pop(0)
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


@pytest.fixture
def pedestrian_category(config_manager: ConfigManager) -> Any:
    categories = config_manager.get_event_categories()
    category = next(c for c in categories if c.event_id == 3)
    assert category.prompt_template_id == "pedestrian_detection"
    return category


@pytest.fixture
def construction_category(config_manager: ConfigManager) -> Any:
    categories = config_manager.get_event_categories()
    category = next(c for c in categories if c.event_id == 6)
    assert category.prompt_template_id == "road_construction_detection"
    return category


def _make_analysis_context(num_frames: int = 3, vlm_max_frames: int = 6) -> AnalysisContext:
    """Build an AnalysisContext with the requested number of frames.

    Each frame contains a small amount of random noise so that adjacent-frame
    motion scores are non-zero and pass the far-enhancement motion filter.
    """

    def _encode_frame(arr: np.ndarray) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return buf.getvalue()

    rng = np.random.RandomState(42)
    frames = []
    for i in range(num_frames):
        arr = rng.randint(0, 256, size=(120, 160, 3), dtype=np.uint8)
        frames.append(
            Keyframe(
                frame_id=i,
                timestamp_sec=float(i),
                image_data=_encode_frame(arr),
            )
        )
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


@pytest.fixture
def make_agent(config_manager: ConfigManager, non_motor_category: Any):
    """Return a factory that builds an ExpertAgent + mock engine pair."""

    def _make(
        responses_by_template: Mapping[str, List[Dict[str, Any]]],
        category: Any = None,
    ) -> tuple[ExpertAgent, _MockVLMEngine]:
        if category is None:
            category = non_motor_category
        engine = _MockVLMEngine(responses_by_template=responses_by_template)
        agent = ExpertAgent(
            category=category,
            vlm_engine=engine,
            config_manager=config_manager,
        )
        return agent, engine

    return _make


def _roi_response(
    bbox_norm: Any,
    reason: str,
    occluded: bool = False,
    confidence: Union[str, float] = 0.85,
) -> Dict[str, Any]:
    return {
        "bbox_norm": bbox_norm,
        "occluded": occluded,
        "confidence": confidence,
        "reason": reason,
    }


def _final_response(detected: bool, reason: str) -> Dict[str, Any]:
    return {"detected": detected, "reason": reason}


def _final_response_with_veto(
    detected: bool,
    reason: str,
    is_target_explicitly_four_wheel_vehicle: Optional[bool] = None,
    target_type: str = "",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"detected": detected, "reason": reason}
    if is_target_explicitly_four_wheel_vehicle is not None:
        result["is_target_explicitly_four_wheel_vehicle"] = is_target_explicitly_four_wheel_vehicle
    if target_type:
        result["target_type"] = target_type
    return result


def _pedestrian_final_response(
    detected: bool,
    summary: str,
    instances: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "detected": detected,
        "instances": instances or [],
        "summary": summary,
    }


def _pedestrian_final_response_with_veto(
    detected: bool,
    summary: str,
    instances: Optional[List[Dict[str, Any]]] = None,
    is_target_explicitly_four_wheel_vehicle: Optional[bool] = None,
    target_type: str = "",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "detected": detected,
        "instances": instances or [],
        "summary": summary,
    }
    if is_target_explicitly_four_wheel_vehicle is not None:
        result["is_target_explicitly_four_wheel_vehicle"] = is_target_explicitly_four_wheel_vehicle
    if target_type:
        result["target_type"] = target_type
    return result


def _detect_with_patched_dir(agent: ExpertAgent, context: AnalysisContext) -> Any:
    """Run detection with the output directory patched to a temp folder."""
    tmpdir = Path(tempfile.mkdtemp())
    with patch(
        "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
        tmpdir,
    ):
        return agent.detect(context)


def test_far_enhancement_success(make_agent, analysis_context) -> None:
    """Happy path: collect ROIs from all frames, rank, then classify the top candidate."""
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 distant target"),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "narrow silhouette, 摩托车"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    raw = candidate.raw_vlm_response
    composite_path = Path(raw["composite_image_path"])
    motion_path = Path(raw["motion_composite_image_path"])
    assert composite_path.name == "test_video_event_4_frame_0_composite.jpg"
    assert "event_4_frame_0_motion_1.jpg" in str(motion_path)
    assert composite_path.exists()
    assert motion_path.exists()

    enhancement_meta = raw.get("far_enhancement", {})
    assert enhancement_meta.get("selected_frame_index") == 0
    assert enhancement_meta.get("bbox_norm") == [0.50, 0.50, 0.65, 0.70]
    assert candidate.instances[0].evidence_frames == [0, 1]

    roi_calls = [c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"]
    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(roi_calls) == 3
    assert all(len(c["images"]) == 1 for c in roi_calls)
    assert len(final_calls) == 1
    assert len(final_calls[0]["images"]) == 2
    assert final_calls[0]["images"][0] == str(composite_path)
    assert final_calls[0]["images"][1] == str(motion_path)


def test_far_enhancement_records_frame_analysis_log(make_agent, analysis_context) -> None:
    """A positive detection result carries the per-frame ROI analysis log."""
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 distant target",
                    confidence="high",
                ),
                _roi_response(None, "no distant target in frame 1"),
                _roi_response(None, "no distant target in frame 2"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "narrow silhouette, 摩托车"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    log = candidate.raw_vlm_response.get("far_enhancement", {}).get("frame_analysis_log")
    assert log is not None
    assert len(log) == 3

    assert log[0]["frame"] == 0
    assert log[0]["has_candidate"] is True
    assert log[0]["bbox_norm"] == [0.50, 0.50, 0.65, 0.70]
    assert isinstance(log[0]["area_px"], int) and log[0]["area_px"] > 0
    assert isinstance(log[0]["aspect_ratio"], float) and log[0]["aspect_ratio"] > 0
    assert log[0]["confidence"] == 0.85
    assert isinstance(log[0]["motion_score"], float)
    assert log[0]["reason"] == "frame 0 distant target"

    for idx in (1, 2):
        assert log[idx]["frame"] == idx
        assert log[idx]["has_candidate"] is False
        assert log[idx]["bbox_norm"] is None
        assert log[idx]["area_px"] is None
        assert log[idx]["aspect_ratio"] is None
        assert log[idx]["confidence"] is None
        assert log[idx]["motion_score"] is None
        assert log[idx]["reason"] == f"no distant target in frame {idx}"


def test_no_valid_candidates_frame_analysis_log(make_agent, analysis_context) -> None:
    """A negative result still carries the per-frame ROI analysis log."""
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(None, "no distant target in frame 0"),
                _roi_response(None, "no distant target in frame 1"),
                _roi_response(None, "no distant target in frame 2"),
            ],
        }
    )

    candidate = agent.detect(analysis_context)

    assert candidate.detected is False
    log = candidate.raw_vlm_response.get("far_enhancement", {}).get("frame_analysis_log")
    assert log is not None
    assert len(log) == 3
    assert all(entry["has_candidate"] is False for entry in log)


def test_frame_analysis_log_records_filter_reasons(make_agent) -> None:
    """Frames that fail the area/aspect filters are logged with the filter reason."""
    context = _make_analysis_context(num_frames=3, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.505, 0.515],
                    "small candidate in frame 0",
                ),
                _roi_response(
                    [0.40, 0.40, 0.70, 0.60],
                    "flat candidate in frame 1",
                ),
                _roi_response(None, "no candidate frame 2"),
            ],
        }
    )

    candidate = agent.detect(context)

    assert candidate.detected is False
    log = candidate.raw_vlm_response.get("far_enhancement", {}).get("frame_analysis_log")
    assert log is not None
    assert len(log) == 3
    assert log[0]["has_candidate"] is False
    assert "too small" in log[0]["reason"]
    assert log[1]["has_candidate"] is False
    assert "aspect ratio" in log[1]["reason"]
    assert log[2]["has_candidate"] is False


def test_far_enhancement_saves_assets_next_to_report(make_agent, analysis_context) -> None:
    """When context.output_dir is set, composites are saved there and referenced relatively."""
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 distant target"),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "narrow silhouette, 摩托车"),
            ],
        }
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        report_dir = Path(tmpdir) / "reports"
        report_dir.mkdir()
        analysis_context.output_dir = str(report_dir)
        candidate = agent.detect(analysis_context)

        assert candidate.detected is True
        raw = candidate.raw_vlm_response
        assert raw["composite_image_path"] == "tmp_img/test_video_event_4_frame_0_composite.jpg"
        assert raw["motion_composite_image_path"] == "tmp_img/test_video_event_4_frame_0_motion_1.jpg"

        composite_file = report_dir / "tmp_img" / "test_video_event_4_frame_0_composite.jpg"
        motion_file = report_dir / "tmp_img" / "test_video_event_4_frame_0_motion_1.jpg"
        assert composite_file.exists()
        assert motion_file.exists()

        final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
        assert final_calls[0]["images"][0] == str(composite_file)
        assert final_calls[0]["images"][1] == str(motion_file)


def test_no_valid_candidates_returns_false(make_agent, analysis_context) -> None:
    """When every frame returns no valid ROI candidate the flow reports detected=False."""
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(None, "no distant target in frame 0"),
                _roi_response(None, "no distant target in frame 1"),
                _roi_response(None, "no distant target in frame 2"),
            ],
        }
    )

    candidate = agent.detect(analysis_context)

    assert candidate.detected is False
    assert "composite_image_path" not in candidate.raw_vlm_response

    roi_calls = [c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"]
    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(roi_calls) == 3
    assert len(final_calls) == 0


def test_later_better_frame_chosen_over_earlier_worse(make_agent, analysis_context) -> None:
    """A later frame with higher score is selected even though an earlier frame is valid."""
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.60],
                    "frame 0 possible target",
                    confidence="low",
                ),
                _roi_response(
                    [0.50, 0.50, 0.70, 0.80],
                    "frame 1 clear distant target",
                    confidence="high",
                ),
                _roi_response(None, "frame 2 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "frame 1 confirmed non-motor vehicle"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    composite_path = Path(candidate.raw_vlm_response["composite_image_path"])
    assert composite_path.name == "test_video_event_4_frame_1_composite.jpg"
    assert candidate.instances[0].evidence_frames == [1, 2]

    roi_calls = [c for c in engine.calls if c["template_id"] == "far_non_motor_roi_detection"]
    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(roi_calls) == 3
    assert len(final_calls) == 1


def test_high_scoring_false_does_not_block_lower_true(make_agent) -> None:
    """A negative top-ranked candidate does not prevent a positive lower-ranked candidate from winning."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.70, 0.80],
                    "frame 0 clear candidate",
                    confidence=0.90,
                ),
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 1 candidate",
                    confidence=0.65,
                ),
            ],
            "non_motor_vehicle_detection": [
                _final_response(False, "frame 0 is a car"),
                _final_response(True, "frame 1 confirmed non-motor vehicle"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    composite_path = Path(candidate.raw_vlm_response["composite_image_path"])
    assert composite_path.name == "test_video_event_4_frame_1_composite.jpg"
    assert candidate.instances[0].evidence_frames == [1, 0]
    assert "frame 1 confirmed" in candidate.instances[0].reasoning

    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 2


@pytest.mark.parametrize(
    "bbox,expected_frame",
    [
        ([0.50, 0.50, 0.505, 0.515], 1),  # too small, skip frame 0
        ([0.40, 0.40, 0.70, 0.60], 1),  # too flat, skip frame 0
    ],
    ids=["area_filter", "aspect_filter"],
)
def test_filter_skips_invalid_bbox(make_agent, bbox, expected_frame) -> None:
    """A frame whose ROI fails a filter is skipped and the next valid frame is used."""
    context = _make_analysis_context(num_frames=3, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(bbox, "invalid candidate in frame 0"),
                _roi_response([0.50, 0.50, 0.65, 0.70], "valid candidate in frame 1"),
                _roi_response(None, "frame 2 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "frame 1 confirmed non-motor vehicle"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    composite_path = Path(candidate.raw_vlm_response["composite_image_path"])
    assert composite_path.name == f"test_video_event_4_frame_{expected_frame}_composite.jpg"

    final_calls = [c for c in agent.vlm_engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 1
    assert final_calls[0]["images"][0] == str(composite_path)


def test_car_override_forces_false(make_agent) -> None:
    """A positive classifier result that explicitly describes a car is overridden to False."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.55, 0.75], "valid candidate in frame 0"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "红色方框内是一辆红色轿车，不是摩托车"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False
    assert "composite_image_path" in candidate.raw_vlm_response
    assert "motion_composite_image_path" in candidate.raw_vlm_response

    final_calls = [c for c in agent.vlm_engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 1


def test_all_top_k_negative_returns_false(make_agent) -> None:
    """If every top-K candidate is classified negative and fallback is blocked, return False."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 candidate",
                    confidence=0.65,
                ),
                _roi_response(
                    [0.50, 0.50, 0.60, 0.65],
                    "frame 1 candidate",
                    confidence=0.70,
                ),
            ],
            "non_motor_vehicle_detection": [
                # Negative reasons that lack identifiable vehicle structure block fallback.
                _final_response(False, "框内仅是一团无结构的暗斑，无法确认车身结构"),
                _final_response(False, "框内仅是一团无结构的暗斑，无法确认车身结构"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False

    final_calls = [c for c in agent.vlm_engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 2


@pytest.mark.parametrize(
    "negative_reason,expected_fallback",
    [
        (
            "目标被右侧车辆部分遮挡，但能看到骑乘者头盔和上半身轮廓",
            True,
        ),
        ("框内仅是一团无结构的暗斑，无法确认车身结构", False),
    ],
    ids=["accepted", "rejected_no_structure"],
)
def test_fallback_behavior(make_agent, negative_reason, expected_fallback) -> None:
    """Fallback accepts safe negatives and rejects 'no structure' negatives."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 high-confidence candidate",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(False, negative_reason),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is expected_fallback
    if expected_fallback:
        assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True
        assert "未确认" not in candidate.summary
        assert "回退" not in candidate.summary


def test_fallback_rejected_when_occluded(make_agent) -> None:
    """An occluded top candidate must not be promoted via fallback."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 occluded candidate",
                    confidence="high",
                    occluded=True,
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(False, "细节不足，无法确认"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False


@pytest.mark.parametrize(
    "reason,expected_no_structure",
    [
        ("细节不足，无法确认", False),
        ("画面模糊，无法判断", False),
        ("看不清具体是什么", False),
        ("无法确认目标类型", False),
        ("无法提供明确证据", False),
        ("目标被部分遮挡，但能看到骑乘者头盔", False),
        ("框内仅是一团无结构的暗斑", True),
        ("没有清晰轮廓，无法辨识车辆结构", True),
        ("无明显车辆结构", True),
        ("仅是一个黑点", True),
        ("模糊色块，没有车轮车把", True),
    ],
)
def test_is_no_structure_reasoning(make_agent, reason, expected_no_structure) -> None:
    """Pure uncertainty expressions must not be treated as 'no structure'."""
    agent, _ = make_agent({})
    assert agent._is_no_structure_reasoning(reason) is expected_no_structure


def test_fallback_accepts_uncertainty_reasoning(make_agent) -> None:
    """A high-confidence unoccluded candidate with only uncertain reasoning is promoted."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 high-confidence candidate",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(False, "细节不足，无法确认"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True


def test_non_motor_low_confidence_filter(make_agent) -> None:
    """Only ROI candidates with confidence >= 0.6 enter the final classifier for event_id=4."""
    context = _make_analysis_context(num_frames=3, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.60],
                    "frame 0 low confidence target",
                    confidence=0.50,
                ),
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 1 medium confidence target",
                    confidence=0.65,
                ),
                _roi_response(
                    [0.50, 0.50, 0.70, 0.80],
                    "frame 2 high confidence target",
                    confidence=0.90,
                ),
            ],
            "non_motor_vehicle_detection": [
                _final_response(False, "frame 1 not confirmed"),
                _final_response(True, "frame 2 confirmed non-motor vehicle"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 2


def test_non_motor_all_candidates_below_confidence_gate(make_agent) -> None:
    """If every non-motor ROI candidate is below 0.6, no classifier call is made,
    but evidence composites are still generated from the best candidate."""
    context = _make_analysis_context(num_frames=3, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.60],
                    "frame 0 low confidence target",
                    confidence=0.50,
                ),
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 1 low confidence target",
                    confidence=0.55,
                ),
                _roi_response(
                    [0.50, 0.50, 0.70, 0.80],
                    "frame 2 low confidence target",
                    confidence=0.20,
                ),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False
    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 0
    raw = candidate.raw_vlm_response
    assert "composite_image_path" in raw
    assert "motion_composite_image_path" in raw
    assert Path(raw["composite_image_path"]).exists()
    assert Path(raw["motion_composite_image_path"]).exists()


def test_dual_composite_paths(make_agent) -> None:
    """A valid ROI produces both a single-frame composite and a motion-comparison composite."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 distant target"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "confirmed non-motor vehicle"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    raw = candidate.raw_vlm_response
    assert "composite_image_path" in raw
    assert "motion_composite_image_path" in raw
    motion_path = Path(raw["motion_composite_image_path"])
    assert "event_4_frame_0_motion_1.jpg" in str(motion_path)
    assert motion_path.exists()

    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 1
    assert len(final_calls[0]["images"]) == 2


def _motion_score(value: float) -> Dict[str, float]:
    """Build a motion-score dict consistent with the helper return shape."""
    return {
        "mean_diff": value,
        "fraction_above_threshold": value / 100.0,
        "motion_score": value,
    }


@pytest.mark.parametrize(
    "side_effect,expected_frame",
    [
        ([_motion_score(0.0), _motion_score(10.0)], 1),
        ([_motion_score(10.0)], 0),
    ],
    ids=["low_motion_drops_out", "high_motion_retained"],
)
def test_motion_score_ranking(make_agent, side_effect, expected_frame) -> None:
    """Motion score penalty/retention affects which frame is selected."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.70, 0.80],
                    "frame 0 clear but static target",
                    confidence=0.90,
                ),
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 1 moving target",
                    confidence=0.65,
                ),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "frame 1 confirmed non-motor vehicle"),
            ],
        }
    )

    with patch(
        "traffic_analyzer.core.expert_agent_far_enhancement.compute_roi_motion_score",
        side_effect=side_effect,
    ):
        candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    enhancement_meta = candidate.raw_vlm_response.get("far_enhancement", {})
    assert enhancement_meta.get("selected_frame_index") == expected_frame


def test_motion_score_computed_without_caching_diff_image(make_agent) -> None:
    """The motion-score helper runs on the input frames and writes no diff files."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 target",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "confirmed non-motor vehicle"),
            ],
        }
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch(
            "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
            Path(tmpdir),
        ):
            candidate = agent.detect(context)

        assert candidate.detected is True
        written = list(Path(tmpdir).glob("*"))
        assert len(written) == 2
        assert all("diff" not in p.name.lower() for p in written)


# ---------------------------------------------------------------------------
# Pedestrian far ROI enhancement
# ---------------------------------------------------------------------------


def test_pedestrian_far_enhancement_success(make_agent, pedestrian_category, analysis_context) -> None:
    """Happy path: a single high-confidence ROI classified positive is enough."""
    agent, engine = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant pedestrian",
                    confidence=0.90,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 1 distant pedestrian",
                    confidence=0.75,
                ),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    True,
                    "第0帧红框内为一名站立行人",
                    instances=[
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 0.0,
                            "evidence_frames": [0],
                            "description": "应急车道边缘站立行人，穿深色衣物",
                            "reasoning": "红框内可见直立人形轮廓，位于道路区域",
                        }
                    ],
                ),
            ],
        },
        category=pedestrian_category,
    )

    with patch(
        "traffic_analyzer.core.expert_agent_far_enhancement.compute_roi_motion_score",
        return_value=_motion_score(10.0),
    ):
        candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert candidate.event_id == 3
    # The first positive frame is returned immediately.
    assert "第0帧红框内为一名站立行人" in candidate.summary
    assert len(candidate.instances) == 1
    assert candidate.instances[0].description == "应急车道边缘站立行人，穿深色衣物"

    raw = candidate.raw_vlm_response
    assert "composite_image_path" in raw
    assert "motion_composite_image_path" in raw
    assert raw["far_enhancement"]["selected_frame_index"] == 0

    roi_calls = [c for c in engine.calls if c["template_id"] == "far_pedestrian_roi_detection"]
    final_calls = [c for c in engine.calls if c["template_id"] == "pedestrian_detection"]
    assert len(roi_calls) == 3
    assert len(final_calls) == 1
    # Pedestrian final classifier should receive the full expert response schema.
    assert "instances" in final_calls[0]["response_schema"].get("properties", {})


def test_pedestrian_far_enhancement_negative(make_agent, pedestrian_category, analysis_context) -> None:
    """If the final classifier rejects a low-confidence ROI, the result stays detected=False."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant target",
                    confidence=0.40,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    False,
                    "红框内为路侧标志牌，不是行人",
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert candidate.event_id == 3
    assert "未检测到" in candidate.summary


def test_pedestrian_far_enhancement_car_override(make_agent, pedestrian_category, analysis_context) -> None:
    """A pedestrian classifier that actually describes a car is overridden to False."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response([0.50, 0.50, 0.55, 0.75], "frame 0 distant target"),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    True,
                    "红框内为一辆白色轿车",
                    instances=[
                        {
                            "description": "白色轿车停在应急车道",
                            "reasoning": "红框内是轿车，不是行人",
                        }
                    ],
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False


def test_pedestrian_far_enhancement_fallback_high_confidence(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """When the final classifier rejects a high-confidence pedestrian ROI, fallback promotes it."""
    roi_reason = "画面右侧应急车道边缘有直立人形轮廓，位于道路区域，疑似行人"
    agent, engine = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    roi_reason,
                    confidence=0.88,
                    occluded=False,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    False,
                    "红框内目标较小，无法确认是否为行人",
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert candidate.event_id == 3
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True
    assert "第0帧红色方框内" in candidate.summary
    assert "直立人形轮廓" in candidate.summary
    assert "位于道路区域" in candidate.summary

    # The final classifier was still called, but its negative result was overridden.
    final_calls = [c for c in engine.calls if c["template_id"] == "pedestrian_detection"]
    assert len(final_calls) == 1


def test_pedestrian_far_enhancement_fallback_rejects_low_confidence(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """A low-confidence pedestrian ROI must not be promoted via fallback."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 possible target",
                    confidence=0.40,
                    occluded=False,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    False,
                    "红框内无法确认",
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False


def test_pedestrian_far_enhancement_fallback_rejects_occluded(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """An occluded pedestrian ROI must not be promoted via fallback."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 occluded candidate",
                    confidence=0.88,
                    occluded=True,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    False,
                    "红框内被遮挡，无法确认",
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False


def test_pedestrian_far_enhancement_fallback_rejects_car_reason(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """If the classifier's negative reason explicitly describes a car, fallback is blocked."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant target",
                    confidence=0.88,
                    occluded=False,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    False,
                    "红框内是一辆白色轿车，不是行人",
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False


def test_pedestrian_far_enhancement_not_overridden_near_vehicle(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """A pedestrian standing next to a vehicle must not be overridden as a car."""
    agent, engine = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant pedestrian",
                    confidence=0.90,
                    occluded=False,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 1 distant pedestrian",
                    confidence=0.75,
                    occluded=False,
                ),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    True,
                    "红色方框内确认为1名高速公路行人，位于白色厢式货车后方，呈直立站立姿态",
                    instances=[
                        {
                            "description": "应急车道边缘站立行人，位于白色厢式货车后方",
                            "reasoning": "红框内可见直立人形轮廓，靠近一辆白色面包车，位于道路区域",
                        }
                    ],
                ),
            ],
        },
        category=pedestrian_category,
    )

    with patch(
        "traffic_analyzer.core.expert_agent_far_enhancement.compute_roi_motion_score",
        return_value=_motion_score(10.0),
    ):
        candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert "白色厢式货车后方" in candidate.summary

    final_calls = [c for c in engine.calls if c["template_id"] == "pedestrian_detection"]
    assert len(final_calls) == 1


def _construction_roi_response(
    evidence_regions: List[Dict[str, Any]],
    summary: str = "construction evidence found",
) -> Dict[str, Any]:
    return {"evidence_regions": evidence_regions, "summary": summary}


def _construction_final_response(
    detected: bool,
    summary: str,
    instances: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "detected": detected,
        "instances": instances or [],
        "summary": summary,
    }


def test_construction_gallery_success(
    make_agent, construction_category, analysis_context
) -> None:
    """Construction multi-ROI gallery produces a detected candidate."""
    agent, engine = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.92},
                        {"bbox_norm": [0.50, 0.45, 0.55, 0.60], "tag": "worker", "confidence": 0.85},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(
                    True,
                    "检测到道路施工",
                    instances=[
                        {
                            "description": "施工区域有锥桶和工人",
                            "reasoning": "合成图显示锥桶和穿反光背心工人，判定为道路施工。",
                        }
                    ],
                ),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert candidate.event_id == 6
    raw = candidate.raw_vlm_response
    assert "gallery_image_path" in raw
    assert raw["gallery_image_path"].endswith("_event_6_frame_1_gallery.jpg")

    gallery_path = Path(str(raw["gallery_image_path"]).replace("tmp_img/", ""))
    # Resolve against the patched temp dir prefix if needed.
    if not gallery_path.is_absolute():
        gallery_path = Path(_FAR_ENHANCEMENT_OUTPUT_DIR) / gallery_path
    assert gallery_path.exists()

    far_enhancement = raw.get("far_enhancement", {})
    assert far_enhancement.get("selected_frame_index") == 1
    assert len(far_enhancement.get("evidence_regions", [])) == 2

    roi_calls = [c for c in engine.calls if c["template_id"] == "road_construction_roi_detection"]
    final_calls = [c for c in engine.calls if c["template_id"] == "road_construction_detection"]
    assert len(roi_calls) == 1
    assert len(final_calls) == 1
    assert len(final_calls[0]["images"]) == 1


def test_construction_gallery_negative_keeps_image(
    make_agent, construction_category, analysis_context
) -> None:
    """A negative classifier still preserves the gallery image path."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.60},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert "gallery_image_path" in candidate.raw_vlm_response


def test_construction_gallery_filters_invalid_regions(
    make_agent, construction_category, analysis_context
) -> None:
    """Regions that fail area/aspect filters result in a negative with no gallery."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        # Too small.
                        {"bbox_norm": [0.50, 0.50, 0.505, 0.515], "tag": "cone", "confidence": 0.9},
                        # Too flat (w/h = 5.0 > 4.0).
                        {"bbox_norm": [0.40, 0.40, 0.90, 0.50], "tag": "barrier", "confidence": 0.9},
                    ]
                ),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert "gallery_image_path" not in candidate.raw_vlm_response


def test_construction_gallery_caps_regions(
    make_agent, construction_category, analysis_context
) -> None:
    """Only the top 4 regions by confidence are shown in the gallery."""
    agent, engine = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.05 * i, 0.40, 0.05 * i + 0.04, 0.55], "tag": "cone", "confidence": 0.5 + i * 0.1}
                        for i in range(6)
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(True, "检测到道路施工"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    far_enhancement = candidate.raw_vlm_response.get("far_enhancement", {})
    assert len(far_enhancement.get("evidence_regions", [])) == 4


def test_construction_gallery_saves_assets_next_to_report(
    make_agent, construction_category, analysis_context
) -> None:
    """When context.output_dir is set, the gallery is saved next to the report."""
    agent, engine = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.92},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(True, "检测到道路施工"),
            ],
        },
        category=construction_category,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        report_dir = Path(tmpdir) / "reports"
        report_dir.mkdir()
        analysis_context.output_dir = str(report_dir)
        candidate = agent.detect(analysis_context)

        assert candidate.detected is True
        assert candidate.raw_vlm_response["gallery_image_path"] == "tmp_img/test_video_event_6_frame_1_gallery.jpg"
        gallery_file = report_dir / "tmp_img" / "test_video_event_6_frame_1_gallery.jpg"
        assert gallery_file.exists()

        final_calls = [c for c in engine.calls if c["template_id"] == "road_construction_detection"]
        assert final_calls[0]["images"][0] == str(gallery_file)



def test_construction_gallery_fallback_classifier_false_but_evidence_strong(
    make_agent, construction_category, analysis_context
) -> None:
    """If classifier returns false but ROI evidence has cone+worker, fallback promotes to true."""
    agent, engine = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.92},
                        {"bbox_norm": [0.50, 0.45, 0.55, 0.60], "tag": "worker", "confidence": 0.85},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工，只有孤立锥桶。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert "检测到道路施工" in candidate.summary
    assert "锥桶" in candidate.summary
    assert "施工人员" in candidate.summary
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True

    # Classifier was still called, but its negative result was overridden.
    final_calls = [c for c in engine.calls if c["template_id"] == "road_construction_detection"]
    assert len(final_calls) == 1


def test_construction_gallery_fallback_three_cones(
    make_agent, construction_category, analysis_context
) -> None:
    """Three cones without worker/vehicle still trigger the construction fallback."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.20, 0.40, 0.25, 0.55], "tag": "cone", "confidence": 0.88},
                        {"bbox_norm": [0.30, 0.42, 0.35, 0.57], "tag": "cone", "confidence": 0.85},
                        {"bbox_norm": [0.40, 0.44, 0.45, 0.59], "tag": "cone", "confidence": 0.82},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert "锥桶×3" in candidate.summary
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True


def test_construction_gallery_fallback_not_triggered_for_isolated_cone(
    make_agent, construction_category, analysis_context
) -> None:
    """A single isolated cone with negative classifier should remain detected=false."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.70},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工，只有孤立锥桶。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert "fallback" not in candidate.raw_vlm_response.get("far_enhancement", {})


def test_construction_gallery_fallback_not_triggered_for_low_confidence(
    make_agent, construction_category, analysis_context
) -> None:
    """Evidence with confidence below 0.5 must not count toward fallback."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.40},
                        {"bbox_norm": [0.50, 0.45, 0.55, 0.60], "tag": "worker", "confidence": 0.35},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert "fallback" not in candidate.raw_vlm_response.get("far_enhancement", {})


def test_construction_gallery_fallback_not_triggered_for_worker_vehicle_only(
    make_agent, construction_category, analysis_context
) -> None:
    """Worker + vehicle alone is not sufficient ground-based construction evidence."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "worker", "confidence": 0.90},
                        {"bbox_norm": [0.50, 0.45, 0.55, 0.60], "tag": "vehicle", "confidence": 0.95},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "只有施工人员和车辆，没有落地锥桶或隔离栏。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert "fallback" not in candidate.raw_vlm_response.get("far_enhancement", {})


# ---------------------------------------------------------------------------
# Non-motor car-semantic veto regression tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        # Non-motor conclusion with car mentioned only for contrast/negation.
        ("红框内是一辆摩托车，不是轿车", False),
        ("红框内为两轮车，而非汽车", False),
        ("目标为电动车，并非汽车", False),
        ("该目标为非机动车，不是轿车", False),
        # Replacement/comparison contexts still anchored to non-motor evidence.
        ("目标被一辆白色轿车取代，但可见骑乘者头盔", False),
        ("并未被汽车遮挡，可见骑乘姿态", False),
        ("虽被轿车遮挡，仍能辨识车把与头盔", False),
        # Explicit car assertion dominates over a trailing non-motor negation.
        ("红色方框内是一辆红色轿车，不是摩托车", True),
        ("框内目标判定为白色面包车", True),
        ("红框内是一辆SUV，不是电动车", True),
        # Plain non-motor description without car context.
        ("目标为非机动车，位于应急车道", False),
        ("红框内可见两轮车和骑手", False),
    ],
)
def test_is_explicitly_car_reasoning_for_non_motor(make_agent, reason, expected) -> None:
    """The event-aware car veto distinguishes car-context from car-conclusion."""
    agent, _ = make_agent({})
    assert agent._is_explicitly_car_reasoning_for_non_motor(reason) is expected


def test_non_motor_car_context_does_not_override_positive(make_agent) -> None:
    """A positive classifier mentioning cars in comparison context is not overridden."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 two-wheeler candidate",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "红框内为两轮车，而非汽车，骑乘姿态明显"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert "fallback" not in candidate.raw_vlm_response.get("far_enhancement", {})
    assert "而非汽车" in candidate.summary or "两轮车" in candidate.summary


def test_non_motor_fallback_accepts_car_context_negative(make_agent) -> None:
    """Fallback accepts a negative classifier whose reason only mentions cars in comparison."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 two-wheeler candidate with rider posture",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                # Classifier mentions a car only as a contextual replacement;
                # it does not claim the boxed target is a car, nor does it invoke
                # the "no structure" veto, so fallback should promote the ROI.
                _final_response(False, "目标被一辆白色轿车取代"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True


def test_non_motor_fallback_skips_double_car_veto_after_override(make_agent) -> None:
    """If classifier was positive but car-vetoed, fallback does not re-apply the veto."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 two-wheeler candidate",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                # The old generic veto would override this to False because of "汽车".
                # The new event-aware veto keeps it True, so fallback is not needed.
                _final_response(True, "红框内为摩托车，而非汽车"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert "fallback" not in candidate.raw_vlm_response.get("far_enhancement", {})


# ---------------------------------------------------------------------------
# Structured veto field tests
# ---------------------------------------------------------------------------


def test_non_motor_structured_veto_true_overrides_detected(make_agent) -> None:
    """A positive classifier with is_target_explicitly_four_wheel_vehicle=true is vetoed."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 candidate", confidence="high"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response_with_veto(
                    True,
                    "红框内为摩托车，而非汽车",
                    is_target_explicitly_four_wheel_vehicle=True,
                    target_type="汽车",
                ),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False
    assert candidate.is_target_explicitly_four_wheel_vehicle is True
    assert candidate.target_type == "汽车"


def test_non_motor_structured_veto_false_keeps_detected(make_agent) -> None:
    """A positive classifier with is_target_explicitly_four_wheel_vehicle=false stays true."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 candidate", confidence="high"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response_with_veto(
                    True,
                    "红框内为摩托车",
                    is_target_explicitly_four_wheel_vehicle=False,
                    target_type="摩托车",
                ),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert candidate.is_target_explicitly_four_wheel_vehicle is False
    assert candidate.target_type == "摩托车"


def test_non_motor_structured_veto_missing_falls_back_to_regex(make_agent) -> None:
    """When the structured veto field is missing, regex checks still veto explicit cars."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 candidate", confidence="high"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "红色方框内是一辆红色轿车，不是摩托车"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False
    assert candidate.is_target_explicitly_four_wheel_vehicle is None


def test_non_motor_structured_veto_false_in_fallback_negative_keeps_accepted(make_agent) -> None:
    """Fallback accepts a negative classifier that explicitly sets is_target_explicitly_four_wheel_vehicle=false."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 two-wheeler candidate with rider posture",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response_with_veto(
                    False,
                    "目标被一辆白色轿车取代",
                    is_target_explicitly_four_wheel_vehicle=False,
                    target_type="无法确定",
                ),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True


def test_non_motor_structured_veto_true_in_fallback_blocks_fallback(make_agent) -> None:
    """Fallback is blocked when the classifier explicitly says the target is a car."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 two-wheeler candidate",
                    confidence="high",
                ),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                _final_response_with_veto(
                    False,
                    "红框内是一辆白色轿车",
                    is_target_explicitly_four_wheel_vehicle=True,
                    target_type="汽车",
                ),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is False


def test_non_motor_final_classifier_parse_failure_retries_and_succeeds(make_agent) -> None:
    """If the first final classifier response is unparseable, retry with a shorter prompt."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 candidate", confidence="high"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                {"_unparseable": True},  # first call returns a dict but we simulate via _MockResponse
                _final_response_with_veto(
                    True,
                    "红框内为摩托车",
                    is_target_explicitly_four_wheel_vehicle=False,
                    target_type="摩托车",
                ),
            ],
        }
    )

    # Override the first response to be a parse failure.
    original_call = engine.call

    def _patched_call(template, images, context_vars=None, response_schema=None):
        queue = engine._responses.get(template.template_id, [])
        if queue and "_unparseable" in queue[0]:
            queue.pop(0)
            engine.calls.append(
                {
                    "template_id": template.template_id,
                    "images": images,
                    "context_vars": context_vars,
                    "response_schema": response_schema,
                }
            )
            return _MockResponse({}, success=False, raw_text="not valid json")
        return original_call(template, images, context_vars, response_schema)

    engine.call = _patched_call

    candidate = _detect_with_patched_dir(agent, context)

    assert candidate.detected is True
    assert candidate.is_target_explicitly_four_wheel_vehicle is False
    final_calls = [c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"]
    assert len(final_calls) == 2


def test_non_motor_final_classifier_parse_failure_retry_falls_back_to_regex(make_agent) -> None:
    """If both initial and retry final classifier responses fail, regex fallback can still veto."""
    context = _make_analysis_context(num_frames=2, vlm_max_frames=6)
    agent, engine = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 candidate", confidence="high"),
                _roi_response(None, "frame 1 no candidate"),
            ],
            "non_motor_vehicle_detection": [
                {"_unparseable": True},
                {"_unparseable": True},
            ],
        }
    )

    original_call = engine.call

    def _patched_call(template, images, context_vars=None, response_schema=None):
        queue = engine._responses.get(template.template_id, [])
        if queue and "_unparseable" in queue[0]:
            queue.pop(0)
            engine.calls.append(
                {
                    "template_id": template.template_id,
                    "images": images,
                    "context_vars": context_vars,
                    "response_schema": response_schema,
                }
            )
            return _MockResponse({}, success=False, raw_text="红色方框内是一辆红色轿车")
        return original_call(template, images, context_vars, response_schema)

    engine.call = _patched_call

    candidate = _detect_with_patched_dir(agent, context)

    # Both calls failed to parse, so _run_final_classifier returns None.
    # The negative_final_reason is set to the raw text, but fallback is blocked
    # by the car regex on "红色方框内是一辆红色轿车".
    assert candidate.detected is False


def test_pedestrian_structured_veto_true_overrides_detected(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """A pedestrian classifier with is_target_explicitly_four_wheel_vehicle=true is vetoed."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response([0.50, 0.50, 0.55, 0.75], "frame 0 distant target"),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response_with_veto(
                    True,
                    "红框内为一名站立行人",
                    instances=[
                        {
                            "description": "应急车道边缘站立行人",
                            "reasoning": "红框内可见直立人形轮廓",
                        }
                    ],
                    is_target_explicitly_four_wheel_vehicle=True,
                    target_type="汽车",
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert candidate.is_target_explicitly_four_wheel_vehicle is True


def test_pedestrian_structured_veto_false_keeps_detected(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """A pedestrian classifier with is_target_explicitly_four_wheel_vehicle=false stays true."""
    agent, engine = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant pedestrian",
                    confidence=0.85,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 1 distant pedestrian",
                    confidence=0.75,
                ),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response_with_veto(
                    True,
                    "红框内为一名站立行人",
                    instances=[
                        {
                            "description": "应急车道边缘站立行人",
                            "reasoning": "红框内可见直立人形轮廓",
                        }
                    ],
                    is_target_explicitly_four_wheel_vehicle=False,
                    target_type="行人",
                ),
            ],
        },
        category=pedestrian_category,
    )

    with patch(
        "traffic_analyzer.core.expert_agent_far_enhancement.compute_roi_motion_score",
        return_value=_motion_score(10.0),
    ):
        candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert candidate.is_target_explicitly_four_wheel_vehicle is False
    assert candidate.target_type == "行人"
    assert candidate.raw_vlm_response["far_enhancement"]["selected_frame_index"] == 0

    final_calls = [c for c in engine.calls if c["template_id"] == "pedestrian_detection"]
    assert len(final_calls) == 1


def test_pedestrian_low_confidence_filter_excludes_below_threshold(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """Only ROI candidates with confidence >= 0.6 enter the final classifier for event_id=3."""
    agent, engine = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 low-confidence target",
                    confidence=0.50,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 1 medium-confidence target",
                    confidence=0.65,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 2 high-confidence target",
                    confidence=0.80,
                ),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    True,
                    "第2帧红框内为一名站立行人",
                    instances=[
                        {
                            "start_time_sec": 2.0,
                            "end_time_sec": 2.0,
                            "evidence_frames": [2],
                            "description": "应急车道边缘站立行人",
                            "reasoning": "红框内可见直立人形轮廓",
                        }
                    ],
                ),
                _pedestrian_final_response(
                    True,
                    "第1帧红框内为一名站立行人",
                    instances=[
                        {
                            "start_time_sec": 1.0,
                            "end_time_sec": 1.0,
                            "evidence_frames": [1],
                            "description": "应急车道边缘站立行人",
                            "reasoning": "红框内可见直立人形轮廓",
                        }
                    ],
                ),
            ],
        },
        category=pedestrian_category,
    )

    with patch(
        "traffic_analyzer.core.expert_agent_far_enhancement.compute_roi_motion_score",
        return_value=_motion_score(10.0),
    ):
        candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    final_calls = [c for c in engine.calls if c["template_id"] == "pedestrian_detection"]
    # The highest-confidence ROI (0.80) is classified first and returns immediately.
    assert len(final_calls) == 1
    assert candidate.raw_vlm_response["far_enhancement"]["selected_frame_index"] == 2


def test_pedestrian_all_candidates_below_confidence_gate_generate_evidence(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """If every pedestrian ROI candidate is below 0.6, no classifier call is made,
    but evidence composites are still generated from the best candidate."""
    agent, engine = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 low-confidence target",
                    confidence=0.50,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 1 low-confidence target",
                    confidence=0.55,
                ),
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 2 low-confidence target",
                    confidence=0.20,
                ),
            ],
        },
        category=pedestrian_category,
    )

    with patch(
        "traffic_analyzer.core.expert_agent_far_enhancement.compute_roi_motion_score",
        return_value=_motion_score(10.0),
    ):
        candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    final_calls = [c for c in engine.calls if c["template_id"] == "pedestrian_detection"]
    assert len(final_calls) == 0
    raw = candidate.raw_vlm_response
    assert "composite_image_path" in raw
    assert "motion_composite_image_path" in raw
    assert Path(raw["composite_image_path"]).exists()
    assert Path(raw["motion_composite_image_path"]).exists()


def test_construction_structured_veto_true_overrides_detected(
    make_agent, construction_category, analysis_context
) -> None:
    """A construction classifier with is_target_explicitly_four_wheel_vehicle=true is vetoed."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {"bbox_norm": [0.30, 0.40, 0.35, 0.55], "tag": "cone", "confidence": 0.92},
                    ]
                ),
            ],
            "road_construction_detection": [
                {
                    "detected": True,
                    "is_target_explicitly_four_wheel_vehicle": True,
                    "target_type": "汽车",
                    "instances": [
                        {
                            "description": "一辆白色轿车",
                            "reasoning": "画面中只有一辆正常行驶车辆",
                        }
                    ],
                    "summary": "画面中只有一辆白色轿车，没有施工",
                },
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert candidate.is_target_explicitly_four_wheel_vehicle is True



def test_construction_gallery_filters_cone_not_on_ground(
    make_agent, construction_category, analysis_context
) -> None:
    """Cone regions explicitly flagged as not on the ground are filtered out."""
    agent, _ = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {
                            "bbox_norm": [0.30, 0.10, 0.35, 0.25],
                            "tag": "cone",
                            "confidence": 0.92,
                            "on_ground": False,
                        },
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    # No gallery should be created when the only region is an invalid cone.
    assert "gallery_image_path" not in candidate.raw_vlm_response
    far_enhancement = candidate.raw_vlm_response.get("far_enhancement", {})
    assert len(far_enhancement.get("evidence_regions", [])) == 0


def test_construction_gallery_keeps_cone_on_ground_for_fallback(
    make_agent, construction_category, analysis_context
) -> None:
    """Grounded cones are kept and can contribute to the construction fallback."""
    agent, engine = make_agent(
        {
            "road_construction_roi_detection": [
                _construction_roi_response(
                    [
                        {
                            "bbox_norm": [0.30, 0.40, 0.35, 0.55],
                            "tag": "cone",
                            "confidence": 0.92,
                            "on_ground": True,
                        },
                        {"bbox_norm": [0.50, 0.45, 0.55, 0.60], "tag": "worker", "confidence": 0.85},
                    ]
                ),
            ],
            "road_construction_detection": [
                _construction_final_response(False, "未检测到道路施工。"),
            ],
        },
        category=construction_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert candidate.raw_vlm_response["far_enhancement"].get("fallback") is True
    # The gallery should only contain the grounded cone + worker.
    far_enhancement = candidate.raw_vlm_response.get("far_enhancement", {})
    tags = {r["tag"] for r in far_enhancement.get("evidence_regions", [])}
    assert "cone" in tags
    assert "worker" in tags

    final_calls = [c for c in engine.calls if c["template_id"] == "road_construction_detection"]
    assert len(final_calls) == 1


def test_far_enhancement_failure_returns_negative_for_enabled_events(make_agent) -> None:
    """When the far-enhancement flow fails, any template with far_object_enhancement
    enabled must return a negative candidate instead of falling back to raw frames."""
    agent, engine = make_agent({})

    with patch.object(agent, "_detect_with_far_enhancement", return_value=None):
        candidate = agent.detect(_make_analysis_context())

    assert candidate.detected is False
    assert candidate.event_id == 4
    assert "增强检测失败" in candidate.summary
    # The direct VLM call with raw frames must not happen.
    final_calls = [
        c for c in engine.calls if c["template_id"] == "non_motor_vehicle_detection"
    ]
    assert len(final_calls) == 0


def test_top_k_zero_returns_negative_without_index_error(
    make_agent, config_manager, non_motor_category, analysis_context
) -> None:
    """top_k <= 0 yields an empty top-candidate list; must not raise IndexError."""
    template = config_manager.get_prompt_template(non_motor_category.prompt_template_id)
    template.far_object_enhancement.top_k = 0
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response([0.50, 0.50, 0.65, 0.70], "frame 0 distant target"),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert candidate.event_id == 4
    assert "未检测到" in candidate.summary


def test_final_classifier_string_false_detected_stays_negative(
    make_agent, analysis_context
) -> None:
    """Regression: detected='false' (string) must not evaluate to True."""
    agent, _ = make_agent(
        {
            "far_non_motor_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 0 distant target",
                    occluded=True,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "non_motor_vehicle_detection": [
                {"detected": "false", "reason": "红框内为路侧设备箱，不是非机动车"},
            ],
        }
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is False
    assert candidate.event_id == 4


def test_pedestrian_final_classifier_tolerates_malformed_instances(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """Malformed instance entries must not crash the pedestrian branch."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant pedestrian",
                    confidence=0.90,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    True,
                    "第0帧红框内为一名站立行人",
                    instances=[
                        None,
                        {
                            "start_time_sec": None,
                            "end_time_sec": None,
                            "evidence_frames": "junk",
                            "description": "应急车道边缘站立行人",
                            "reasoning": "红框内可见直立人形轮廓",
                        },
                    ],
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert len(candidate.instances) == 1
    assert candidate.instances[0].start_time_sec == 0.0
    assert candidate.instances[0].end_time_sec == 0.0
    assert candidate.instances[0].evidence_frames == [0, 1]


def test_pedestrian_final_classifier_tolerates_non_list_instances(
    make_agent, pedestrian_category, analysis_context
) -> None:
    """A non-list instances field is treated as empty, not iterated as keys."""
    agent, _ = make_agent(
        {
            "far_pedestrian_roi_detection": [
                _roi_response(
                    [0.50, 0.50, 0.55, 0.75],
                    "frame 0 distant pedestrian",
                    confidence=0.90,
                ),
                _roi_response(None, "no candidate frame 1"),
                _roi_response(None, "no candidate frame 2"),
            ],
            "pedestrian_detection": [
                _pedestrian_final_response(
                    True,
                    "第0帧红框内为一名站立行人",
                    instances={"start_time_sec": 0.0},
                ),
            ],
        },
        category=pedestrian_category,
    )

    candidate = _detect_with_patched_dir(agent, analysis_context)

    assert candidate.detected is True
    assert len(candidate.instances) == 1
    assert candidate.instances[0].description == "第0帧红框内为一名站立行人"
    assert candidate.instances[0].evidence_frames == [0, 1]
