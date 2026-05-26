"""Unit tests for YOLOVehicleTracker."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from traffic_analyzer.core.tracking_models import TrackedVehicle, VehicleTrajectory
from traffic_analyzer.core.vehicle_tracker import YOLOVehicleTracker
from traffic_analyzer.models.schemas import AnalysisContext, SceneInfo, VideoMetadata


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker() -> YOLOVehicleTracker:
    return YOLOVehicleTracker(
        model_path="/fake/yolo.pt",
        target_fps=5.0,
        device="cpu",
        confidence_threshold=0.3,
    )


@pytest.fixture
def mock_video_meta(tmp_path: Path) -> VideoMetadata:
    # Create a dummy video file so Path.exists() passes
    dummy_video = tmp_path / "test.mp4"
    dummy_video.write_bytes(b"dummy")
    return VideoMetadata(
        file_path=str(dummy_video),
        file_name="test.mp4",
        duration_sec=10.0,
        fps=25.0,
        total_frames=250,
        width=1920,
        height=1080,
        codec="h264",
        bitrate=0,
    )


@pytest.fixture
def mock_context(mock_video_meta: VideoMetadata) -> AnalysisContext:
    return AnalysisContext(video_meta=mock_video_meta)


# ---------------------------------------------------------------------------
# _determine_roi
# ---------------------------------------------------------------------------


def test_determine_roi_with_scene_left(tracker: YOLOVehicleTracker) -> None:
    scene = SceneInfo(
        roads=[
            {
                "road_id": 1,
                "has_emergency_lane": True,
                "emergency_lane_side": "left",
                "normal_direction": "toward_bottom",
            }
        ]
    )
    roi = tracker._determine_roi(1920, 1080, scene)
    assert roi == (0, 0, 288, 1080)  # 15% of 1920 = 288


def test_determine_roi_with_scene_right(tracker: YOLOVehicleTracker) -> None:
    scene = SceneInfo(
        roads=[
            {
                "road_id": 1,
                "has_emergency_lane": True,
                "emergency_lane_side": "right",
                "normal_direction": "toward_bottom",
            }
        ]
    )
    roi = tracker._determine_roi(1920, 1080, scene)
    assert roi == (1920 - 288, 0, 288, 1080)


def test_determine_roi_fallback(tracker: YOLOVehicleTracker) -> None:
    roi = tracker._determine_roi(1920, 1080, None)
    assert roi == (0, 0, 288, 1080)


def test_determine_roi_no_emergency_lane_info(tracker: YOLOVehicleTracker) -> None:
    scene = SceneInfo(roads=[{"road_id": 1, "has_emergency_lane": False}])
    roi = tracker._determine_roi(1920, 1080, scene)
    assert roi == (0, 0, 288, 1080)


# ---------------------------------------------------------------------------
# _get_normal_direction
# ---------------------------------------------------------------------------


def test_get_normal_direction_with_scene(tracker: YOLOVehicleTracker) -> None:
    scene = SceneInfo(
        roads=[
            {
                "road_id": 1,
                "normal_direction": "toward_top",
                "has_emergency_lane": True,
            }
        ]
    )
    assert tracker._get_normal_direction(scene) == "toward_top"


def test_get_normal_direction_fallback(tracker: YOLOVehicleTracker) -> None:
    assert tracker._get_normal_direction(None) == "toward_bottom"


def test_get_normal_direction_unknown_road(tracker: YOLOVehicleTracker) -> None:
    scene = SceneInfo(
        roads=[
            {
                "road_id": 1,
                "normal_direction": "unknown",
                "has_emergency_lane": True,
            }
        ]
    )
    assert tracker._get_normal_direction(scene) == "toward_bottom"


# ---------------------------------------------------------------------------
# _compute_iou
# ---------------------------------------------------------------------------


def test_compute_iou_identical(tracker: YOLOVehicleTracker) -> None:
    box = np.array([0, 0, 10, 10])
    assert tracker._compute_iou(box, box) == pytest.approx(1.0)


def test_compute_iou_no_overlap(tracker: YOLOVehicleTracker) -> None:
    box1 = np.array([0, 0, 10, 10])
    box2 = np.array([20, 20, 30, 30])
    assert tracker._compute_iou(box1, box2) == pytest.approx(0.0)


def test_compute_iou_partial(tracker: YOLOVehicleTracker) -> None:
    box1 = np.array([0, 0, 10, 10])
    box2 = np.array([5, 5, 15, 15])
    # intersection = 5x5 = 25, union = 100 + 100 - 25 = 175
    assert tracker._compute_iou(box1, box2) == pytest.approx(25 / 175)


def test_compute_iou_zero_area(tracker: YOLOVehicleTracker) -> None:
    box1 = np.array([0, 0, 0, 10])
    box2 = np.array([0, 0, 10, 10])
    assert tracker._compute_iou(box1, box2) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _compute_avg_speed
# ---------------------------------------------------------------------------


def test_compute_avg_speed_basic(tracker: YOLOVehicleTracker) -> None:
    positions = [
        (0.0, 0.0, 0.0),
        (1.0, 0.1, 0.0),  # dx=0.1, dy=0, dist=0.1, dt=1.0
    ]
    assert tracker._compute_avg_speed(positions) == pytest.approx(0.1)


def test_compute_avg_speed_diagonal(tracker: YOLOVehicleTracker) -> None:
    positions = [
        (0.0, 0.0, 0.0),
        (1.0, 0.3, 0.4),  # dx=0.3, dy=0.4, dist=0.5, dt=1.0
    ]
    assert tracker._compute_avg_speed(positions) == pytest.approx(0.5)


def test_compute_avg_speed_multiple_steps(tracker: YOLOVehicleTracker) -> None:
    positions = [
        (0.0, 0.0, 0.0),
        (1.0, 0.1, 0.0),  # dist=0.1
        (2.0, 0.3, 0.0),  # dist=0.2
    ]
    # total_dist=0.3, total_time=2.0
    assert tracker._compute_avg_speed(positions) == pytest.approx(0.15)


def test_compute_avg_speed_insufficient(tracker: YOLOVehicleTracker) -> None:
    assert tracker._compute_avg_speed([(0.0, 0.0, 0.0)]) == 0.0


def test_compute_avg_speed_zero_dt(tracker: YOLOVehicleTracker) -> None:
    positions = [
        (0.0, 0.0, 0.0),
        (0.0, 0.1, 0.0),  # dt=0, should be skipped
    ]
    assert tracker._compute_avg_speed(positions) == 0.0


# ---------------------------------------------------------------------------
# _classify_vehicle_direction
# ---------------------------------------------------------------------------


def make_vehicle(positions: List[Tuple[float, float, float]]) -> TrackedVehicle:
    traj = VehicleTrajectory(track_id=1, positions=positions)
    return TrackedVehicle(track_id=1, trajectory=traj)


def test_classify_toward_bottom_reversing(tracker: YOLOVehicleTracker) -> None:
    """Normal=toward_bottom (down). Vehicle moves up = reversing."""
    v = make_vehicle([
        (0.0, 0.1, 0.8),
        (1.0, 0.1, 0.6),
        (2.0, 0.1, 0.4),
    ])
    tracker._classify_vehicle_direction(v, "toward_bottom")
    assert v.direction_classification == "reversing"
    assert v.direction_confidence > 0.0
    assert "reversing" in v.summary


def test_classify_toward_bottom_normal(tracker: YOLOVehicleTracker) -> None:
    """Normal=toward_bottom (down). Vehicle moves down = normal."""
    v = make_vehicle([
        (0.0, 0.1, 0.2),
        (1.0, 0.1, 0.4),
        (2.0, 0.1, 0.6),
    ])
    tracker._classify_vehicle_direction(v, "toward_bottom")
    assert v.direction_classification == "normal"
    assert v.direction_confidence > 0.0
    assert "normal" in v.summary


def test_classify_toward_top_reversing(tracker: YOLOVehicleTracker) -> None:
    """Normal=toward_top (up). Vehicle moves down = reversing."""
    v = make_vehicle([
        (0.0, 0.1, 0.2),
        (1.0, 0.1, 0.4),
        (2.0, 0.1, 0.6),
    ])
    tracker._classify_vehicle_direction(v, "toward_top")
    assert v.direction_classification == "reversing"
    assert "reversing" in v.summary


def test_classify_toward_top_normal(tracker: YOLOVehicleTracker) -> None:
    """Normal=toward_top (up). Vehicle moves up = normal."""
    v = make_vehicle([
        (0.0, 0.1, 0.8),
        (1.0, 0.1, 0.6),
        (2.0, 0.1, 0.4),
    ])
    tracker._classify_vehicle_direction(v, "toward_top")
    assert v.direction_classification == "normal"
    assert "normal" in v.summary


def test_classify_stationary(tracker: YOLOVehicleTracker) -> None:
    """All positions nearly identical = stationary."""
    v = make_vehicle([
        (0.0, 0.1, 0.5),
        (1.0, 0.1, 0.5001),
        (2.0, 0.1, 0.5002),
    ])
    tracker._classify_vehicle_direction(v, "toward_bottom")
    assert v.direction_classification == "stationary"
    assert "stationary" in v.summary


def test_classify_insufficient_positions(tracker: YOLOVehicleTracker) -> None:
    v = make_vehicle([(0.0, 0.1, 0.5)])
    tracker._classify_vehicle_direction(v, "toward_bottom")
    assert v.direction_classification == "uncertain"
    assert "insufficient" in v.summary


def test_classify_unknown_normal(tracker: YOLOVehicleTracker) -> None:
    v = make_vehicle([
        (0.0, 0.1, 0.2),
        (1.0, 0.1, 0.4),
        (2.0, 0.1, 0.6),
    ])
    tracker._classify_vehicle_direction(v, "unknown")
    assert v.direction_classification == "uncertain"
    assert "unknown normal" in v.summary


# ---------------------------------------------------------------------------
# _build_vehicles
# ---------------------------------------------------------------------------


def test_build_vehicles_filters_short_tracks(tracker: YOLOVehicleTracker) -> None:
    raw_tracks = {
        1: [
            (0, 0.0, np.array([0.1, 0.1, 0.2, 0.2]), 2),
            (1, 0.2, np.array([0.11, 0.1, 0.21, 0.2]), 2),
        ],  # length 2 < MIN_TRACK_LENGTH=3
        2: [
            (0, 0.0, np.array([0.5, 0.5, 0.6, 0.6]), 7),
            (1, 0.2, np.array([0.51, 0.5, 0.61, 0.6]), 7),
            (2, 0.4, np.array([0.52, 0.5, 0.62, 0.6]), 7),
        ],  # length 3 >= MIN_TRACK_LENGTH
    }
    vehicles = tracker._build_vehicles(raw_tracks, 1920, 1080)
    assert len(vehicles) == 1
    assert vehicles[0].track_id == 2
    assert vehicles[0].vehicle_type == "truck"


def test_build_vehicles_class_vote(tracker: YOLOVehicleTracker) -> None:
    raw_tracks = {
        1: [
            (0, 0.0, np.array([0.1, 0.1, 0.2, 0.2]), 2),  # car
            (1, 0.2, np.array([0.11, 0.1, 0.21, 0.2]), 2),  # car
            (2, 0.4, np.array([0.12, 0.1, 0.22, 0.2]), 7),  # truck
        ],
    }
    vehicles = tracker._build_vehicles(raw_tracks, 1920, 1080)
    assert len(vehicles) == 1
    assert vehicles[0].vehicle_type == "car"  # majority vote


def test_build_vehicles_empty(tracker: YOLOVehicleTracker) -> None:
    assert tracker._build_vehicles({}, 1920, 1080) == []


# ---------------------------------------------------------------------------
# _run_tracking (Hungarian matching)
# ---------------------------------------------------------------------------


def test_run_tracking_first_frame_creates_tracks(tracker: YOLOVehicleTracker) -> None:
    detections = [
        {"bbox": np.array([10, 10, 50, 50]), "cls": 2, "conf": 0.8},
        {"bbox": np.array([100, 100, 150, 150]), "cls": 7, "conf": 0.9},
    ]
    result = tracker._run_tracking(detections, frame_id=0)
    assert len(result) == 2
    assert set(result.keys()) == {1, 2}


def test_run_tracking_same_object_persists(tracker: YOLOVehicleTracker) -> None:
    # First frame
    dets1 = [{"bbox": np.array([10, 10, 50, 50]), "cls": 2, "conf": 0.8}]
    result1 = tracker._run_tracking(dets1, frame_id=0)
    assert len(result1) == 1
    track_id = list(result1.keys())[0]

    # Second frame: same object moved slightly
    dets2 = [{"bbox": np.array([12, 12, 52, 52]), "cls": 2, "conf": 0.8}]
    result2 = tracker._run_tracking(dets2, frame_id=1)
    assert len(result2) == 1
    assert list(result2.keys())[0] == track_id


def test_run_tracking_new_object_gets_new_id(tracker: YOLOVehicleTracker) -> None:
    # First frame: one object
    dets1 = [{"bbox": np.array([10, 10, 50, 50]), "cls": 2, "conf": 0.8}]
    result1 = tracker._run_tracking(dets1, frame_id=0)
    existing_id = list(result1.keys())[0]

    # Second frame: two objects (original + new)
    dets2 = [
        {"bbox": np.array([12, 12, 52, 52]), "cls": 2, "conf": 0.8},
        {"bbox": np.array([200, 200, 250, 250]), "cls": 3, "conf": 0.9},
    ]
    result2 = tracker._run_tracking(dets2, frame_id=1)
    assert len(result2) == 2
    ids = set(result2.keys())
    assert existing_id in ids
    assert ids - {existing_id}  # there is a new ID


def test_run_tracking_missed_frames_remove_stale(tracker: YOLOVehicleTracker) -> None:
    tracker.TRACK_BUFFER = 2  # small for testing

    # First frame
    dets1 = [{"bbox": np.array([10, 10, 50, 50]), "cls": 2, "conf": 0.8}]
    result1 = tracker._run_tracking(dets1, frame_id=0)
    track_id = list(result1.keys())[0]

    # Miss 3 frames (no detections)
    for i in range(1, 4):
        result = tracker._run_tracking([], frame_id=i)

    # Track should be removed after exceeding TRACK_BUFFER
    assert track_id not in result
    assert len(result) == 0


def test_run_tracking_frame_backward_resets(tracker: YOLOVehicleTracker) -> None:
    # First frame
    dets1 = [{"bbox": np.array([10, 10, 50, 50]), "cls": 2, "conf": 0.8}]
    tracker._run_tracking(dets1, frame_id=5)
    assert len(tracker._track_states) == 1

    # Frame goes backward (new video)
    dets2 = [{"bbox": np.array([100, 100, 150, 150]), "cls": 3, "conf": 0.9}]
    result2 = tracker._run_tracking(dets2, frame_id=0)
    assert len(result2) == 1
    # Should be a fresh track ID starting from 1
    assert list(result2.keys()) == [1]


# ---------------------------------------------------------------------------
# detect() error cases
# ---------------------------------------------------------------------------


def test_detect_no_video_meta(tracker: YOLOVehicleTracker) -> None:
    context = AnalysisContext()
    result = tracker.detect(context)
    assert result.error_message == "No video metadata in context"


def test_detect_video_not_found(tracker: YOLOVehicleTracker) -> None:
    meta = VideoMetadata(
        file_path="/nonexistent/video.mp4",
        file_name="video.mp4",
        duration_sec=10.0,
        fps=25.0,
        total_frames=250,
        width=1920,
        height=1080,
    )
    context = AnalysisContext(video_meta=meta)
    result = tracker.detect(context)
    assert "not found" in result.error_message.lower() or "Video file not found" in result.error_message


# ---------------------------------------------------------------------------
# detect() integration (mocked cv2 + YOLO)
# ---------------------------------------------------------------------------


def test_detect_success(mock_context: AnalysisContext, tracker: YOLOVehicleTracker) -> None:
    """Full detect() flow with mocked video and YOLO."""

    # Mock YOLO model
    mock_model = MagicMock()

    def mock_predict(frame, **kwargs):
        mock_result = MagicMock()
        # Simulate one detection: car at left edge (emergency lane)
        mock_result.boxes = MagicMock()

        # Wrap numpy arrays to have .cpu() method (like torch tensors)
        class _TensorLike:
            def __init__(self, arr):
                self._arr = arr
            def cpu(self):
                return self
            def numpy(self):
                return self._arr
            def item(self):
                return self._arr.item()

        mock_result.boxes.xyxy = [_TensorLike(np.array([50, 100, 150, 200]))]
        mock_result.boxes.cls = [_TensorLike(np.array(2))]
        mock_result.boxes.conf = [_TensorLike(np.array(0.85))]
        mock_result.boxes.__len__ = lambda self: 1
        # Support indexing: boxes.xyxy[i] returns the tensor
        mock_result.boxes.__getitem__ = lambda self, idx: self.xyxy[idx]
        return [mock_result]

    mock_model.predict = mock_predict
    tracker._model = mock_model

    # Mock cv2.VideoCapture
    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    # Use numeric constants directly to avoid cv2 reference in lambda
    _fps = 5
    _frame_count = 7
    _width = 3
    _height = 4
    mock_cap.get.side_effect = lambda prop: {
        _fps: 25.0,
        _frame_count: 25,
        _width: 1920,
        _height: 1080,
    }.get(prop, 0)

    # Return 10 frames then stop
    frame_count = [0]
    def mock_read():
        if frame_count[0] < 10:
            frame_count[0] += 1
            # Create a dummy frame
            return (True, np.zeros((1080, 1920, 3), dtype=np.uint8))
        return (False, None)

    mock_cap.read = mock_read

    with patch("cv2.VideoCapture", return_value=mock_cap):
        result = tracker.detect(mock_context)

    assert result.error_message == ""
    # interval = max(1, round(25/5)) = 5
    # 10 frames (0-9), interval=5 -> frames 0 and 5 -> 2 frames processed
    assert result.frames_processed == 2
    assert result.normal_direction == "toward_bottom"
    assert result.roi_bounds[2] == 288  # 15% of 1920
