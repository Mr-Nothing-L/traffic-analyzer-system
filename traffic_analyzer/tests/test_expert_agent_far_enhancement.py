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

    def _make(responses_by_template: Mapping[str, List[Dict[str, Any]]]) -> tuple[ExpertAgent, _MockVLMEngine]:
        engine = _MockVLMEngine(responses_by_template=responses_by_template)
        agent = ExpertAgent(
            category=non_motor_category,
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
    assert composite_path.name == "test_video_frame_0_composite.jpg"
    assert "frame_0_motion_1.jpg" in str(motion_path)
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
        assert raw["composite_image_path"] == "tmp_img/test_video_frame_0_composite.jpg"
        assert raw["motion_composite_image_path"] == "tmp_img/test_video_frame_0_motion_1.jpg"

        composite_file = report_dir / "tmp_img" / "test_video_frame_0_composite.jpg"
        motion_file = report_dir / "tmp_img" / "test_video_frame_0_motion_1.jpg"
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
    assert composite_path.name == "test_video_frame_1_composite.jpg"
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
                    confidence="high",
                ),
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 1 candidate",
                    confidence="medium",
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
    assert composite_path.name == "test_video_frame_1_composite.jpg"
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
    assert composite_path.name == f"test_video_frame_{expected_frame}_composite.jpg"

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
                    confidence="low",
                ),
                _roi_response(
                    [0.50, 0.50, 0.60, 0.65],
                    "frame 1 candidate",
                    confidence="low",
                ),
            ],
            "non_motor_vehicle_detection": [
                _final_response(False, "frame 0 not a non-motor vehicle"),
                _final_response(False, "frame 1 not a non-motor vehicle"),
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
    assert "frame_0_motion_1.jpg" in str(motion_path)
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
                    confidence="high",
                ),
                _roi_response(
                    [0.50, 0.50, 0.65, 0.70],
                    "frame 1 moving target",
                    confidence="medium",
                ),
            ],
            "non_motor_vehicle_detection": [
                _final_response(True, "frame 1 confirmed non-motor vehicle"),
            ],
        }
    )

    with patch(
        "traffic_analyzer.core.expert_agent.compute_roi_motion_score",
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
