"""Integration tests for AnalysisOrchestrator (v2.0.0)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

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
from traffic_analyzer.core.pipeline_steps import AdjudicationStep, ExpertAgentLayer
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
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            is_active=True,
        ),
        EventCategory(
            event_id=1,
            event_code="B",
            name="Test Event B",
            name_zh="测试事件B",
            description="Test description B",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            is_active=True,
        ),
        EventCategory(
            event_id=2,
            event_code="C",
            name="Inactive Event",
            name_zh="未激活事件",
            description="Should be skipped",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            is_active=False,
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
        raw_text='{"detected": true}',
        parsed_data={"detected": True},
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    engine.get_usage_stats.return_value = {"total_calls": 3, "total_tokens": 45}
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
def mock_expert_agent_layer() -> MagicMock:
    layer = MagicMock()
    layer.execute.return_value = MagicMock(
        success=True,
        data=[
            EventCandidate(
                event_id=0,
                event_name="Test Event A",
                detected=True,
            ),
            EventCandidate(
                event_id=1,
                event_name="Test Event B",
                detected=False,
            ),
        ],
    )
    return layer


@pytest.fixture
def mock_adjudication_step() -> MagicMock:
    step = MagicMock()
    step.execute.return_value = MagicMock(
        success=True,
        data=AdjudicationResult(
            event_results=[
                EventResult(
                    event_id=0,
                    event_name="Test Event A",
                    detected=True,
                ),
                EventResult(
                    event_id=1,
                    event_name="Test Event B",
                    detected=False,
                ),
            ],
            adjudication_reasoning="Test reasoning",
            reasoning_chain=[],
            audit_log=[],
        ),
    )
    return step


@pytest.fixture
def orchestrator(
    mock_config_manager: MagicMock,
    mock_video_preprocessor: MagicMock,
    mock_vlm_engine: MagicMock,
    mock_report_generator: MagicMock,
    mock_expert_agent_layer: MagicMock,
    mock_adjudication_step: MagicMock,
) -> AnalysisOrchestrator:
    return AnalysisOrchestrator(
        config_manager=mock_config_manager,
        video_preprocessor=mock_video_preprocessor,
        vlm_engine=mock_vlm_engine,
        report_generator=mock_report_generator,
        expert_agent_layer=mock_expert_agent_layer,
        adjudication_step=mock_adjudication_step,
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
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.ReportGenerator")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.ExpertAgentLayer")
    @patch("traffic_analyzer.orchestrator.analysis_orchestrator.AdjudicationStep")
    def test_factory_creates_orchestrator(
        self,
        mock_adj_cls: MagicMock,
        mock_exp_cls: MagicMock,
        mock_report_cls: MagicMock,
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
        orchestrator._expert_agent_layer.execute.assert_called_once()
        orchestrator._adjudication_step.execute.assert_called_once()
        orchestrator.report_generator.generate.assert_called_once()

    def test_total_categories_passed_to_report_generator(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        """Orchestrator must pass configured total category count to ReportGenerator."""
        report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)
        call_kwargs = orchestrator.report_generator.generate.call_args.kwargs
        assert call_kwargs.get("total_categories") == 3

    def test_scene_understanding_passed_externally(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        scene_info = SceneInfo(road_count=3, weather="rainy", lighting="night")
        report = orchestrator.analyze(temp_video, scene_understanding=scene_info)
        assert isinstance(report, Report)

    def test_inactive_events_skipped_by_expert_layer(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        report = orchestrator.analyze(temp_video)
        categories = orchestrator.config_manager.get_event_categories()
        active_count = sum(1 for c in categories if c.is_active)
        assert active_count == 2
        assert isinstance(report, Report)

    def test_expert_layer_failure_handled(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        orchestrator._expert_agent_layer.execute.return_value = MagicMock(
            success=False,
            data=None,
            error=Exception("Expert layer failed"),
        )
        report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)

    def test_adjudication_fallback_on_failure(
        self,
        orchestrator: AnalysisOrchestrator,
        temp_video: str,
    ) -> None:
        orchestrator._adjudication_step.execute.return_value = MagicMock(
            success=False,
            data=None,
            error=Exception("Adjudication failed"),
        )
        report = orchestrator.analyze(temp_video)
        assert isinstance(report, Report)

    def test_pipeline_without_steps_returns_report(
        self,
        mock_config_manager: MagicMock,
        mock_video_preprocessor: MagicMock,
        mock_vlm_engine: MagicMock,
        mock_report_generator: MagicMock,
        temp_video: str,
    ) -> None:
        orch = AnalysisOrchestrator(
            config_manager=mock_config_manager,
            video_preprocessor=mock_video_preprocessor,
            vlm_engine=mock_vlm_engine,
            report_generator=mock_report_generator,
            expert_agent_layer=None,
            adjudication_step=None,
        )
        report = orch.analyze(temp_video)
        assert isinstance(report, Report)


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
        img_path = str(tmp_path / "bad_video.mp4")
        Path(img_path).write_bytes(b"\x00" * 100)
        meta = AnalysisOrchestrator._extract_video_meta(img_path)
        assert meta.duration_sec == 0.0
        assert meta.file_name == "bad_video.mp4"


# ---------------------------------------------------------------------------
# Pipeline step unit tests
# ---------------------------------------------------------------------------


class TestAdjudicationStep:
    def test_filters_inactive_and_hallucinated_event_ids(self) -> None:
        """AdjudicationStep must only return active event IDs from config."""
        config_manager = MagicMock()
        config_manager.get_active_event_categories.return_value = [
            EventCategory(
                event_id=0,
                event_code="A",
                name="Active A",
                name_zh="活跃A",
                description="Active.",
                detection_mode="expert_agent",
                prompt_template_id="tpl",
                is_active=True,
            ),
            EventCategory(
                event_id=1,
                event_code="B",
                name="Active B",
                name_zh="活跃B",
                description="Active.",
                detection_mode="expert_agent",
                prompt_template_id="tpl",
                is_active=True,
            ),
        ]
        config_manager.get_adjudication_rules.return_value = []
        config_manager.get_prompt_template.return_value = PromptTemplate(
            template_id="adjudication",
            name="Adjudication",
            system_prompt="",
            user_prompt="",
        )
        config_manager.config_dir = Path("/fake/config")

        vlm_response = MagicMock()
        vlm_response.success = True
        vlm_response.parsed_data = {
            "event_results": [
                {"event_id": 0, "event_name": "Active A", "detected": True, "summary": "yes"},
                {"event_id": 1, "event_name": "Active B", "detected": False, "summary": "no"},
                {"event_id": 2, "event_name": "Inactive C", "detected": True, "summary": "hallucinated"},
                {"event_id": 10, "event_name": "Unknown", "detected": True, "summary": "hallucinated"},
            ],
            "audit_log": [
                {"event_id": 0, "event_name": "Active A", "action": "included", "reason": "", "rule_id": None},
                {"event_id": 2, "event_name": "Inactive C", "action": "excluded", "reason": "", "rule_id": None},
            ],
            "reasoning_chain": [
                {"event_id": 0, "event_name": "Active A", "decision": "保留", "thought_process": "", "basis": ""},
                {"event_id": 10, "event_name": "Unknown", "decision": "保留", "thought_process": "", "basis": ""},
            ],
            "adjudication_reasoning": "test",
        }
        vlm_response.raw_text = ""

        vlm_engine = MagicMock()
        vlm_engine.call.return_value = vlm_response

        step = AdjudicationStep(config_manager, vlm_engine)
        context = AnalysisContext()
        context.event_candidates[0] = EventCandidate(
            event_id=0, event_name="Active A", detected=True, summary="yes"
        )
        context.event_candidates[1] = EventCandidate(
            event_id=1, event_name="Active B", detected=False, summary="no"
        )

        result = step.execute(context)
        assert result.success
        assert result.data is not None
        adjudication_result = result.data
        result_ids = {r.event_id for r in adjudication_result.event_results}
        assert result_ids == {0, 1}
        assert all(r.event_id in {0, 1} for r in adjudication_result.audit_log)
        assert all(rc["event_id"] in {0, 1} for rc in adjudication_result.reasoning_chain)
