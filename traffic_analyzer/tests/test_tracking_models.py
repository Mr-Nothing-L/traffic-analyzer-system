"""Tests for vehicle tracking data models."""

from __future__ import annotations

import pytest

from traffic_analyzer.core.tracking_models import (
    TrackedVehicle,
    TrackingEvidence,
    VehicleTrajectory,
)


class TestVehicleTrajectory:
    def test_empty_trajectory_properties(self) -> None:
        traj = VehicleTrajectory(track_id=1)
        assert traj.start_time_sec == 0.0
        assert traj.end_time_sec == 0.0
        assert traj.duration_sec == 0.0
        assert traj.is_in_emergency_lane is False

    def test_trajectory_with_positions(self) -> None:
        traj = VehicleTrajectory(
            track_id=2,
            positions=[
                (0.0, 0.5, 0.5),
                (1.0, 0.6, 0.5),
                (2.0, 0.7, 0.5),
            ],
        )
        assert traj.start_time_sec == 0.0
        assert traj.end_time_sec == 2.0
        assert traj.duration_sec == 2.0
        assert traj.is_in_emergency_lane is False

    def test_emergency_lane_left_edge(self) -> None:
        traj = VehicleTrajectory(
            track_id=3,
            positions=[
                (0.0, 0.05, 0.5),
                (1.0, 0.08, 0.5),
                (2.0, 0.10, 0.5),
            ],
        )
        assert traj.is_in_emergency_lane is True

    def test_emergency_lane_right_edge(self) -> None:
        traj = VehicleTrajectory(
            track_id=4,
            positions=[
                (0.0, 0.95, 0.5),
                (1.0, 0.90, 0.5),
                (2.0, 0.88, 0.5),
            ],
        )
        assert traj.is_in_emergency_lane is True

    def test_emergency_lane_center(self) -> None:
        traj = VehicleTrajectory(
            track_id=5,
            positions=[
                (0.0, 0.5, 0.5),
                (1.0, 0.5, 0.5),
                (2.0, 0.5, 0.5),
            ],
        )
        assert traj.is_in_emergency_lane is False

    def test_emergency_lane_boundary_50_percent(self) -> None:
        """Exactly 50% edge positions should NOT count as emergency lane."""
        traj = VehicleTrajectory(
            track_id=6,
            positions=[
                (0.0, 0.05, 0.5),
                (1.0, 0.50, 0.5),
            ],
        )
        assert traj.is_in_emergency_lane is False


class TestTrackingEvidence:
    def test_empty_evidence_text_formatting(self) -> None:
        evidence = TrackingEvidence()
        text = evidence.to_prompt_text()
        assert "## 跟踪分析结果" in text
        assert "分析帧范围: 0 ~ 0" in text
        assert "跟踪到车辆总数: 0" in text
        assert "逆行/倒车嫌疑车辆" not in text
        assert "错误信息" not in text

    def test_evidence_with_reversing_vehicles(self) -> None:
        traj = VehicleTrajectory(
            track_id=10,
            positions=[(0.0, 0.5, 0.5), (1.0, 0.4, 0.5)],
        )
        rev_vehicle = TrackedVehicle(
            track_id=10,
            trajectory=traj,
            direction_classification="reversing",
            direction_confidence=0.85,
            avg_speed=0.1,
            vehicle_type="car",
        )
        evidence = TrackingEvidence(
            detected=True,
            vehicles=[rev_vehicle],
            reversing_vehicles=[rev_vehicle],
            normal_direction="left_to_right",
            frame_range=(0, 30),
            time_range_sec=(0.0, 1.0),
            frames_processed=30,
        )
        text = evidence.to_prompt_text()
        assert "逆行/倒车嫌疑车辆" in text
        assert "ID=10" in text
        assert "置信度=0.85" in text
        assert "方向判定=reversing" in text
        assert "正常流向: left_to_right" in text

    def test_evidence_with_normal_vehicles_only(self) -> None:
        traj = VehicleTrajectory(
            track_id=20,
            positions=[(0.0, 0.3, 0.5), (1.0, 0.5, 0.5), (2.0, 0.7, 0.5)],
        )
        normal_vehicle = TrackedVehicle(
            track_id=20,
            trajectory=traj,
            direction_classification="normal",
            direction_confidence=0.92,
            avg_speed=0.2,
            vehicle_type="truck",
        )
        evidence = TrackingEvidence(
            detected=True,
            vehicles=[normal_vehicle],
            normal_direction="right_to_left",
            frame_range=(0, 60),
            time_range_sec=(0.0, 2.0),
            frames_processed=60,
        )
        text = evidence.to_prompt_text()
        assert "跟踪到车辆总数: 1" in text
        assert "ID=20" in text
        assert "类型=truck" in text
        assert "方向判定=normal" in text
        assert "逆行/倒车嫌疑车辆" not in text
        assert "(0.300,0.500) -> (0.700,0.500)" in text

    def test_error_message_formatting(self) -> None:
        evidence = TrackingEvidence(
            error_message="YOLO model failed to load",
        )
        text = evidence.to_prompt_text()
        assert "错误信息" in text
        assert "YOLO model failed to load" in text

    def test_roi_bounds_in_output(self) -> None:
        evidence = TrackingEvidence(
            roi_bounds=(100, 200, 300, 400),
        )
        text = evidence.to_prompt_text()
        assert "(100, 200, 300, 400)" in text
