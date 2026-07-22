"""Tests for the per-video evidence exporter (web UI evidence.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.core.evidence_exporter import export_evidence
from traffic_analyzer.models.schemas import (
    AdjudicationResult,
    AnalysisContext,
    BinaryEncoding,
    EventCandidate,
    EventCategory,
    EventResult,
    Keyframe,
    KeyframeSequence,
    LLMProviderConfig,
    LLMResponse,
    PromptTemplate,
    Report,
    SamplingConfig,
    SceneInfo,
    SystemConfig,
    VideoMetadata,
)
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _write_image(path: Path) -> None:
    """Write a tiny valid JPEG at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(path, "JPEG")


def _make_video_meta() -> VideoMetadata:
    return VideoMetadata(
        file_path="/tmp/test_video.mp4",
        file_name="test_video.mp4",
        duration_sec=15.0,
        fps=25.0,
        total_frames=375,
        width=1920,
        height=1080,
    )


def _occupancy_raw_response() -> Dict[str, Any]:
    """Raw response shape produced by the event_id=1 occupancy branch."""
    prefix = "tmp_img/test_video/test_video_event_1_occupancy"
    return {
        "mask_overlay_image_path": f"{prefix}/02_masks_overlay.jpg",
        "vehicle_boxes_image_path": f"{prefix}/03_vehicles_red_boxes.jpg",
        "zoom_grid_image_path": f"{prefix}/04_zoom_grid.jpg",
        "single_zoom_image_paths": [
            ("V1", f"{prefix}/zoom/V1_白色工程车_zoom4x.jpg"),
        ],
        "occupancy_detection": {
            "selected_frame_index": 1,
            "emergency_polygon_rel": [
                [0.65, 0.2],
                [0.95, 0.2],
                [0.95, 0.8],
                [0.65, 0.8],
            ],
            "chevron_polygon_rel": None,
            "calibration_reasoning": "画面右侧为应急车道",
            "rois": [
                {
                    "id": "V1",
                    "label": "白色小车",
                    "zone": "emergency_lane",
                    "rel_box": [0.68, 0.35, 0.82, 0.55],
                    "reason": "车辆完全位于应急车道内",
                },
            ],
            "vehicle_overlaps": {"V1": 0.9},
        },
    }


def _make_occupancy_context(report_dir: Path) -> AnalysisContext:
    """Context with one detected occupancy event + matching raw response."""
    context = AnalysisContext(
        video_meta=_make_video_meta(),
        output_dir=str(report_dir),
    )
    context.event_results[1] = EventResult(
        event_id=1, event_name="应急车道占用", detected=True
    )
    context.event_candidates[1] = EventCandidate(
        event_id=1,
        event_name="应急车道占用",
        detected=True,
        raw_vlm_response=_occupancy_raw_response(),
    )
    # Materialize the referenced artifacts next to the "report".
    prefix = report_dir / "tmp_img" / "test_video" / "test_video_event_1_occupancy"
    _write_image(prefix / "02_masks_overlay.jpg")
    _write_image(prefix / "03_vehicles_red_boxes.jpg")
    _write_image(prefix / "04_zoom_grid.jpg")
    _write_image(prefix / "zoom" / "V1_白色工程车_zoom4x.jpg")
    return context


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestEvidenceContract:
    def test_contract_fields_and_types(self, tmp_path: Path) -> None:
        context = _make_occupancy_context(tmp_path / "reports")

        out = export_evidence(context, tmp_path / "sft")

        assert out == tmp_path / "sft" / "test_video_evidence.json"
        payload = json.loads(out.read_text(encoding="utf-8"))

        assert payload["schema_version"] == 1
        video = payload["video"]
        assert video["file_name"] == "test_video.mp4"
        assert video["duration_sec"] == 15.0
        assert video["fps"] == 25.0
        assert video["width"] == 1920
        assert video["height"] == 1080

        assert len(payload["events"]) == 1
        event = payload["events"][0]
        assert event["event_id"] == 1
        assert event["name"] == "应急车道占用"
        assert event["detected"] is True

        calibration = event["calibration"]
        assert calibration["frame_index"] == 1
        assert calibration["emergency_polygon_rel"] == [
            [0.65, 0.2],
            [0.95, 0.2],
            [0.95, 0.8],
            [0.65, 0.8],
        ]
        assert calibration["chevron_polygon_rel"] is None

        assert len(event["evidence_regions"]) == 1
        region = event["evidence_regions"][0]
        assert region["frame_index"] == 1
        assert region["box_rel"] == [0.68, 0.35, 0.82, 0.55]
        assert all(isinstance(v, float) for v in region["box_rel"])
        assert region["label"] == "白色小车"
        assert isinstance(region["image"], str)

        assert isinstance(event["gallery_images"], list)
        assert len(event["gallery_images"]) == 3

    def test_images_copied_and_referenced_relatively(self, tmp_path: Path) -> None:
        context = _make_occupancy_context(tmp_path / "reports")

        out = export_evidence(context, tmp_path / "sft")
        payload = json.loads(out.read_text(encoding="utf-8"))
        event = payload["events"][0]

        # Occupancy artifact names are generic, so they are scoped with the
        # video stem on copy to keep a shared images/ dir collision-free.
        region_image = event["evidence_regions"][0]["image"]
        assert region_image == "images/test_video__V1_白色工程车_zoom4x.jpg"
        assert (tmp_path / "sft" / region_image).is_file()

        assert event["gallery_images"] == [
            "images/test_video__02_masks_overlay.jpg",
            "images/test_video__03_vehicles_red_boxes.jpg",
            "images/test_video__04_zoom_grid.jpg",
        ]
        for ref in event["gallery_images"]:
            assert (tmp_path / "sft" / ref).is_file()

    def test_detected_false_event_with_no_raw_data(self, tmp_path: Path) -> None:
        """Events without coordinate data are included with empty lists."""
        context = AnalysisContext(video_meta=_make_video_meta())
        context.event_results[2] = EventResult(
            event_id=2, event_name="抛洒物", detected=False
        )

        out = export_evidence(context, tmp_path / "sft")
        payload = json.loads(out.read_text(encoding="utf-8"))

        event = payload["events"][0]
        assert event["event_id"] == 2
        assert event["name"] == "抛洒物"
        assert event["detected"] is False
        assert event["calibration"] == {
            "frame_index": None,
            "emergency_polygon_rel": None,
            "chevron_polygon_rel": None,
        }
        assert event["evidence_regions"] == []
        assert event["gallery_images"] == []
        # No images were referenced, so no images dir is created.
        assert not (tmp_path / "sft" / "images").exists()

    def test_construction_gallery_event(self, tmp_path: Path) -> None:
        report_dir = tmp_path / "reports"
        gallery_rel = "tmp_img/test_video/test_video_event_6_frame_1_gallery.jpg"
        _write_image(report_dir / gallery_rel)

        context = AnalysisContext(
            video_meta=_make_video_meta(),
            output_dir=str(report_dir),
        )
        context.event_results[6] = EventResult(
            event_id=6, event_name="道路施工", detected=True
        )
        context.event_candidates[6] = EventCandidate(
            event_id=6,
            event_name="道路施工",
            detected=True,
            raw_vlm_response={
                "gallery_image_path": gallery_rel,
                "far_enhancement": {
                    "selected_frame_index": 1,
                    "evidence_regions": [
                        {"bbox_norm": [0.3, 0.4, 0.35, 0.55], "tag": "cone"},
                        {"bbox_norm": [0.5, 0.4, 0.6, 0.6], "tag": "worker"},
                    ],
                    "summary": "施工区域",
                },
            },
        )

        out = export_evidence(context, tmp_path / "sft")
        payload = json.loads(out.read_text(encoding="utf-8"))
        event = payload["events"][0]

        # Gallery names already embed the video stem, so they are kept as-is.
        assert event["gallery_images"] == [
            "images/test_video_event_6_frame_1_gallery.jpg"
        ]
        assert (tmp_path / "sft" / event["gallery_images"][0]).is_file()

        regions = event["evidence_regions"]
        assert [r["label"] for r in regions] == ["cone", "worker"]
        assert all(r["frame_index"] == 1 for r in regions)
        assert all(
            r["image"] == "images/test_video_event_6_frame_1_gallery.jpg"
            for r in regions
        )
        # Non-occupancy events carry no calibration polygons.
        assert event["calibration"]["emergency_polygon_rel"] is None
        assert event["calibration"]["chevron_polygon_rel"] is None

    def test_generic_far_enhancement_event(self, tmp_path: Path) -> None:
        report_dir = tmp_path / "reports"
        composite_rel = "tmp_img/test_video/test_video_event_4_frame_0_composite.jpg"
        motion_rel = "tmp_img/test_video/test_video_event_4_frame_0_motion_1.jpg"
        _write_image(report_dir / composite_rel)
        _write_image(report_dir / motion_rel)

        context = AnalysisContext(
            video_meta=_make_video_meta(),
            output_dir=str(report_dir),
        )
        context.event_results[4] = EventResult(
            event_id=4, event_name="摩托车出现", detected=True
        )
        context.event_candidates[4] = EventCandidate(
            event_id=4,
            event_name="摩托车出现",
            detected=True,
            raw_vlm_response={
                "composite_image_path": composite_rel,
                "motion_composite_image_path": motion_rel,
                "far_enhancement": {
                    "selected_frame_index": 0,
                    "bbox_norm": [0.5, 0.5, 0.65, 0.7],
                    "reason": "frame 0 distant target",
                },
            },
        )

        out = export_evidence(context, tmp_path / "sft")
        payload = json.loads(out.read_text(encoding="utf-8"))
        event = payload["events"][0]

        assert event["evidence_regions"] == [
            {
                "frame_index": 0,
                "box_rel": [0.5, 0.5, 0.65, 0.7],
                "label": "frame 0 distant target",
                "image": "images/test_video_event_4_frame_0_composite.jpg",
            }
        ]
        assert event["gallery_images"] == [
            "images/test_video_event_4_frame_0_composite.jpg",
            "images/test_video_event_4_frame_0_motion_1.jpg",
        ]
        for ref in event["gallery_images"]:
            assert (tmp_path / "sft" / ref).is_file()


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_returns_none_when_literally_nothing(self, tmp_path: Path) -> None:
        context = AnalysisContext()
        assert export_evidence(context, tmp_path / "sft") is None
        assert not (tmp_path / "sft").exists()

    def test_missing_video_meta_still_writes_events(self, tmp_path: Path) -> None:
        context = AnalysisContext()
        context.event_results[1] = EventResult(
            event_id=1, event_name="应急车道占用", detected=False
        )

        out = export_evidence(context, tmp_path / "sft")

        assert out is not None
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["video"]["file_name"] is None
        assert len(payload["events"]) == 1

    def test_missing_image_files_are_skipped(self, tmp_path: Path) -> None:
        """Referenced images that do not exist degrade to null/empty, not errors."""
        context = AnalysisContext(
            video_meta=_make_video_meta(),
            output_dir=str(tmp_path / "reports"),
        )
        context.event_results[6] = EventResult(
            event_id=6, event_name="道路施工", detected=True
        )
        context.event_candidates[6] = EventCandidate(
            event_id=6,
            event_name="道路施工",
            detected=True,
            raw_vlm_response={
                "gallery_image_path": "tmp_img/test_video/missing_gallery.jpg",
                "far_enhancement": {
                    "selected_frame_index": 1,
                    "evidence_regions": [
                        {"bbox_norm": [0.3, 0.4, 0.35, 0.55], "tag": "cone"},
                    ],
                },
            },
        )

        out = export_evidence(context, tmp_path / "sft")

        assert out is not None
        payload = json.loads(out.read_text(encoding="utf-8"))
        event = payload["events"][0]
        assert event["gallery_images"] == []
        assert event["evidence_regions"][0]["image"] is None
        # Coordinates survive even though the image is gone.
        assert event["evidence_regions"][0]["box_rel"] == [0.3, 0.4, 0.35, 0.55]

    def test_unwritable_out_dir_returns_none(self, tmp_path: Path) -> None:
        context = AnalysisContext(video_meta=_make_video_meta())
        context.event_results[1] = EventResult(
            event_id=1, event_name="应急车道占用", detected=False
        )
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")

        assert export_evidence(context, blocker / "sft") is None


# ---------------------------------------------------------------------------
# Orchestrator wiring (Step 3.5, style-matched to test_orchestrator.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config_manager() -> MagicMock:
    manager = MagicMock()
    system_config = SystemConfig(
        llm_provider=LLMProviderConfig(provider="anthropic", api_key="test"),
        sampling=SamplingConfig(),
        output_dir="./output",
        save_debug_frames=False,
    )
    manager.load_all.return_value = system_config
    manager.get_event_categories.return_value = [
        EventCategory(
            event_id=0,
            event_code="A",
            name="Test Event A",
            name_zh="测试事件A",
            description="Test description A",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            is_active=True,
        ),
    ]
    manager.get_prompt_template.return_value = PromptTemplate(
        template_id="test_template",
        name="Test Template",
        system_prompt="You are a test.",
        user_prompt="Test: {{event_name}}",
    )
    manager.get_adjudication_rules.return_value = []
    return manager


@pytest.fixture
def mock_video_preprocessor() -> MagicMock:
    preprocessor = MagicMock()
    preprocessor.process.return_value = KeyframeSequence(
        coarse_frames=[
            Keyframe(frame_id=0, timestamp_sec=0.0, image_path="/tmp/f0.jpg"),
        ],
        precision_frames=[],
    )
    return preprocessor


@pytest.fixture
def mock_vlm_engine() -> MagicMock:
    engine = MagicMock()
    engine.call.return_value = LLMResponse(
        success=True,
        raw_text='{"detected": true}',
        parsed_data={"detected": True},
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    engine.get_usage_stats.return_value = {"total_calls": 1, "total_tokens": 15}
    return engine


@pytest.fixture
def mock_report_generator() -> MagicMock:
    generator = MagicMock()
    report = Report(
        video_info=_make_video_meta(),
        scene_summary=SceneInfo(road_count=2),
        binary_encoding=BinaryEncoding(encoding_string="1", detected_events=[0]),
    )
    generator.generate.return_value = report
    return generator


@pytest.fixture
def orchestrator(
    mock_config_manager: MagicMock,
    mock_video_preprocessor: MagicMock,
    mock_vlm_engine: MagicMock,
    mock_report_generator: MagicMock,
) -> AnalysisOrchestrator:
    expert_layer = MagicMock()
    expert_layer.execute.return_value = MagicMock(
        success=True,
        data=[EventCandidate(event_id=0, event_name="Test Event A", detected=True)],
    )
    adjudication_step = MagicMock()
    adjudication_step.execute.return_value = MagicMock(
        success=True,
        data=AdjudicationResult(
            event_results=[
                EventResult(event_id=0, event_name="Test Event A", detected=True),
            ],
            adjudication_reasoning="Test reasoning",
            reasoning_chain=[],
            audit_log=[],
        ),
    )
    return AnalysisOrchestrator(
        config_manager=mock_config_manager,
        video_preprocessor=mock_video_preprocessor,
        vlm_engine=mock_vlm_engine,
        report_generator=mock_report_generator,
        expert_agent_layer=expert_layer,
        adjudication_step=adjudication_step,
    )


@pytest.fixture
def temp_video(tmp_path: Path) -> str:
    """Create a temporary synthetic video file."""
    video_path = str(tmp_path / "test_video.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(video_path, fourcc, 30.0, (640, 480))
    for _ in range(30):
        writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
    writer.release()
    return video_path


class TestEvidenceExportMount:
    _SFT_STEP_CLS = (
        "traffic_analyzer.orchestrator.analysis_orchestrator.SftLabelRewriteStep"
    )
    _EXPORT_FN = (
        "traffic_analyzer.orchestrator.analysis_orchestrator.export_evidence"
    )

    def test_export_runs_when_sft_enabled(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
        tmp_path: Path,
    ) -> None:
        config = orchestrator.config_manager.load_all.return_value
        config.sft_label_enabled = True
        config.sft_label_output_dir = str(tmp_path / "sft")
        with patch(self._SFT_STEP_CLS), patch(self._EXPORT_FN) as mock_export:
            mock_export.return_value = tmp_path / "sft" / "test_video_evidence.json"
            report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)
        mock_export.assert_called_once()
        call_args = mock_export.call_args
        assert isinstance(call_args.args[0], AnalysisContext)
        assert call_args.args[1] == tmp_path / "sft"

    def test_export_skipped_when_sft_disabled(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        assert orchestrator.config_manager.load_all.return_value.sft_label_enabled is False
        with patch(self._EXPORT_FN) as mock_export:
            report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)
        mock_export.assert_not_called()

    def test_export_failure_does_not_break_report(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
        tmp_path: Path,
    ) -> None:
        """Evidence export is auxiliary: errors must not break the pipeline."""
        config = orchestrator.config_manager.load_all.return_value
        config.sft_label_enabled = True
        config.sft_label_output_dir = str(tmp_path / "sft")
        with patch(self._SFT_STEP_CLS), patch(self._EXPORT_FN) as mock_export:
            mock_export.side_effect = RuntimeError("boom")
            report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)
        orchestrator.report_generator.generate.assert_called_once()
