"""Tests for ExpertAgent tool calling integration."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from traffic_analyzer.core.expert_agent import ExpertAgent
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCategory,
    LLMResponse,
    PromptTemplate,
    SystemConfig,
    VideoMetadata,
)


@pytest.fixture
def mock_vlm_engine():
    """Mock VLM engine that returns predefined responses."""
    engine = MagicMock()
    return engine


@pytest.fixture
def mock_config_manager():
    """Mock config manager with templates."""
    cm = MagicMock()
    cm.get_prompt_template.return_value = PromptTemplate(
        template_id="test_template",
        name="Test Template",
        system_prompt="You are a test expert.",
        user_prompt="Detect events in these frames.",
        available_tools=["yolo_track_tool"],
    )
    return cm


@pytest.fixture
def analysis_context():
    """Create a basic analysis context."""
    return AnalysisContext(
        video_meta=VideoMetadata(
            file_path="/data/test_videos/test.mp4",
            file_name="test.mp4",
            duration_sec=10.0,
            fps=25.0,
            total_frames=250,
            width=1920,
            height=1080,
        ),
        config=SystemConfig(),
    )


class TestExpertAgentToolCalling:
    """Test ExpertAgent tool calling flow."""

    def test_no_tool_call_when_no_tools_configured(
        self, mock_vlm_engine, mock_config_manager, analysis_context
    ):
        """Expert without tools should not attempt tool calls."""
        category = EventCategory(
            event_id=1,
            event_code="B",
            name="Emergency Lane Occupancy",
            name_zh="应急车道占用",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=[],  # No tools
        )

        # First response - no tool call
        first_response = LLMResponse(
            success=True,
            raw_text='{"detected": false, "summary": "No event"}',
            parsed_data={"detected": False, "summary": "No event"},
        )
        mock_vlm_engine.call.return_value = first_response

        agent = ExpertAgent(category, mock_vlm_engine, mock_config_manager)

        with patch("traffic_analyzer.core.expert_agent.select_event_images", return_value=["img1"]):
            result = agent.detect(analysis_context)

        # Should only call VLM once
        assert mock_vlm_engine.call.call_count == 1
        assert result.detected is False

    def test_tool_call_parsed_and_executed(
        self, mock_vlm_engine, mock_config_manager, analysis_context
    ):
        """Expert with tools should parse tool calls and do second VLM call."""
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Illegal Parking",
            name_zh="违法停车",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=["yolo_track_tool"],
        )

        # First response - contains tool call
        first_response = LLMResponse(
            success=True,
            raw_text='''
            Analysis: I see a suspicious vehicle.
            <tool_call>
            {"tool_name": "yolo_track_tool", "arguments": {"video_path": "/data/test_videos/test.mp4", "stationary_threshold": 5.0}}
            </tool_call>
            ''',
            parsed_data={"detected": False},
        )

        # Second response - final judgment
        second_response = LLMResponse(
            success=True,
            raw_text='{"detected": true, "confidence": 0.9, "summary": "Vehicle is stationary"}',
            parsed_data={"detected": True, "confidence": 0.9, "summary": "Vehicle is stationary"},
        )

        mock_vlm_engine.call.side_effect = [first_response, second_response]

        # Mock tool router
        mock_tool_result = {
            "annotated_image_path": "/data/output/annotated.jpg",
            "displacements": [
                {
                    "track_id": 1,
                    "class": "car",
                    "direction_text": "dx=+2px, dy=+1px (静止)",
                    "distance_pixels": 2.2,
                    "is_stationary": True,
                }
            ],
        }
        mock_tool_response = MagicMock()
        mock_tool_response.success = True
        mock_tool_response.data = mock_tool_result

        with patch("traffic_analyzer.core.expert_agent.select_event_images", return_value=["img1"]):
            with patch("traffic_analyzer.tools.tool_registry.get_default_router") as mock_get_router:
                mock_router = MagicMock()
                mock_router.route.return_value = mock_tool_response
                mock_get_router.return_value = mock_router

                agent = ExpertAgent(category, mock_vlm_engine, mock_config_manager)
                result = agent.detect(analysis_context)

        # Should call VLM twice
        assert mock_vlm_engine.call.call_count == 2
        assert result.detected is True
        assert result.confidence == 0.9

    def test_tool_not_allowed_filtered(
        self, mock_vlm_engine, mock_config_manager, analysis_context
    ):
        """Tool calls for tools not in category.tools should be rejected."""
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Illegal Parking",
            name_zh="违法停车",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=["yolo_track_tool"],  # Only yolo_track_tool allowed
        )

        # Response requests a different tool
        first_response = LLMResponse(
            success=True,
            raw_text='''
            <tool_call>
            {"tool_name": "some_other_tool", "arguments": {}}
            </tool_call>
            ''',
            parsed_data={"detected": False},
        )
        mock_vlm_engine.call.return_value = first_response

        agent = ExpertAgent(category, mock_vlm_engine, mock_config_manager)

        with patch("traffic_analyzer.core.expert_agent.select_event_images", return_value=["img1"]):
            with patch("traffic_analyzer.tools.tool_registry.get_default_router") as mock_get_router:
                mock_router = MagicMock()
                mock_get_router.return_value = mock_router

                result = agent.detect(analysis_context)

        # Should only call VLM once (tool rejected, no second call)
        assert mock_vlm_engine.call.call_count == 1

    def test_video_path_auto_filled(
        self, mock_vlm_engine, mock_config_manager, analysis_context
    ):
        """Tool call with placeholder video_path should be auto-filled from context."""
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Illegal Parking",
            name_zh="违法停车",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=["yolo_track_tool"],
        )

        first_response = LLMResponse(
            success=True,
            raw_text='''
            <tool_call>
            {"tool_name": "yolo_track_tool", "arguments": {"video_path": "{{video_meta.file_path}}", "stationary_threshold": 5.0}}
            </tool_call>
            ''',
            parsed_data={"detected": False},
        )

        second_response = LLMResponse(
            success=True,
            raw_text='{"detected": false, "summary": "No parking"}',
            parsed_data={"detected": False, "summary": "No parking"},
        )

        mock_vlm_engine.call.side_effect = [first_response, second_response]

        mock_tool_response = MagicMock()
        mock_tool_response.success = True
        mock_tool_response.data = {"displacements": []}

        with patch("traffic_analyzer.core.expert_agent.select_event_images", return_value=["img1"]):
            with patch("traffic_analyzer.tools.tool_registry.get_default_router") as mock_get_router:
                mock_router = MagicMock()
                mock_router.route.return_value = mock_tool_response
                mock_get_router.return_value = mock_router

                agent = ExpertAgent(category, mock_vlm_engine, mock_config_manager)
                result = agent.detect(analysis_context)

        # Verify the tool was called with auto-filled video path
        call_args = mock_router.route.call_args[0][0]
        assert call_args.arguments["video_path"] == "/data/test_videos/test.mp4"

    def test_format_tracking_result(self):
        """Test tracking result formatting for prompt injection."""
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Illegal Parking",
            name_zh="违法停车",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=["yolo_track_tool"],
        )

        agent = ExpertAgent(category, MagicMock(), MagicMock())

        result_data = {
            "displacements": [
                {
                    "track_id": 3,
                    "class": "car",
                    "direction_text": "dx=+150px, dy=-80px (向右下)",
                    "distance_pixels": 170.0,
                    "is_stationary": False,
                },
                {
                    "track_id": 7,
                    "class": "truck",
                    "direction_text": "dx=+2px, dy=+1px (静止)",
                    "distance_pixels": 2.2,
                    "is_stationary": True,
                },
            ],
        }

        text = agent._format_tracking_result(result_data)

        assert "track_id=3" in text
        assert "track_id=7" in text
        assert "静止" in text
        assert "向右下" in text
        assert "附带的最后一张图是 YOLO 跟踪标注帧" in text


class TestExpertAgentSecondVLMMCall:
    """Test second VLM call with tool results."""

    def test_second_call_includes_annotated_image(
        self, mock_vlm_engine, mock_config_manager, analysis_context
    ):
        """Second VLM call should include annotated image in image list."""
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Illegal Parking",
            name_zh="违法停车",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=["yolo_track_tool"],
        )

        first_response = LLMResponse(
            success=True,
            raw_text='''
            <tool_call>
            {"tool_name": "yolo_track_tool", "arguments": {"video_path": "/data/test.mp4"}}
            </tool_call>
            ''',
            parsed_data={"detected": False},
        )

        second_response = LLMResponse(
            success=True,
            raw_text='{"detected": true, "confidence": 0.85, "summary": "Found parking"}',
            parsed_data={"detected": True, "confidence": 0.85, "summary": "Found parking"},
        )

        mock_vlm_engine.call.side_effect = [first_response, second_response]

        mock_tool_response = MagicMock()
        mock_tool_response.success = True
        mock_tool_response.data = {
            "annotated_image_path": "/data/output/annotated.jpg",
            "displacements": [],
        }

        with patch("traffic_analyzer.core.expert_agent.select_event_images", return_value=["frame1", "frame2"]):
            with patch("traffic_analyzer.tools.tool_registry.get_default_router") as mock_get_router:
                mock_router = MagicMock()
                mock_router.route.return_value = mock_tool_response
                mock_get_router.return_value = mock_router

                agent = ExpertAgent(category, mock_vlm_engine, mock_config_manager)
                result = agent.detect(analysis_context)

        # Check second call images include annotated image
        second_call_kwargs = mock_vlm_engine.call.call_args_list[1].kwargs
        second_images = second_call_kwargs["images"]
        assert "/data/output/annotated.jpg" in second_images
        assert "frame1" in second_images
        assert "frame2" in second_images

    def test_second_call_prompt_includes_tool_results(
        self, mock_vlm_engine, mock_config_manager, analysis_context
    ):
        """Second VLM call prompt should include tracking data."""
        category = EventCategory(
            event_id=0,
            event_code="A",
            name="Illegal Parking",
            name_zh="违法停车",
            description="Test",
            detection_mode="expert_agent",
            prompt_template_id="test_template",
            tools=["yolo_track_tool"],
        )

        first_response = LLMResponse(
            success=True,
            raw_text='First analysis\n<tool_call>\n{"tool_name": "yolo_track_tool", "arguments": {"video_path": "/data/test.mp4"}}\n</tool_call>',
            parsed_data={"detected": False},
        )

        second_response = LLMResponse(
            success=True,
            raw_text='{"detected": true, "confidence": 0.9, "summary": "Parked"}',
            parsed_data={"detected": True, "confidence": 0.9, "summary": "Parked"},
        )

        mock_vlm_engine.call.side_effect = [first_response, second_response]

        mock_tool_response = MagicMock()
        mock_tool_response.success = True
        mock_tool_response.data = {
            "annotated_image_path": "/data/output/annotated.jpg",
            "displacements": [
                {
                    "track_id": 1,
                    "class": "car",
                    "direction_text": "dx=+2px, dy=+1px (静止)",
                    "distance_pixels": 2.2,
                    "is_stationary": True,
                }
            ],
        }

        with patch("traffic_analyzer.core.expert_agent.select_event_images", return_value=["img1"]):
            with patch("traffic_analyzer.tools.tool_registry.get_default_router") as mock_get_router:
                mock_router = MagicMock()
                mock_router.route.return_value = mock_tool_response
                mock_get_router.return_value = mock_router

                agent = ExpertAgent(category, mock_vlm_engine, mock_config_manager)
                result = agent.detect(analysis_context)

        # Should call VLM twice
        assert mock_vlm_engine.call.call_count == 2
        
        # Check second call prompt includes tool results
        second_call_kwargs = mock_vlm_engine.call.call_args_list[1].kwargs
        second_template = second_call_kwargs["template"]
        assert "YOLO 车辆跟踪结果" in second_template.user_prompt
        assert "track_id=1" in second_template.user_prompt
        assert "上下文 — 第一次分析结论" in second_template.user_prompt
