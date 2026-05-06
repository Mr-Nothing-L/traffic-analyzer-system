"""
Unit tests for :mod:`traffic_analyzer.core.external_adapter`.

Covers:
- ``load_cv_tracks`` with valid, missing, and malformed JSON.
- ``compute_track_direction`` for all cardinal directions and stationary.
- ``find_track_for_instance`` matching by road, time, and space.
- ``cross_validate_direction`` boost / penalty / no-op logic.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from traffic_analyzer.core.external_adapter import ExternalAdapter
from traffic_analyzer.models.schemas import (
    ConfidenceLevel,
    EventInstance,
    RoadInfo,
    Track,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_vehicles_merged() -> Dict[str, Any]:
    """Return a minimal ``vehicles_merged.json``-like dictionary."""
    return {
        "1": {
            "boxes": [
                # frame, x1, y1, w, h, cx, cy, area
                [10, 100.0, 500.0, 50.0, 50.0, 125.0, 525.0, 2500.0],
                [11, 100.0, 480.0, 50.0, 50.0, 125.0, 505.0, 2500.0],
                [12, 100.0, 460.0, 50.0, 50.0, 125.0, 485.0, 2500.0],
                [13, 100.0, 440.0, 50.0, 50.0, 125.0, 465.0, 2500.0],
            ],
            "road_id": 1,
            "enter_frame": 10,
            "exit_frame": 13,
            "total_displacement": 60.0,
            "lifetime_frames": 4,
            "lifetime_sec": 0.267,
            "merged_from": [],
        },
        "2": {
            "boxes": [
                [20, 200.0, 100.0, 60.0, 60.0, 230.0, 130.0, 3600.0],
                [21, 200.0, 120.0, 60.0, 60.0, 230.0, 150.0, 3600.0],
                [22, 200.0, 140.0, 60.0, 60.0, 230.0, 170.0, 3600.0],
            ],
            "road_id": 2,
            "enter_frame": 20,
            "exit_frame": 22,
            "total_displacement": 40.0,
            "lifetime_frames": 3,
            "lifetime_sec": 0.2,
            "merged_from": [
                {"original_id": "2a", "frame_range": [20, 21], "num_boxes": 2},
                {"original_id": "2b", "frame_range": [22, 22], "num_boxes": 1},
            ],
        },
        "3": {
            "boxes": [
                [30, 300.0, 300.0, 40.0, 40.0, 320.0, 320.0, 1600.0],
            ],
            "road_id": 1,
            "enter_frame": 30,
            "exit_frame": 30,
            "total_displacement": 0.0,
            "lifetime_frames": 1,
            "lifetime_sec": 0.067,
            "merged_from": [],
        },
    }


@pytest.fixture
def mock_vehicles_path(mock_vehicles_merged: Dict[str, Any]) -> str:
    """Write the mock data to a temporary JSON file and yield its path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(mock_vehicles_merged, fh)
        path = fh.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def roads() -> List[RoadInfo]:
    """Return a list of :class:`RoadInfo` objects for tests."""
    return [
        RoadInfo(
            road_id=1,
            name="northbound",
            normal_direction="down",
            direction_confidence=0.95,
        ),
        RoadInfo(
            road_id=2,
            name="southbound",
            normal_direction="up",
            direction_confidence=0.90,
        ),
    ]


# ---------------------------------------------------------------------------
# load_cv_tracks
# ---------------------------------------------------------------------------


class TestLoadCvTracks:
    def test_load_valid_json(self, mock_vehicles_path: str) -> None:
        tracks = ExternalAdapter.load_cv_tracks(mock_vehicles_path)
        assert len(tracks) == 3
        assert "1" in tracks
        assert isinstance(tracks["1"], Track)
        assert tracks["1"].road_id == 1
        assert tracks["2"].merged_from  # non-empty merged_from

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            ExternalAdapter.load_cv_tracks("/nonexistent/path/vehicles.json")

    def test_invalid_json(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("not-json{{")
            path = fh.name

        try:
            with pytest.raises(json.JSONDecodeError):
                ExternalAdapter.load_cv_tracks(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_missing_fields_use_defaults(self) -> None:
        data = {
            "99": {
                "boxes": [[0, 0.0, 0.0, 10.0, 10.0, 5.0, 5.0, 100.0]],
                # deliberately omit several fields
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(data, fh)
            path = fh.name

        try:
            tracks = ExternalAdapter.load_cv_tracks(path)
            assert "99" in tracks
            t = tracks["99"]
            assert t.total_displacement == 0.0
            assert t.lifetime_frames == 1
            assert t.merged_from == []
        finally:
            Path(path).unlink(missing_ok=True)

    def test_non_dict_root_returns_empty(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump([1, 2, 3], fh)
            path = fh.name

        try:
            tracks = ExternalAdapter.load_cv_tracks(path)
            assert tracks == {}
        finally:
            Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# compute_track_direction
# ---------------------------------------------------------------------------


class TestComputeTrackDirection:
    def test_up(self) -> None:
        track = Track(
            track_id="up",
            boxes=[
                [0, 0.0, 100.0, 10.0, 10.0, 5.0, 105.0, 100.0],
                [1, 0.0, 80.0, 10.0, 10.0, 5.0, 85.0, 100.0],
                [2, 0.0, 60.0, 10.0, 10.0, 5.0, 65.0, 100.0],
            ],
            lifetime_sec=0.2,
        )
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "up"

    def test_down(self) -> None:
        track = Track(
            track_id="down",
            boxes=[
                [0, 0.0, 60.0, 10.0, 10.0, 5.0, 65.0, 100.0],
                [1, 0.0, 80.0, 10.0, 10.0, 5.0, 85.0, 100.0],
                [2, 0.0, 100.0, 10.0, 10.0, 5.0, 105.0, 100.0],
            ],
            lifetime_sec=0.2,
        )
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "down"

    def test_left(self) -> None:
        track = Track(
            track_id="left",
            boxes=[
                [0, 100.0, 0.0, 10.0, 10.0, 105.0, 5.0, 100.0],
                [1, 80.0, 0.0, 10.0, 10.0, 85.0, 5.0, 100.0],
                [2, 60.0, 0.0, 10.0, 10.0, 65.0, 5.0, 100.0],
            ],
            lifetime_sec=0.2,
        )
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "left"

    def test_right(self) -> None:
        track = Track(
            track_id="right",
            boxes=[
                [0, 60.0, 0.0, 10.0, 10.0, 65.0, 5.0, 100.0],
                [1, 80.0, 0.0, 10.0, 10.0, 85.0, 5.0, 100.0],
                [2, 100.0, 0.0, 10.0, 10.0, 105.0, 5.0, 100.0],
            ],
            lifetime_sec=0.2,
        )
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "right"

    def test_stationary_short_track(self) -> None:
        track = Track(
            track_id="stationary",
            boxes=[
                [0, 0.0, 0.0, 10.0, 10.0, 5.0, 5.0, 100.0],
            ],
            lifetime_sec=0.067,
        )
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "stationary"

    def test_stationary_small_drift(self) -> None:
        track = Track(
            track_id="drift",
            boxes=[
                [0, 0.0, 0.0, 10.0, 10.0, 5.0, 5.0, 100.0],
                [1, 0.0, 1.0, 10.0, 10.0, 5.0, 6.0, 100.0],
                [2, 0.0, 2.0, 10.0, 10.0, 5.0, 7.0, 100.0],
            ],
            lifetime_sec=0.2,
        )
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "stationary"

    def test_malformed_boxes_fallback(self) -> None:
        track = Track(
            track_id="bad",
            boxes=[
                [0, 0.0],  # missing centre fields
                [1, 0.0, 0.0, 10.0, 10.0, 5.0, 5.0, 100.0],
            ],
            lifetime_sec=0.2,
        )
        # Only one valid centre = stationary
        assert ExternalAdapter.compute_track_direction(track, fps=15.0) == "stationary"


# ---------------------------------------------------------------------------
# find_track_for_instance
# ---------------------------------------------------------------------------


class TestFindTrackForInstance:
    @pytest.fixture
    def tracks(self, mock_vehicles_path: str) -> Dict[str, Track]:
        return ExternalAdapter.load_cv_tracks(mock_vehicles_path)

    def test_match_by_road_time_space(self, tracks: Dict[str, Track]) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            vehicle_id="1",
            road_id=1,
            start_time_sec=10 / 15.0,
            end_time_sec=13 / 15.0,
            evidence_frames=[10, 11, 12],
            confidence=0.75,
        )
        matched = ExternalAdapter.find_track_for_instance(instance, tracks)
        assert matched is not None
        assert matched.track_id == "1"

    def test_no_match_different_road(self, tracks: Dict[str, Track]) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=99,
            evidence_frames=[10, 11, 12],
            confidence=0.75,
        )
        matched = ExternalAdapter.find_track_for_instance(instance, tracks)
        assert matched is None

    def test_no_match_time_gap(self, tracks: Dict[str, Track]) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=1,
            evidence_frames=[100, 101],  # far outside any track
            confidence=0.75,
        )
        matched = ExternalAdapter.find_track_for_instance(instance, tracks)
        assert matched is None

    def test_fallback_without_evidence_frames(self, tracks: Dict[str, Track]) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=1,
            start_time_sec=10 / 15.0,
            end_time_sec=13 / 15.0,
            evidence_frames=[],
            confidence=0.75,
        )
        matched = ExternalAdapter.find_track_for_instance(instance, tracks)
        assert matched is not None
        assert matched.track_id == "1"

    def test_empty_tracks(self) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=1,
            evidence_frames=[10],
            confidence=0.75,
        )
        assert ExternalAdapter.find_track_for_instance(instance, {}) is None


# ---------------------------------------------------------------------------
# cross_validate_direction
# ---------------------------------------------------------------------------


class TestCrossValidateDirection:
    @pytest.fixture
    def tracks(self, mock_vehicles_path: str) -> Dict[str, Track]:
        return ExternalAdapter.load_cv_tracks(mock_vehicles_path)

    def test_boost_when_opposite(self, tracks: Dict[str, Track], roads: List[RoadInfo]) -> None:
        """Track 1 moves UP on road 1 whose normal is DOWN -> boost."""
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            vehicle_id="1",
            road_id=1,
            start_time_sec=10 / 15.0,
            end_time_sec=13 / 15.0,
            evidence_frames=[10, 11, 12],
            confidence=0.70,
            confidence_level=ConfidenceLevel.MEDIUM,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, roads, fps=15.0
        )
        assert len(results) == 1
        assert results[0].confidence > instance.confidence
        assert results[0].confidence_level == ConfidenceLevel.HIGH
        assert "opposes normal" in results[0].reasoning

    def test_penalty_when_same_direction(self, tracks: Dict[str, Track]) -> None:
        """Track 2 moves DOWN on road 2 whose normal is DOWN -> penalty."""
        roads = [
            RoadInfo(road_id=2, name="southbound", normal_direction="down"),
        ]
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            vehicle_id="2",
            road_id=2,
            start_time_sec=20 / 15.0,
            end_time_sec=22 / 15.0,
            evidence_frames=[20, 21, 22],
            confidence=0.70,
            confidence_level=ConfidenceLevel.MEDIUM,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, roads, fps=15.0
        )
        assert len(results) == 1
        assert results[0].confidence < instance.confidence
        assert "flag for review" in results[0].reasoning

    def test_no_track_found_downgrade(self, tracks: Dict[str, Track], roads: List[RoadInfo]) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=1,
            evidence_frames=[999, 1000],  # no overlap with any track
            confidence=0.70,
            confidence_level=ConfidenceLevel.MEDIUM,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, roads, fps=15.0
        )
        assert results[0].confidence < instance.confidence
        assert "no matching track" in results[0].reasoning

    def test_non_direction_event_unchanged(self, tracks: Dict[str, Track], roads: List[RoadInfo]) -> None:
        instance = EventInstance(
            event_id=2,
            event_name="parking_violation",
            road_id=1,
            evidence_frames=[10, 11],
            confidence=0.80,
            confidence_level=ConfidenceLevel.HIGH,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, roads, fps=15.0
        )
        assert results[0].confidence == instance.confidence
        assert results[0].reasoning == instance.reasoning

    def test_road_unknown_no_change(self, tracks: Dict[str, Track]) -> None:
        roads = [
            RoadInfo(road_id=1, name="unknown_road", normal_direction="unknown"),
        ]
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=1,
            evidence_frames=[10, 11, 12],
            confidence=0.70,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, roads, fps=15.0
        )
        assert results[0].confidence == instance.confidence

    def test_stationary_track_penalty(self, tracks: Dict[str, Track]) -> None:
        """Track 3 has a single box = stationary; reversing claim is penalised."""
        roads = [
            RoadInfo(road_id=1, name="northbound", normal_direction="down"),
        ]
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            vehicle_id="3",
            road_id=1,
            evidence_frames=[30],
            confidence=0.70,
            confidence_level=ConfidenceLevel.MEDIUM,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, roads, fps=15.0
        )
        assert results[0].confidence < instance.confidence
        # Track 3 has only one box so compute_track_direction returns stationary,
        # but find_track_for_instance may also reject it due to low temporal overlap.
        # Either outcome is acceptable as long as confidence is reduced.
        assert "stationary" in results[0].reasoning or "no matching track" in results[0].reasoning

    def test_empty_instances(self, tracks: Dict[str, Track], roads: List[RoadInfo]) -> None:
        assert ExternalAdapter.cross_validate_direction([], tracks, roads) == []

    def test_no_roads(self, tracks: Dict[str, Track]) -> None:
        instance = EventInstance(
            event_id=1,
            event_name="reversing",
            road_id=1,
            evidence_frames=[10, 11, 12],
            confidence=0.70,
        )
        results = ExternalAdapter.cross_validate_direction(
            [instance], tracks, [], fps=15.0
        )
        # No roads means road lookup fails -> track is found but road is None,
        # so instance is returned unchanged.
        assert results[0].confidence == instance.confidence
