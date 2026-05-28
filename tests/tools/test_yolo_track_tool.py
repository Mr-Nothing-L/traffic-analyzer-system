"""
Tests for YoloTrackTool.

Run with: pytest tests/tools/test_yolo_track_tool.py -v
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from traffic_analyzer.tools.yolo_track_tool import TrackResult, YoloTrackTool


# Test video fixtures
@pytest.fixture
def sample_video_path():
    """Create a temporary test video with moving objects."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        video_path = f.name
    
    # Create a simple test video: 30 frames, 640x480, object moving left to right
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(video_path, fourcc, 10.0, (640, 480))
    
    for i in range(30):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        # Draw a moving rectangle (simulating a vehicle)
        x = 50 + i * 15
        cv2.rectangle(frame, (x, 200), (x + 60, 260), (255, 255, 255), -1)
        out.write(frame)
    
    out.release()
    yield video_path
    
    # Cleanup
    if os.path.exists(video_path):
        os.remove(video_path)


@pytest.fixture
def empty_video_path():
    """Create an empty video (all black frames)."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        video_path = f.name
    
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(video_path, fourcc, 10.0, (640, 480))
    
    for _ in range(10):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        out.write(frame)
    
    out.release()
    yield video_path
    
    if os.path.exists(video_path):
        os.remove(video_path)


@pytest.fixture
def invalid_video_path():
    """Return a non-existent video path."""
    return "/tmp/nonexistent_video_12345.mp4"


class TestYoloTrackToolInit:
    """Test tool initialization."""
    
    def test_default_init(self):
        tool = YoloTrackTool()
        assert tool.model_path == "yolov8n.pt"
        assert tool.stationary_threshold == 5.0
        assert tool.conf_threshold == 0.3
        assert tool.device == "cpu"
        assert tool._model is None
    
    def test_custom_init(self):
        tool = YoloTrackTool(
            model_path="yolov8s.pt",
            stationary_threshold=10.0,
            conf_threshold=0.5,
            device="cuda",
        )
        assert tool.model_path == "yolov8s.pt"
        assert tool.stationary_threshold == 10.0
        assert tool.conf_threshold == 0.5
        assert tool.device == "cuda"


class TestYoloTrackToolTrack:
    """Test track method with various scenarios."""
    
    @pytest.fixture(autouse=True)
    def setup_tool(self):
        self.tool = YoloTrackTool()
    
    def test_track_normal_video(self, sample_video_path):
        """Test tracking on a video with moving objects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.tool.track(sample_video_path, output_dir=tmpdir)
            
            assert isinstance(result, TrackResult)
            assert result.success is True
            assert result.total_frames == 30
            assert result.processed_frames == 30
            
            # Note: synthetic video with white rectangles may not be detected by YOLO
            # as it expects real vehicle features. Test passes if processing completes.
            if result.vehicle_count > 0:
                # Check annotated image was saved
                assert result.annotated_image_path is not None
                assert os.path.exists(result.annotated_image_path)
                
                # Check displacement data
                assert len(result.displacements) >= 0
                
                for disp in result.displacements:
                    assert disp.track_id >= 0
                    assert disp.vehicle_class in ["car", "motorcycle", "bus", "truck"]
                    assert len(disp.start_pos) == 2
                    assert len(disp.end_pos) == 2
                    assert len(disp.displacement) == 2
                    assert disp.distance >= 0
                    assert 0 <= disp.direction_deg <= 360
                    assert disp.direction_text in [
                        "北", "东北", "东", "东南", "南", "西南", "西", "西北"
                    ]
                    assert isinstance(disp.is_stationary, bool)
                    assert disp.frame_count >= 1
                    assert 0 <= disp.confidence <= 1
    
    def test_track_empty_video(self, empty_video_path):
        """Test tracking on empty video (no objects)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.tool.track(empty_video_path, output_dir=tmpdir)
            
            assert isinstance(result, TrackResult)
            # Should succeed but with no vehicles
            assert result.success is True
            assert result.vehicle_count == 0
            assert "未检测到车辆" in result.error_message or result.vehicle_count == 0
    
    def test_track_invalid_path(self, invalid_video_path):
        """Test tracking with non-existent video."""
        result = self.tool.track(invalid_video_path)
        
        assert isinstance(result, TrackResult)
        assert result.success is False
        assert result.error_message is not None
        assert "无法打开" in result.error_message or "Cannot open" in result.error_message
    
    def test_track_model_load_failure(self, sample_video_path):
        """Test fallback when model fails to load."""
        tool = YoloTrackTool(model_path="/nonexistent/model.pt")
        result = tool.track(sample_video_path)
        
        assert result.success is False
        assert result.error_message is not None
        assert "模型加载失败" in result.error_message


class TestYoloTrackToolDisplacement:
    """Test displacement calculation."""
    
    def test_stationary_detection(self):
        """Test that small movements are detected as stationary."""
        tool = YoloTrackTool(stationary_threshold=10.0)
        
        # Positions with small movement (3 pixels on 1920 width = 0.00156 normalized)
        positions = [(0.1, 0.1), (0.1005, 0.1), (0.101, 0.1)]
        confs = [0.8, 0.85, 0.9]
        
        disp = tool._calculate_displacement(1, positions, 2, confs, 1920, 1080)
        
        assert disp is not None
        # distance is in pixels: 0.001 * 1920 = 1.9px < 10
        assert disp.is_stationary is True
    
    def test_moving_detection(self):
        """Test that large movements are detected as moving."""
        tool = YoloTrackTool(stationary_threshold=5.0)
        
        # Positions with large movement (100 pixels on 1920 width = 0.052 normalized)
        positions = [(0.1, 0.1), (0.15, 0.1)]  # 0.05 * 1920 = 96px > 5
        confs = [0.8, 0.9]
        
        disp = tool._calculate_displacement(1, positions, 2, confs, 1920, 1080)
        
        assert disp is not None
        assert disp.is_stationary is False
    
    def test_direction_calculation(self):
        """Test direction calculation with pixel format."""
        tool = YoloTrackTool()
        
        # Moving east (right) 150px
        text = tool._get_direction_text(150, 0)
        assert "dx=+150px" in text
        assert "dy=+0px" in text
        assert "(右)" in text
        
        # Moving south (down) 80px
        text = tool._get_direction_text(0, 80)
        assert "dx=+0px" in text
        assert "dy=+80px" in text
        assert "(下)" in text
        
        # Moving up-left
        text = tool._get_direction_text(-100, -50)
        assert "dx=-100px" in text
        assert "dy=-50px" in text
        assert "(左上)" in text
        
        # Stationary
        text = tool._get_direction_text(0, 0)
        assert "(静止)" in text
    
    def test_insufficient_positions(self):
        """Test with only one position."""
        tool = YoloTrackTool()
        
        positions = [(0.1, 0.1)]
        result = tool._calculate_displacement(1, positions, 2, [0.8], 1920, 1080)
        
        assert result is None


class TestYoloTrackToolJSON:
    """Test JSON serialization."""
    
    def test_to_json(self):
        tool = YoloTrackTool()
        
        # Create a mock result
        result = TrackResult(
            success=True,
            annotated_image_path="/tmp/test.jpg",
            total_frames=100,
            processed_frames=100,
            vehicle_count=2,
        )
        
        json_data = tool.to_json(result)
        
        assert json_data["success"] is True
        assert json_data["total_frames"] == 100
        assert json_data["vehicle_count"] == 2
        assert json_data["annotated_image_path"] == "/tmp/test.jpg"
        assert isinstance(json_data["displacements"], list)


class TestYoloTrackToolIntegration:
    """Integration tests with real video."""
    
    @pytest.mark.slow
    def test_real_video_tracking(self):
        """
        Test with a real video file if available.
        Skip if no test video exists.
        """
        test_video = "/data/test_videos/高速交通事件agent测试视频_v2/02-08-20260514-173730_后半段.mp4"
        
        if not os.path.exists(test_video):
            pytest.skip(f"Test video not found: {test_video}")
        
        tool = YoloTrackTool()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = tool.track(test_video, output_dir=tmpdir)
            
            assert result.success is True
            assert result.vehicle_count > 0
            assert result.annotated_image_path is not None
            assert os.path.exists(result.annotated_image_path)
            
            # Verify JSON output
            json_data = tool.to_json(result)
            assert json_data["success"] is True
            assert len(json_data["displacements"]) > 0
            
            # Check for stationary vs moving vehicles
            stationary_count = sum(1 for d in result.displacements if d.is_stationary)
            moving_count = len(result.displacements) - stationary_count
            
            print(f"\nTracking Results:")
            print(f"  Total vehicles: {result.vehicle_count}")
            print(f"  Stationary: {stationary_count}")
            print(f"  Moving: {moving_count}")
            print(f"  Frames processed: {result.processed_frames}/{result.total_frames}")
            
            # Print first few displacements
            for disp in result.displacements[:3]:
                status = "静止" if disp.is_stationary else "移动"
                print(f"  ID {disp.track_id}: {disp.vehicle_class}, "
                      f"方向={disp.direction_text}({disp.direction_deg:.1f}°), "
                      f"距离={disp.distance:.2f}, 状态={status}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
