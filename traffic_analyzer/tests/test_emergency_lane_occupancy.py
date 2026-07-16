"""Unit and integration tests for emergency lane occupancy detection.

Covers:
- Visualization utility functions in
  :mod:`traffic_analyzer.utils.emergency_lane_occupancy`.
- Far-enhancement report rendering for event_id=1.
- End-to-end ExpertAgent far-enhancement flow for event_id=1.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.expert_agent import ExpertAgent, _FAR_ENHANCEMENT_OUTPUT_DIR
from traffic_analyzer.core.report_far_enhancement_renderer import _render_far_enhancement
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    Keyframe,
    KeyframeSequence,
    SystemConfig,
    VideoMetadata,
)
from traffic_analyzer.utils.emergency_lane_occupancy import (
    build_occupancy_summary,
    compute_roi_zone_overlap,
    create_single_zooms,
    create_zoom_grid,
    draw_vehicle_rois,
    generate_masks_overlay,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_manager() -> ConfigManager:
    manager = ConfigManager("./traffic_analyzer/config")
    manager.load_all()
    return manager


@pytest.fixture
def emergency_lane_category(config_manager: ConfigManager) -> Any:
    categories = config_manager.get_event_categories()
    category = next(c for c in categories if c.event_id == 1)
    assert category.prompt_template_id == "emergency_lane_occupancy_detection"
    return category


@pytest.fixture
def synthetic_720p_frame() -> np.ndarray:
    """A synthetic 1280x720 BGR frame."""
    frame = np.full((720, 1280, 3), (180, 180, 180), dtype=np.uint8)
    return frame


@pytest.fixture
def sample_rois() -> List[Dict[str, Any]]:
    """Two synthetic ROIs for testing visual evidence generation."""
    return [
        {
            "id": "V1",
            "label": "黄色工程车",
            "zone": "emergency_lane",
            "rel_box": [0.68, 0.35, 0.82, 0.55],
            "reason": "车辆完全位于应急车道内",
        },
        {
            "id": "V2",
            "label": "白色轿车",
            "zone": "chevron",
            "rel_box": [0.48, 0.62, 0.58, 0.72],
            "reason": "车辆位于导流区内",
        },
    ]


@pytest.fixture
def sample_polygons() -> Dict[str, List[List[float]]]:
    return {
        "emergency_polygon_rel": [[0.65, 0.2], [0.95, 0.2], [0.95, 0.8], [0.65, 0.8]],
        "chevron_polygon_rel": [[0.45, 0.6], [0.65, 0.6], [0.55, 0.8]],
    }


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
        context_vars: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
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
def make_agent(config_manager: ConfigManager, emergency_lane_category: Any):
    """Return a factory that builds an ExpertAgent + mock engine pair."""

    def _make(
        responses_by_template: Mapping[str, List[Dict[str, Any]]],
    ) -> tuple[ExpertAgent, _MockVLMEngine]:
        engine = _MockVLMEngine(responses_by_template=responses_by_template)
        agent = ExpertAgent(
            category=emergency_lane_category,
            vlm_engine=engine,
            config_manager=config_manager,
        )
        return agent, engine

    return _make


def _make_analysis_context(num_frames: int = 3, vlm_max_frames: int = 6) -> AnalysisContext:
    """Build an AnalysisContext with the requested number of frames."""

    def _encode_frame(arr: np.ndarray) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return buf.getvalue()

    rng = np.random.RandomState(42)
    frames = []
    for i in range(num_frames):
        arr = rng.randint(0, 256, size=(720, 1280, 3), dtype=np.uint8)
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
            width=1280,
            height=720,
        ),
        keyframes=KeyframeSequence(coarse_frames=frames, precision_frames=[]),
    )


# ---------------------------------------------------------------------------
# A. Visualization utility tests
# ---------------------------------------------------------------------------


class TestVisualizationUtilities:
    def test_generate_masks_overlay_dimensions_and_save(
        self,
        synthetic_720p_frame: np.ndarray,
        sample_polygons: Dict[str, List[List[float]]],
    ) -> None:
        """Mask overlay should match input dimensions and be saved as JPEG."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "02_masks_overlay.jpg")
            result = generate_masks_overlay(
                synthetic_720p_frame,
                emergency_polygon_rel=sample_polygons["emergency_polygon_rel"],
                chevron_polygon_rel=sample_polygons["chevron_polygon_rel"],
                output_path=output_path,
            )

            assert isinstance(result, Image.Image)
            assert result.mode == "RGB"
            assert result.size == (1280, 720)
            assert os.path.exists(output_path)

    def test_draw_vehicle_rois_dimensions_and_save(
        self,
        synthetic_720p_frame: np.ndarray,
        sample_rois: List[Dict[str, Any]],
    ) -> None:
        """Vehicle ROI annotated image should match input dimensions and be saved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "03_vehicles_red_boxes.jpg")
            result = draw_vehicle_rois(
                synthetic_720p_frame,
                sample_rois,
                output_path=output_path,
            )

            assert isinstance(result, Image.Image)
            assert result.mode == "RGB"
            assert result.size == (1280, 720)
            assert os.path.exists(output_path)

    def test_create_zoom_grid_dimensions_and_save(
        self,
        synthetic_720p_frame: np.ndarray,
        sample_rois: List[Dict[str, Any]],
    ) -> None:
        """Zoom grid should match input dimensions and be saved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "04_zoom_grid.jpg")
            result = create_zoom_grid(
                synthetic_720p_frame,
                sample_rois,
                scale=4,
                output_path=output_path,
            )

            assert isinstance(result, Image.Image)
            assert result.mode == "RGB"
            assert result.size == (1280, 720)
            assert os.path.exists(output_path)

    def test_create_zoom_grid_empty_rois(
        self,
        synthetic_720p_frame: np.ndarray,
    ) -> None:
        """Zoom grid with no ROIs should still produce a valid image."""
        result = create_zoom_grid(synthetic_720p_frame, rois=[], scale=4)
        assert result.size == (1280, 720)

    def test_create_single_zooms_outputs(
        self,
        synthetic_720p_frame: np.ndarray,
        sample_rois: List[Dict[str, Any]],
    ) -> None:
        """Single zooms should produce relative paths and saved images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = create_single_zooms(
                synthetic_720p_frame,
                sample_rois,
                scale=4,
                output_dir=tmpdir,
            )

            assert len(results) == len(sample_rois)
            for roi_id, rel_path in results:
                assert rel_path.startswith("zoom/")
                assert rel_path.endswith("_zoom4x.jpg")
                assert roi_id in {roi["id"] for roi in sample_rois}
                saved_path = os.path.join(tmpdir, rel_path)
                assert os.path.exists(saved_path)

    def test_compute_roi_zone_overlap_range(
        self,
        sample_rois: List[Dict[str, Any]],
        sample_polygons: Dict[str, List[List[float]]],
    ) -> None:
        """Overlap values should be within [0, 1]."""
        overlap = compute_roi_zone_overlap(
            sample_rois[0]["rel_box"],
            sample_polygons["emergency_polygon_rel"],
            img_width=1280,
            img_height=720,
        )
        assert 0.0 <= overlap <= 1.0
        # V1 sits well inside the emergency polygon, so overlap should be high.
        assert overlap > 0.5

    def test_compute_roi_zone_overlap_zero_for_invalid(
        self,
        sample_polygons: Dict[str, List[List[float]]],
    ) -> None:
        """Overlap should be 0 for invalid/empty inputs."""
        assert (
            compute_roi_zone_overlap(
                [0.0, 0.0, 0.1, 0.1],
                [],
                img_width=1280,
                img_height=720,
            )
            == 0.0
        )

    def test_build_occupancy_summary_structure(
        self,
        sample_rois: List[Dict[str, Any]],
    ) -> None:
        """Summary should contain the expected keys and vehicle list."""
        overlaps = {"V1": 0.85, "V2": 0.0}
        summary = build_occupancy_summary("test_video", sample_rois, overlaps)

        assert summary["video_stem"] == "test_video"
        assert summary["event_type"] == "emergency_lane_occupancy"
        assert summary["total_vehicles"] == len(sample_rois)
        assert summary["occupied_count"] == 1
        assert sorted(summary["zones"]) == ["chevron", "emergency_lane"]
        assert isinstance(summary["summary_text"], str)

        vehicles = summary["vehicles"]
        assert len(vehicles) == len(sample_rois)
        for vehicle in vehicles:
            assert "id" in vehicle
            assert "label" in vehicle
            assert "zone" in vehicle
            assert "overlap" in vehicle
            assert "occupied" in vehicle
            assert 0.0 <= vehicle["overlap"] <= 1.0


# ---------------------------------------------------------------------------
# B. Report rendering tests
# ---------------------------------------------------------------------------


class TestReportRendering:
    def test_render_emergency_lane_occupancy_markdown(self) -> None:
        """Renderer should emit expected markdown lines for event_id=1 evidence."""
        candidate: Dict[str, Any] = {
            "event_id": 1,
            "event_name": "应急车道占用",
            "detected": True,
            "summary": "检测到应急车道占用",
            "raw_vlm_response": {
                "mask_overlay_image_path": "tmp_img/test_video_event_1_occupancy/02_masks_overlay.jpg",
                "vehicle_boxes_image_path": "tmp_img/test_video_event_1_occupancy/03_vehicles_red_boxes.jpg",
                "zoom_grid_image_path": "tmp_img/test_video_event_1_occupancy/04_zoom_grid.jpg",
                "single_zoom_image_paths": [
                    ("V1", "tmp_img/test_video_event_1_occupancy/zoom/V1_黄色工程车_zoom4x.jpg"),
                ],
                "occupancy_detection": {
                    "selected_frame_index": 1,
                    "rois": [
                        {
                            "id": "V1",
                            "label": "黄色工程车",
                            "zone": "emergency_lane",
                            "rel_box": [0.68, 0.35, 0.82, 0.55],
                            "reason": "车辆完全位于应急车道内",
                        },
                    ],
                    "vehicle_overlaps": {"V1": 0.85},
                    "calibration_reasoning": "应急车道内有一辆黄色工程车",
                },
            },
        }

        lines = _render_far_enhancement(candidate, event_id=1)
        markdown = "\n".join(lines)

        assert "应急车道占用增强证据" in markdown
        assert "02_masks_overlay.jpg" in markdown
        assert "03_vehicles_red_boxes.jpg" in markdown
        assert "04_zoom_grid.jpg" in markdown
        assert "车辆ID | 标签 | 区域 | bbox | overlap | 标定理由" in markdown
        assert "tmp_img/test_video_event_1_occupancy/zoom/V1_黄色工程车_zoom4x.jpg" in markdown
        assert "V1" in markdown
        assert "黄色工程车" in markdown
        assert "emergency_lane" in markdown


# ---------------------------------------------------------------------------
# C. ExpertAgent far enhancement integration test
# ---------------------------------------------------------------------------


class TestExpertAgentIntegration:
    def test_detect_emergency_lane_occupancy_creates_evidence(
        self,
        make_agent: Any,
    ) -> None:
        """Full far-enhancement flow should generate occupancy evidence images."""
        agent, engine = make_agent(
            {
                "emergency_lane_calibration": [
                    {
                        "emergency_polygon_rel": [
                            [0.65, 0.2],
                            [0.95, 0.2],
                            [0.95, 0.8],
                            [0.65, 0.8],
                        ],
                        "chevron_polygon_rel": None,
                        "summary": "画面右侧为应急车道",
                    },
                ],
                "emergency_lane_vehicle_roi": [
                    {
                        "rois": [
                            {
                                "id": "V1",
                                "label": "黄色工程车",
                                "zone": "emergency_lane",
                                "rel_box": [0.68, 0.35, 0.82, 0.55],
                                "reason": "车辆完全位于应急车道内",
                            },
                        ],
                        "summary": "应急车道内有一辆黄色工程车",
                    },
                ],
                "emergency_lane_occupancy_detection": [
                    {
                        "detected": True,
                        "instances": [
                            {
                                "start_time_sec": 1.0,
                                "end_time_sec": 2.0,
                                "evidence_frames": [1],
                                "description": "黄色工程车占用应急车道",
                                "reasoning": "车辆整体位于应急车道内，构成占用",
                            },
                        ],
                        "summary": "检测到应急车道占用",
                    },
                ],
            }
        )

        context = _make_analysis_context(num_frames=3, vlm_max_frames=6)

        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir) / "reports"
            report_dir.mkdir()
            context.output_dir = str(report_dir)

            with patch(
                "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
                Path(tmpdir) / "tmp_img",
            ):
                candidate = agent.detect(context)

            assert candidate.detected is True
            assert candidate.event_id == 1

            raw = candidate.raw_vlm_response
            assert "mask_overlay_image_path" in raw
            assert "vehicle_boxes_image_path" in raw
            assert "zoom_grid_image_path" in raw
            assert "single_zoom_image_paths" in raw
            assert "occupancy_detection" in raw

            occupancy = raw["occupancy_detection"]
            assert occupancy["selected_frame_index"] == 1
            assert len(occupancy["rois"]) == 1
            assert occupancy["vehicle_overlaps"]["V1"] > 0.0
            assert "summary" in occupancy

            # Evidence files should exist on disk.
            base_dir = Path(context.output_dir) / "tmp_img" / "test_video_event_1_occupancy"
            assert (base_dir / "02_masks_overlay.jpg").exists()
            assert (base_dir / "03_vehicles_red_boxes.jpg").exists()
            assert (base_dir / "04_zoom_grid.jpg").exists()
            assert (base_dir / "zoom" / "V1_黄色工程车_zoom4x.jpg").exists()

        # All three VLM calls should have been made.
        calibration_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_calibration"
        ]
        vehicle_roi_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_vehicle_roi"
        ]
        final_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_occupancy_detection"
        ]
        assert len(calibration_calls) == 1
        assert len(vehicle_roi_calls) == 1
        assert len(final_calls) == 1
        assert len(final_calls[0]["images"]) == 3
        # Vehicle ROI call should receive the calibrated polygons as context.
        assert vehicle_roi_calls[0]["context_vars"]["emergency_polygon_rel"] is not None
        assert vehicle_roi_calls[0]["context_vars"]["chevron_polygon_rel"] is None

    def test_detect_emergency_lane_occupancy_no_rois(self, make_agent: Any) -> None:
        """When calibration finds no zones, the flow should return a negative candidate."""
        agent, engine = make_agent(
            {
                "emergency_lane_calibration": [
                    {
                        "emergency_polygon_rel": None,
                        "chevron_polygon_rel": None,
                        "summary": "画面中未识别到应急车道或导流区。",
                    },
                ],
            }
        )

        context = _make_analysis_context(num_frames=3, vlm_max_frames=6)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
                Path(tmpdir),
            ):
                candidate = agent.detect(context)

            assert candidate.detected is False
            assert candidate.event_id == 1
            assert "occupancy_detection" in candidate.raw_vlm_response

        # No vehicle ROI or final classifier call should be made when no zones are found.
        vehicle_roi_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_vehicle_roi"
        ]
        final_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_occupancy_detection"
        ]
        assert len(vehicle_roi_calls) == 0
        assert len(final_calls) == 0

    def test_detect_emergency_lane_occupancy_empty_vehicle_rois(
        self, make_agent: Any
    ) -> None:
        """When calibration finds zones but no vehicles, return a negative candidate."""
        agent, engine = make_agent(
            {
                "emergency_lane_calibration": [
                    {
                        "emergency_polygon_rel": [
                            [0.65, 0.2],
                            [0.95, 0.2],
                            [0.95, 0.8],
                            [0.65, 0.8],
                        ],
                        "chevron_polygon_rel": None,
                        "summary": "画面右侧为应急车道",
                    },
                ],
                "emergency_lane_vehicle_roi": [
                    {
                        "rois": [],
                        "summary": "已标定区域内无占用车辆",
                    },
                ],
            }
        )

        context = _make_analysis_context(num_frames=3, vlm_max_frames=6)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
                Path(tmpdir),
            ):
                candidate = agent.detect(context)

            assert candidate.detected is False
            assert candidate.event_id == 1
            occupancy = candidate.raw_vlm_response["occupancy_detection"]
            assert occupancy["emergency_polygon_rel"] is not None
            assert occupancy["rois"] == []

        # No final classifier call should be made when there are no vehicle ROIs.
        final_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_occupancy_detection"
        ]
        assert len(final_calls) == 0

    def test_detect_emergency_lane_occupancy_calibration_fails(
        self, make_agent: Any
    ) -> None:
        """If the calibration VLM call fails, the flow returns a negative candidate."""
        agent, engine = make_agent({})
        context = _make_analysis_context(num_frames=3, vlm_max_frames=6)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
                Path(tmpdir),
            ):
                candidate = agent.detect(context)

        assert candidate.detected is False
        assert candidate.event_id == 1
        assert "增强检测失败" in candidate.summary
        final_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_occupancy_detection"
        ]
        assert len(final_calls) == 0

    def test_detect_emergency_lane_occupancy_vehicle_roi_fails(
        self, make_agent: Any
    ) -> None:
        """If calibration succeeds but vehicle ROI detection fails, return negative."""
        agent, engine = make_agent(
            {
                "emergency_lane_calibration": [
                    {
                        "emergency_polygon_rel": [
                            [0.65, 0.2],
                            [0.95, 0.2],
                            [0.95, 0.8],
                            [0.65, 0.8],
                        ],
                        "chevron_polygon_rel": None,
                        "summary": "画面右侧为应急车道",
                    },
                ],
            }
        )
        context = _make_analysis_context(num_frames=3, vlm_max_frames=6)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "traffic_analyzer.core.expert_agent._FAR_ENHANCEMENT_OUTPUT_DIR",
                Path(tmpdir),
            ):
                candidate = agent.detect(context)

        assert candidate.detected is False
        assert candidate.event_id == 1
        assert "增强检测失败" in candidate.summary
        final_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_occupancy_detection"
        ]
        assert len(final_calls) == 0

    def test_far_enhancement_failure_does_not_fallback_to_raw_frames(
        self,
        make_agent: Any,
    ) -> None:
        """If the emergency lane far-enhancement flow fails, do not fall back to raw frames."""
        agent, engine = make_agent({})
        context = _make_analysis_context(num_frames=3, vlm_max_frames=6)

        with patch.object(agent, "_detect_with_far_enhancement", return_value=None):
            candidate = agent.detect(context)

        assert candidate.detected is False
        assert candidate.event_id == 1
        assert "增强检测失败" in candidate.summary

        final_calls = [
            c for c in engine.calls if c["template_id"] == "emergency_lane_occupancy_detection"
        ]
        assert len(final_calls) == 0
