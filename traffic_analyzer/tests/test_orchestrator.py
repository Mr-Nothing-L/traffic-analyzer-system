"""Integration tests for AnalysisOrchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from traffic_analyzer.models.schemas import (
    AnalysisContext,
    BinaryEncoding,
    EventCategory,
    EventInstance,
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
    Track,
    VideoMetadata,
)
from traffic_analyzer.orchestrator.analysis_orchestrator import (
    AnalysisOrchestrator,
    OrchestratorError,
)


# ---------------------------------------------------------------------------
# Fixtures
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
            detection_mode="direct_vlm",
            is_active=True,
        ),
        EventCategory(
            event_id=1,
            event_code="B",
            name="Test Event B",
            name_zh="测试事件B",
            description="Test description B",
            detection_mode="logic_chain",
            logic_chain_id="test_chain",
            is_active=True,
        ),
        EventCategory(
            event_id=2,
            event_code="C",
            name="Inactive Event",
            name_zh="未激活事件",
            description="Should be skipped",
            detection_mode="direct_vlm",
            is_active=False,
        ),
    ]
    manager.get_prompt_template.return_value = PromptTemplate(
        template_id="test_template",
        name="Test Template",
        system_prompt="You are a test.",
        user_prompt="Test: {{event_name}}",
    )
    manager.get_logic_chain.return_value = MagicMock(
        chain_id="test_chain",
        name="Test Chain",
        target_event_id=1,
        steps=[],
    )
    return manager


@pytest.fixture
def mock_video_preprocessor() -> MagicMock:
    preprocessor = MagicMock()
    preprocessor.process.return_value = KeyframeSequence(
        coarse_frames=[
            Keyframe(frame_id=0, timestamp_sec=0.0, image_path="/tmp/f0.jpg"),
            Keyframe(frame_id=30, timestamp_sec=1.0, image_path="/tmp/f1.jpg"),
        ],
        precision_frames=[
            Keyframe(frame_id=5, timestamp_sec=0.17, image_path="/tmp/p0.jpg"),
        ],
    )
    return preprocessor


@pytest.fixture
def mock_vlm_engine() -> MagicMock:
    engine = MagicMock()
    engine.call.return_value = LLMResponse(
        success=True,
        raw_text='{"detected": true, "confidence": 0.85}',
        parsed_data={"detected": True, "confidence": 0.85},
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    engine.get_usage_stats.return_value = {"total_calls": 3, "total_tokens": 45}
    return engine


@pytest.fixture
def mock_logic_engine() -> MagicMock:
    engine = MagicMock()
    engine.execute.return_value = EventResult(
        event_id=1,
        event_name="Test Event B",
        detected=True,
        confidence=0.9,
        summary="Logic chain detected",
    )
    return engine


@pytest.fixture
def mock_report_generator() -> MagicMock:
    generator = MagicMock()
    report = Report(
        video_info=VideoMetadata(
            file_path="test.mp4",
            file_name="test.mp4",
            duration_sec=10.0,
            fps=30.0,
            total_frames=300,
            width=640,
            height=480,
        ),
        scene_summary=SceneInfo(road_count=2),
        binary_encoding=BinaryEncoding(encoding_string="1_1", detected_events=[0, 1]),
    )
    generator.generate.return_value = report
    return generator


@pytest.fixture
def mock_external_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.load_cv_tracks.return_value = {
        "track_1": MagicMock(track_id="track_1", road_id=0),
    }
    adapter.cross_validate_direction.return_value = [
        EventInstance(
            event_id=0,
            event_name="Test Event A",
            confidence=0.9,
        )
    ]
    return adapter


@pytest.fixture
def orchestrator(
    mock_config_manager: MagicMock,
    mock_video_preprocessor: MagicMock,
    mock_vlm_engine: MagicMock,
    mock_logic_engine: MagicMock,
    mock_report_generator: MagicMock,
    mock_external_adapter: MagicMock,
) -> AnalysisOrchestrator:
    return AnalysisOrchestrator(
        config_manager=mock_config_manager,
        video_preprocessor=mock_video_preprocessor,
        vlm_engine=mock_vlm_engine,
        logic_engine=mock_logic_engine,
        report_generator=mock_report_generator,
        external_adapter=mock_external_adapter,
    )


@pytest.fixture
def temp_video(tmp_path: Path) -> str:
    """Create a temporary synthetic video file."""
    video_path = str(tmp_path / "test_video.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(video_path, fourcc, 30.0, (640, 480))
    for _ in range(30):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return video_path


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFromConfigDir:
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.ConfigManager")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.VideoPreprocessor")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.VLMInferenceEngine")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.LogicEngine")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.ReportGenerator")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.ExternalAdapter")
    def test_factory_creates_orchestrator(
        self,
        mock_ext_cls: MagicMock,
        mock_report_cls: MagicMock,
        mock_logic_cls: MagicMock,
        mock_vlm_cls: MagicMock,
        mock_pre_cls: MagicMock,
        mock_config_cls: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_config.load_all.return_value = SystemConfig(
            llm_provider=LLMProviderConfig(provider="anthropic", api_key="test"),
            sampling=SamplingConfig(),
            output_dir="./output",
            save_debug_frames=False,
        )
        mock_config_cls.return_value = mock_config

        orch = AnalysisOrchestrator.from_config_dir("/fake/config")
        assert isinstance(orch, AnalysisOrchestrator)
        assert orch.config_manager is mock_config
        mock_config_cls.assert_called_once_with("/fake/config")
        mock_config.load_all.assert_called_once()


# ---------------------------------------------------------------------------
# Main analyze flow
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_full_pipeline_returns_report(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)
        orchestrator.video_preprocessor.process.assert_called_once_with(temp_video)
        orchestrator.report_generator.generate.assert_called_once()

    def test_pipeline_without_cv_tracks(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        report = orchestrator.analyze(temp_video, cv_tracks_path=None)
        assert isinstance(report, Report)
        orchestrator.external_adapter.load_cv_tracks.assert_not_called()

    def test_pipeline_with_cv_tracks(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
        mock_vlm_engine: MagicMock,
    ) -> None:
        # Return instances so cross-validation is triggered
        mock_vlm_engine.call.return_value = LLMResponse(
            success=True,
            raw_text='{"detected": true, "confidence": 0.85, "instances": [{"start_time_sec": 1.0, "end_time_sec": 2.0, "confidence": 0.8}]}',
            parsed_data={
                "detected": True,
                "confidence": 0.85,
                "instances": [
                    {"start_time_sec": 1.0, "end_time_sec": 2.0, "confidence": 0.8}
                ],
            },
            model="test-model",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        tracks_path = "/fake/tracks.json"
        report = orchestrator.analyze(temp_video, cv_tracks_path=tracks_path)
        assert isinstance(report, Report)
        orchestrator.external_adapter.load_cv_tracks.assert_called_once_with(tracks_path)
        orchestrator.external_adapter.cross_validate_direction.assert_called_once()

    def test_scene_understanding_uses_fallback_when_template_missing(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
        mock_config_manager: MagicMock,
    ) -> None:
        mock_config_manager.get_prompt_template.side_effect = KeyError("not found")
        report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)
        # VLM engine should still be called with a fallback template
        assert orchestrator.vlm_engine.call.call_count >= 1

    def test_inactive_events_skipped(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        report = orchestrator.analyze(temp_video)
        # event_id=2 is inactive, so only 2 active categories should be processed
        categories = orchestrator.config_manager.get_event_categories()
        active_count = sum(1 for c in categories if c.is_active)
        # Each active category triggers either direct_vlm or logic_chain
        total_event_calls = (
            orchestrator.vlm_engine.call.call_count
            + orchestrator.logic_engine.execute.call_count
        )
        # Scene understanding also calls VLM, so subtract 1
        assert total_event_calls - 1 == active_count

    def test_event_detection_error_handled_gracefully(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
        mock_vlm_engine: MagicMock,
    ) -> None:
        # Make direct_vlm detection fail for one event
        mock_vlm_engine.call.side_effect = [
            # First call: scene understanding
            LLMResponse(
                success=True,
                raw_text='{"roads": [], "traffic_density": "low"}',
                parsed_data={"roads": [], "traffic_density": "low"},
                model="test",
                total_tokens=10,
            ),
            # Second call: event detection raises
            Exception("VLM explosion"),
        ]
        # Should not raise; returns report with failed event marked undetected
        report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)

    def test_cross_validation_error_handled_gracefully(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
        mock_external_adapter: MagicMock,
    ) -> None:
        mock_external_adapter.cross_validate_direction.side_effect = Exception("CV error")
        report = orchestrator.analyze(temp_video, cv_tracks_path="/fake/tracks.json")
        assert isinstance(report, Report)


# ---------------------------------------------------------------------------
# Scene understanding
# ---------------------------------------------------------------------------


class TestSceneUnderstanding:
    def test_scene_info_parsed_from_vlm_response(
        self,
        orchestrator: AnalysisOrchestrator,
    ) -> None:
        keyframes = KeyframeSequence(
            coarse_frames=[Keyframe(frame_id=0, timestamp_sec=0.0, image_path="/tmp/f.jpg")]
        )
        orchestrator.vlm_engine.call.return_value = LLMResponse(
            success=True,
            raw_text='{"roads": [{"road_id": 0, "name": "Main"}], "weather": "clear"}',
            parsed_data={
                "roads": [{"road_id": 0, "name": "Main"}],
                "weather": "clear",
                "lighting": "day",
                "traffic_density": "medium",
                "confidence": 0.9,
            },
            total_tokens=10,
        )
        scene_info = orchestrator._scene_understanding(keyframes)
        assert scene_info.road_count == 1
        assert scene_info.weather == "clear"
        assert scene_info.traffic_density == "medium"
        assert scene_info.confidence == 0.9

    def test_scene_info_failure_returns_default(
        self,
        orchestrator: AnalysisOrchestrator,
    ) -> None:
        keyframes = KeyframeSequence(coarse_frames=[])
        orchestrator.vlm_engine.call.return_value = LLMResponse(
            success=False,
            raw_text="Bad response",
            total_tokens=0,
        )
        scene_info = orchestrator._scene_understanding(keyframes)
        assert scene_info.road_count == 0
        assert scene_info.weather == "unknown"


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------


class TestDetectEvents:
    def test_direct_vlm_detection(
        self,
        orchestrator: AnalysisOrchestrator,
        mock_vlm_engine: MagicMock,
    ) -> None:
        mock_vlm_engine.call.return_value = LLMResponse(
            success=True,
            raw_text='{"detected": true, "confidence": 0.8, "instances": []}',
            parsed_data={"detected": True, "confidence": 0.8, "instances": []},
            total_tokens=10,
        )
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Direct Event",
            name_zh="直接事件",
            description="Direct detection",
            detection_mode="direct_vlm",
            is_active=True,
        )
        context = AnalysisContext(keyframes=KeyframeSequence(coarse_frames=[]))
        result = orchestrator._detect_direct_vlm(category, context)
        assert result.detected is True
        assert result.confidence == 0.8

    def test_direct_vlm_with_instances(
        self,
        orchestrator: AnalysisOrchestrator,
    ) -> None:
        orchestrator.vlm_engine.call.return_value = LLMResponse(
            success=True,
            raw_text='{"detected": true, "instances": [{"start_time_sec": 1.0, "end_time_sec": 2.0, "confidence": 0.7}]}',
            parsed_data={
                "detected": True,
                "instances": [
                    {"start_time_sec": 1.0, "end_time_sec": 2.0, "confidence": 0.7, "description": "test"}
                ],
                "confidence": 0.75,
            },
            total_tokens=10,
        )
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Direct Event",
            name_zh="直接事件",
            description="Direct detection",
            detection_mode="direct_vlm",
            is_active=True,
        )
        context = AnalysisContext(
            keyframes=KeyframeSequence(
                coarse_frames=[Keyframe(frame_id=0, timestamp_sec=0.0, image_path="/tmp/f.jpg")]
            )
        )
        result = orchestrator._detect_direct_vlm(category, context)
        assert result.detected is True
        assert len(result.instances) == 1
        assert result.instances[0].confidence == 0.7

    def test_logic_chain_detection(
        self,
        orchestrator: AnalysisOrchestrator,
    ) -> None:
        category = EventCategory(
            event_id=1,
            event_code="B",
            name="Logic Event",
            name_zh="逻辑事件",
            description="Logic detection",
            detection_mode="logic_chain",
            logic_chain_id="test_chain",
            is_active=True,
        )
        context = AnalysisContext()
        result = orchestrator._detect_logic_chain(category, context)
        assert result.detected is True
        orchestrator.logic_engine.execute.assert_called_once()

    def test_unknown_detection_mode(
        self,
        orchestrator: AnalysisOrchestrator,
        mock_config_manager: MagicMock,
    ) -> None:
        # Bypass enum validation to test the unknown-mode branch
        category = EventCategory.model_construct(
            event_id=0,
            event_code="Z",
            name="Unknown",
            name_zh="未知",
            description="Unknown mode",
            detection_mode="unknown_mode",  # type: ignore[arg-type]
            is_active=True,
        )
        mock_config_manager.get_event_categories.return_value = [category]
        context = AnalysisContext()
        result = orchestrator._detect_events(context)
        unknown_result = next(r for r in result if r.event_name == "Unknown")
        assert unknown_result.detected is False
        assert "Unknown detection mode" in unknown_result.summary


# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------


class TestCrossValidate:
    def test_cross_validate_with_tracks(
        self,
        orchestrator: AnalysisOrchestrator,
        mock_external_adapter: MagicMock,
    ) -> None:
        event_results = [
            EventResult(
                event_id=0,
                event_name="Test Event A",
                detected=True,
                instances=[
                    EventInstance(
                        event_id=0,
                        event_name="Test Event A",
                        confidence=0.8,
                    )
                ],
            )
        ]
        context = AnalysisContext(
            cv_tracks={"t1": Track(track_id="t1", road_id=0)},
            scene_understanding=SceneInfo(roads=[]),
            video_meta=VideoMetadata(
                file_path="test.mp4",
                file_name="test.mp4",
                duration_sec=10.0,
                fps=30.0,
                total_frames=300,
                width=640,
                height=480,
            ),
        )
        results = orchestrator._cross_validate(event_results, context)
        mock_external_adapter.cross_validate_direction.assert_called_once()
        assert len(results) == 1

    def test_cross_validate_skips_undetected_events(
        self,
        orchestrator: AnalysisOrchestrator,
        mock_external_adapter: MagicMock,
    ) -> None:
        event_results = [
            EventResult(
                event_id=0,
                event_name="Test Event A",
                detected=False,
                instances=[],
            )
        ]
        context = AnalysisContext(cv_tracks={"t1": Track(track_id="t1", road_id=0)})
        results = orchestrator._cross_validate(event_results, context)
        mock_external_adapter.cross_validate_direction.assert_not_called()
        assert results[0].detected is False

    def test_cross_validate_no_adapter_returns_unchanged(
        self,
        orchestrator: AnalysisOrchestrator,
    ) -> None:
        orchestrator.external_adapter = None
        event_results = [
            EventResult(event_id=0, event_name="A", detected=True, instances=[])
        ]
        context = AnalysisContext(cv_tracks={"t1": Track(track_id="t1", road_id=0)})
        results = orchestrator._cross_validate(event_results, context)
        assert results == event_results


# ---------------------------------------------------------------------------
# Video metadata extraction
# ---------------------------------------------------------------------------


class TestExtractVideoMeta:
    def test_extracts_metadata(self, tmp_path: Path) -> None:
        video_path = str(tmp_path / "meta_test.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(video_path, fourcc, 25.0, (640, 480))
        for _ in range(100):
            writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
        writer.release()

        meta = AnalysisOrchestrator._extract_video_meta(video_path)
        assert meta.file_name == "meta_test.mp4"
        assert meta.width == 640
        assert meta.height == 480
        assert meta.fps == 25.0
        assert meta.total_frames == 100
        assert abs(meta.duration_sec - 4.0) < 0.1

    def test_handles_zero_fps_gracefully(self, tmp_path: Path) -> None:
        # Create an image file and rename it to simulate a bad video
        img_path = str(tmp_path / "bad_video.mp4")
        # Write minimal invalid data
        Path(img_path).write_bytes(b"\x00" * 100)
        # cv2.VideoCapture will report 0 fps for this
        meta = AnalysisOrchestrator._extract_video_meta(img_path)
        assert meta.duration_sec == 0.0
        assert meta.file_name == "bad_video.mp4"
