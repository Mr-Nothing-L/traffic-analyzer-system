"""
ExternalAdapter module for the traffic analyzer framework.

Provides adapters to ingest CV track data (e.g. from ``merge_tracks.py``)
and cross-validate VLM-detected events against those tracks.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from traffic_analyzer.models.schemas import (
    ConfidenceLevel,
    EventInstance,
    RoadInfo,
    Track,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Spatial/temporal matching thresholds for ``find_track_for_instance``
_DEFAULT_IOU_THRESHOLD = 0.3
_DEFAULT_TIME_OVERLAP_RATIO = 0.5
_DEFAULT_PROXIMITY_PX = 100.0

# Confidence boost / penalty applied during cross-validation
_AGREEMENT_BOOST = 0.15
_DISAGREEMENT_PENALTY = 0.20
_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 1.0


class ExternalAdapter:
    """Adapter that bridges external CV track data with VLM event instances."""

    # ------------------------------------------------------------------
    # Load CV tracks
    # ------------------------------------------------------------------
    @staticmethod
    def load_cv_tracks(json_path: str) -> Dict[str, Track]:
        """Load vehicle tracks from a ``vehicles_merged.json`` file.

        The JSON is expected to be a flat mapping ``{vehicle_id: track_dict}``
        where each *track_dict* contains keys such as ``boxes``,
        ``road_id``, ``enter_frame``, ``exit_frame``,
        ``total_displacement``, ``lifetime_frames``, ``lifetime_sec``,
        and ``merged_from``.

        Missing or malformed fields are filled with sensible defaults so that
        the pipeline can continue gracefully.

        Args:
            json_path: Path to the JSON file produced by the CV pipeline.

        Returns:
            A mapping from track ID to validated :class:`Track` models.

        Raises:
            FileNotFoundError: If *json_path* does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        path = Path(json_path)
        if not path.exists():
            raise FileNotFoundError(f"CV track file not found: {json_path}")

        with path.open("r", encoding="utf-8") as fh:
            raw: Dict[str, Any] = json.load(fh)

        tracks: Dict[str, Track] = {}
        if not isinstance(raw, dict):
            logger.warning(
                "Expected root object to be a dict, got %s. Returning empty tracks.",
                type(raw).__name__,
            )
            return tracks

        for vid, data in raw.items():
            if not isinstance(data, dict):
                logger.warning("Skipping non-dict entry for vehicle %s", vid)
                continue

            # Normalise boxes – each box is [frame, x1, y1, w, h, cx, cy, area]
            boxes = data.get("boxes", [])
            if not isinstance(boxes, list):
                boxes = []

            # Basic frame range fallback
            enter_frame = data.get("enter_frame", 0)
            exit_frame = data.get("exit_frame", 0)
            if boxes and (not enter_frame or not exit_frame):
                try:
                    enter_frame = int(boxes[0][0])
                    exit_frame = int(boxes[-1][0])
                except Exception:  # pragma: no cover
                    pass

            # Normalise merged_from fragments
            merged_from = data.get("merged_from", [])
            if not isinstance(merged_from, list):
                merged_from = []

            # appearance_feature is optional and usually not present in JSON
            appearance_feature = data.get("appearance_feature")
            if appearance_feature is not None and not isinstance(
                appearance_feature, list
            ):
                appearance_feature = None

            try:
                track = Track(
                    track_id=str(vid),
                    road_id=data.get("road_id"),
                    boxes=boxes,
                    enter_frame=int(enter_frame),
                    exit_frame=int(exit_frame),
                    total_displacement=float(data.get("total_displacement", 0.0)),
                    lifetime_frames=int(data.get("lifetime_frames", len(boxes))),
                    lifetime_sec=float(data.get("lifetime_sec", 0.0)),
                    merged_from=merged_from,
                    appearance_feature=appearance_feature,
                )
                tracks[str(vid)] = track
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed to parse track %s: %s", vid, exc)
                continue

        logger.info("Loaded %d CV tracks from %s", len(tracks), json_path)
        return tracks

    # ------------------------------------------------------------------
    # Cross-validation
    # ------------------------------------------------------------------
    @staticmethod
    def cross_validate_direction(
        vlm_instances: List[EventInstance],
        tracks: Dict[str, Track],
        roads: List[RoadInfo],
        fps: float = 15.0,
    ) -> List[EventInstance]:
        """Cross-validate VLM reversing events against CV track directions.

        For each instance whose *event_name* suggests a direction anomaly
        (e.g. "reversing", "逆行", "wrong_way"), the adapter attempts to
        find a matching CV track.  When a track is found its dominant
        direction is compared with the road's ``normal_direction``.  If the
        track moves *opposite* to the normal direction the VLM confidence is
        boosted; if it moves *with* the normal direction the confidence is
        reduced and the instance is flagged for review.

        Args:
            vlm_instances: Event instances produced by the VLM pipeline.
            tracks: CV tracks keyed by track ID (from :meth:`load_cv_tracks`).
            roads: Scene road definitions used to obtain ``normal_direction``.
            fps: Video frame rate used when computing track direction.

        Returns:
            A new list of :class:`EventInstance` objects with adjusted
            confidence fields.  The original list is not modified.
        """
        road_map: Dict[int, RoadInfo] = {r.road_id: r for r in roads}
        validated: List[EventInstance] = []

        for inst in vlm_instances:
            # Only process direction-related events
            if not _is_direction_event(inst.event_name):
                validated.append(inst)
                continue

            matched = ExternalAdapter.find_track_for_instance(inst, tracks)
            if matched is None:
                # No CV evidence – keep original but downgrade slightly
                new_inst = inst.model_copy(deep=True)
                new_inst.confidence = max(
                    _MIN_CONFIDENCE, new_inst.confidence - 0.05
                )
                new_inst.confidence_level = _confidence_level(new_inst.confidence)
                new_inst.reasoning = (
                    new_inst.reasoning + " [CV: no matching track found]"
                ).strip()
                validated.append(new_inst)
                continue

            road = road_map.get(inst.road_id) if inst.road_id is not None else None
            if road is None or road.normal_direction == "unknown":
                # Cannot validate without road direction info
                validated.append(inst)
                continue

            track_dir = ExternalAdapter.compute_track_direction(matched, fps)

            # Determine whether the track agrees with VLM "reversing" claim
            # Reversing means moving *opposite* to normal_direction.
            if track_dir == "stationary":
                # Stationary vehicle cannot be reversing
                new_inst = inst.model_copy(deep=True)
                new_inst.confidence = max(
                    _MIN_CONFIDENCE, new_inst.confidence - _DISAGREEMENT_PENALTY
                )
                new_inst.confidence_level = _confidence_level(new_inst.confidence)
                new_inst.reasoning = (
                    new_inst.reasoning
                    + f" [CV: track {matched.track_id} is stationary]"
                ).strip()
                validated.append(new_inst)
                continue

            is_opposite = _is_opposite_direction(track_dir, road.normal_direction)

            new_inst = inst.model_copy(deep=True)
            if is_opposite:
                new_inst.confidence = min(
                    _MAX_CONFIDENCE, new_inst.confidence + _AGREEMENT_BOOST
                )
                new_inst.confidence_level = _confidence_level(new_inst.confidence)
                new_inst.reasoning = (
                    new_inst.reasoning
                    + (
                        f" [CV: track {matched.track_id} moving {track_dir} "
                        f"opposes normal {road.normal_direction}]"
                    )
                ).strip()
            else:
                new_inst.confidence = max(
                    _MIN_CONFIDENCE, new_inst.confidence - _DISAGREEMENT_PENALTY
                )
                new_inst.confidence_level = _confidence_level(new_inst.confidence)
                new_inst.reasoning = (
                    new_inst.reasoning
                    + (
                        f" [CV: track {matched.track_id} moving {track_dir} "
                        f"matches normal {road.normal_direction} — flag for review]"
                    )
                ).strip()

            validated.append(new_inst)

        return validated

    # ------------------------------------------------------------------
    # Track / instance matching
    # ------------------------------------------------------------------
    @staticmethod
    def find_track_for_instance(
        instance: EventInstance,
        tracks: Dict[str, Track],
        iou_threshold: float = _DEFAULT_IOU_THRESHOLD,
        time_overlap_ratio: float = _DEFAULT_TIME_OVERLAP_RATIO,
        proximity_px: float = _DEFAULT_PROXIMITY_PX,
    ) -> Optional[Track]:
        """Find the best-matching CV track for a VLM event instance.

        Matching criteria (all must be satisfied):

        1. **Road ID** – the track's ``road_id`` must equal the instance's
           ``road_id`` (unless the instance has no road assigned).
        2. **Temporal overlap** – the overlap between the instance's time
           window and the track's frame range must be at least
           *time_overlap_ratio* of the instance duration.
        3. **Spatial proximity** – the median centre of the track's boxes
           must be within *proximity_px* pixels of the instance's implicit
           location (derived from ``evidence_frames`` if available).

        When multiple tracks satisfy the criteria the one with the largest
        temporal overlap is returned.

        Args:
            instance: VLM-detected event instance.
            tracks: Available CV tracks.
            iou_threshold: Minimum IoU (reserved for future use).
            time_overlap_ratio: Minimum fraction of instance duration that
                must overlap with the track.
            proximity_px: Maximum centroid distance in pixels.

        Returns:
            The best matching :class:`Track`, or ``None`` if no track fits.
        """
        if not tracks:
            return None

        # Derive instance frame range from evidence_frames if present
        inst_frames = sorted(instance.evidence_frames)
        if inst_frames:
            inst_start = inst_frames[0]
            inst_end = inst_frames[-1]
        else:
            # Fallback: assume instance times map roughly to frames at 15 fps
            inst_start = int(instance.start_time_sec * 15.0)
            inst_end = int(instance.end_time_sec * 15.0)

        if inst_end < inst_start:
            inst_start, inst_end = inst_end, inst_start

        instance_duration = max(1, inst_end - inst_start)
        best_track: Optional[Track] = None
        best_overlap = 0.0

        for track in tracks.values():
            # 1. Road ID check
            if instance.road_id is not None and track.road_id is not None:
                if track.road_id != instance.road_id:
                    continue

            # 2. Temporal overlap
            track_start = track.enter_frame
            track_end = track.exit_frame
            overlap_start = max(inst_start, track_start)
            overlap_end = min(inst_end, track_end)
            overlap = max(0, overlap_end - overlap_start)
            if overlap / instance_duration < time_overlap_ratio:
                continue

            # 3. Spatial proximity – median centre of track boxes
            if not track.boxes:
                continue

            try:
                cx = float(track.boxes[len(track.boxes) // 2][5])
                cy = float(track.boxes[len(track.boxes) // 2][6])
            except (IndexError, ValueError, TypeError):
                continue

            # Instance location – use median evidence frame if available
            if inst_frames:
                median_frame = inst_frames[len(inst_frames) // 2]
                # Find the box closest to median_frame
                closest_box = min(
                    track.boxes,
                    key=lambda b: abs(float(b[0]) - median_frame),
                )
                try:
                    inst_cx = float(closest_box[5])
                    inst_cy = float(closest_box[6])
                except (IndexError, ValueError, TypeError):
                    inst_cx, inst_cy = cx, cy
            else:
                inst_cx, inst_cy = cx, cy

            dist = math.hypot(cx - inst_cx, cy - inst_cy)
            if dist > proximity_px:
                continue

            # Pick track with largest temporal overlap
            if overlap > best_overlap:
                best_overlap = overlap
                best_track = track

        return best_track

    # ------------------------------------------------------------------
    # Direction computation
    # ------------------------------------------------------------------
    @staticmethod
    def compute_track_direction(track: Track, fps: float) -> str:
        """Compute the dominant Y displacement direction of a track.

        The method analyses the sequence of bounding-box centres and decides
        whether the vehicle is primarily moving **up**, **down**, **left**,
        **right**, or is **stationary**.

        The vertical (Y) component is weighted more heavily because road
        cameras are typically mounted with the road running vertically in
        the image plane.

        Args:
            track: A CV track with at least two boxes.
            fps: Frame rate of the source video (used to normalise motion
                magnitude but not the direction itself).

        Returns:
            One of ``"up"``, ``"down"``, ``"left"``, ``"right"``,
            or ``"stationary"``.
        """
        boxes = track.boxes
        if len(boxes) < 2:
            return "stationary"

        # Extract centres
        centres: List[Tuple[float, float]] = []
        for box in boxes:
            try:
                cx = float(box[5])
                cy = float(box[6])
                centres.append((cx, cy))
            except (IndexError, ValueError, TypeError):
                continue

        if len(centres) < 2:
            return "stationary"

        # Compute total displacement using first and last valid centres
        dx = centres[-1][0] - centres[0][0]
        dy = centres[-1][1] - centres[0][1]

        # Also compute median step-wise displacement to reduce outlier influence
        dys = [centres[i + 1][1] - centres[i][1] for i in range(len(centres) - 1)]
        dxs = [centres[i + 1][0] - centres[i][0] for i in range(len(centres) - 1)]
        median_dy = float(sorted(dys)[len(dys) // 2]) if dys else 0.0
        median_dx = float(sorted(dxs)[len(dxs) // 2]) if dxs else 0.0

        # Combine total and median for robustness
        combined_dy = dy * 0.5 + median_dy * 0.5
        combined_dx = dx * 0.5 + median_dx * 0.5

        # Minimum movement threshold (pixels per second)
        # Use track lifetime to avoid tiny drifts being classified as motion
        lifetime_sec = track.lifetime_sec or (len(centres) / max(fps, 1.0))
        min_movement_px = max(2.0, 5.0 * lifetime_sec)  # 5 px/sec threshold

        total_movement = math.hypot(combined_dx, combined_dy)
        if total_movement < min_movement_px:
            return "stationary"

        # Prefer vertical direction when ambiguous (typical road camera geometry)
        if abs(combined_dy) >= abs(combined_dx) * 0.8:
            return "down" if combined_dy > 0 else "up"
        else:
            return "right" if combined_dx > 0 else "left"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_direction_event(event_name: str) -> bool:
    """Return ``True`` if *event_name* indicates a direction anomaly."""
    keywords = ("reverse", "revers", "逆行", "wrong_way", "wrongway", "backward")
    lowered = event_name.lower()
    return any(k in lowered for k in keywords)


def _is_opposite_direction(track_dir: str, normal_dir: str) -> bool:
    """Return ``True`` if *track_dir* is opposite to *normal_dir*."""
    opposites = {
        ("up", "down"),
        ("down", "up"),
        ("left", "right"),
        ("right", "left"),
    }
    return (track_dir, normal_dir) in opposites


def _confidence_level(confidence: float) -> ConfidenceLevel:
    """Map a raw confidence score to a :class:`ConfidenceLevel`."""
    if confidence >= 0.8:
        return ConfidenceLevel.HIGH
    if confidence >= 0.5:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW
