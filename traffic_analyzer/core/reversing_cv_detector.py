"""Lightweight CV detector for reversing vehicles on highway emergency lanes.

Uses pure OpenCV operations (no deep learning) on existing coarse frames.
Only activates for event_id=7 (Vehicle Reversing).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

from traffic_analyzer.models.schemas import AnalysisContext, Keyframe, SceneInfo

logger = logging.getLogger(__name__)


@dataclass
class CVDetectionResult:
    """Result from the reversing CV detector."""

    detected: bool = False
    confidence: float = 0.0
    summary: str = ""
    displacement_pixels: float = 0.0
    direction: str = "unknown"  # "normal", "reversing", "unknown"
    roi_bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h
    evidence: str = ""


class ReversingCVDetector:
    """Lightweight CV supplement for reversing vehicle detection.

    Uses frame differencing and centroid analysis on coarse keyframes.
    Only processes event_id=7. Does NOT replace VLM - provides
    supplementary motion evidence to boost or reduce confidence.
    """

    # Minimum contour area as fraction of frame area
    MIN_CONTOUR_AREA_FRAC: float = 0.001
    # Threshold for significant pixel difference
    DIFF_THRESHOLD: int = 30
    # Minimum displacement (pixels) to consider as motion
    MIN_DISPLACEMENT_PX: int = 3
    # Maximum confidence CV can assign (it supplements, not replaces VLM)
    MAX_CV_CONFIDENCE: float = 0.4
    # Default emergency lane width as fraction of frame width
    DEFAULT_ROI_WIDTH_FRAC: float = 0.20

    def __init__(
        self,
        diff_threshold: int = 30,
        min_displacement_px: int = 3,
        max_confidence: float = 0.4,
    ) -> None:
        self.diff_threshold = diff_threshold
        self.min_displacement_px = min_displacement_px
        self.max_confidence = max_confidence

    def detect(self, context: AnalysisContext) -> CVDetectionResult:
        """Run CV detection for reversing vehicles.

        Args:
            context: Analysis context with keyframes and scene understanding.

        Returns:
            CVDetectionResult with motion evidence.
        """
        try:
            return self._detect_internal(context)
        except Exception as exc:
            logger.error("ReversingCVDetector error: %s", exc, exc_info=True)
            return CVDetectionResult(
                summary=f"CV analysis error: {exc}",
                evidence="CV detector encountered an error and could not complete analysis",
            )

    def _detect_internal(self, context: AnalysisContext) -> CVDetectionResult:
        """Internal detection logic (may raise)."""
        # -- 1. Validate inputs ------------------------------------------------
        if not context.keyframes or not context.keyframes.coarse_frames:
            logger.debug("ReversingCVDetector: no coarse frames available")
            return CVDetectionResult(summary="No frames available for CV analysis")

        coarse = context.keyframes.coarse_frames
        if len(coarse) < 2:
            logger.debug("ReversingCVDetector: need at least 2 frames, got %d", len(coarse))
            return CVDetectionResult(summary="Insufficient frames for motion analysis")

        # -- 2. Decode first and last frames -----------------------------------
        first_frame = self._decode_frame(coarse[0])
        last_frame = self._decode_frame(coarse[-1])

        if first_frame is None or last_frame is None:
            logger.warning("ReversingCVDetector: failed to decode frames")
            return CVDetectionResult(summary="Frame decode failed")

        if first_frame.shape != last_frame.shape:
            logger.warning("ReversingCVDetector: frame shape mismatch")
            return CVDetectionResult(summary="Frame shape mismatch")

        # -- 3. Determine ROI --------------------------------------------------
        h, w = first_frame.shape[:2]
        roi = self._determine_roi(w, h, context.scene_understanding)

        # -- 4. Extract ROI and compute difference -----------------------------
        roi_first = first_frame[roi[1] : roi[1] + roi[3], roi[0] : roi[0] + roi[2]]
        roi_last = last_frame[roi[1] : roi[1] + roi[3], roi[0] : roi[0] + roi[2]]

        diff = cv2.absdiff(roi_first, roi_last)
        gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray_diff, self.diff_threshold, 255, cv2.THRESH_BINARY)

        # -- 5. Find connected components --------------------------------------
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
            thresh, connectivity=8
        )

        min_area = int(w * h * self.MIN_CONTOUR_AREA_FRAC)
        significant_components: List[Tuple[int, np.ndarray, float]] = []

        for i in range(1, num_labels):  # skip background label 0
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= min_area:
                cx, cy = centroids[i]
                significant_components.append((i, np.array([cx, cy]), float(area)))

        if not significant_components:
            logger.debug("ReversingCVDetector: no significant motion detected in ROI")
            return CVDetectionResult(
                roi_bounds=roi,
                summary="No significant motion detected in emergency lane ROI",
            )

        # -- 6. Analyze direction ----------------------------------------------
        normal_direction = self._get_normal_direction(context.scene_understanding)
        time_span_sec = coarse[-1].timestamp_sec - coarse[0].timestamp_sec

        best_evidence: Optional[Tuple[float, str, float]] = None

        for label_id, centroid, area in significant_components:
            # Create mask for this component
            component_mask = (labels == label_id).astype(np.uint8) * 255

            displacement_px = self._estimate_displacement(
                roi_first, roi_last, component_mask
            )

            if abs(displacement_px) < self.min_displacement_px:
                continue

            direction = self._classify_direction(displacement_px, normal_direction)
            confidence = min(
                abs(displacement_px) / (self.min_displacement_px * 5),
                1.0,
            ) * self.max_confidence

            evidence = (
                f"Component {label_id}: area={area:.0f}px, "
                f"displacement={displacement_px:.1f}px over {time_span_sec:.1f}s, "
                f"direction={direction}"
            )

            if best_evidence is None or confidence > best_evidence[0]:
                best_evidence = (confidence, direction, displacement_px)

            logger.debug("ReversingCVDetector: %s", evidence)

        # -- 7. Build result ---------------------------------------------------
        if best_evidence is None:
            return CVDetectionResult(
                roi_bounds=roi,
                summary="Motion detected but displacement below threshold",
            )

        confidence, direction, displacement = best_evidence
        detected = direction == "reversing"

        summary = (
            f"CV analysis: {direction} motion detected in emergency lane ROI "
            f"(displacement={displacement:.1f}px over {time_span_sec:.1f}s, "
            f"confidence={confidence:.2f})"
        )

        return CVDetectionResult(
            detected=detected,
            confidence=confidence,
            summary=summary,
            displacement_pixels=displacement,
            direction=direction,
            roi_bounds=roi,
            evidence=summary,
        )

    def _decode_frame(self, keyframe: Keyframe) -> Optional[np.ndarray]:
        """Decode a Keyframe to an OpenCV BGR image."""
        if keyframe.image_data is not None:
            arr = np.frombuffer(keyframe.image_data, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        elif keyframe.image_path is not None:
            return cv2.imread(keyframe.image_path)
        return None

    def _determine_roi(
        self,
        frame_width: int,
        frame_height: int,
        scene_understanding: Optional[SceneInfo],
    ) -> Tuple[int, int, int, int]:
        """Determine emergency lane ROI from scene understanding.

        Returns (x, y, w, h) in pixel coordinates.
        """
        roi_width = int(frame_width * self.DEFAULT_ROI_WIDTH_FRAC)

        if scene_understanding and scene_understanding.roads:
            for road in scene_understanding.roads:
                if road.has_emergency_lane and road.emergency_lane_side:
                    if road.emergency_lane_side == "left":
                        return (0, 0, roi_width, frame_height)
                    elif road.emergency_lane_side == "right":
                        return (frame_width - roi_width, 0, roi_width, frame_height)
                    elif road.emergency_lane_side == "both":
                        # Use left side as primary, but could analyze both
                        return (0, 0, roi_width, frame_height)

        # Fallback: use left 20% (most common for Chinese highways)
        logger.debug("ReversingCVDetector: no emergency lane info, using left-side fallback ROI")
        return (0, 0, roi_width, frame_height)

    def _get_normal_direction(self, scene_understanding: Optional[SceneInfo]) -> str:
        """Extract normal flow direction from scene understanding."""
        if scene_understanding and scene_understanding.roads:
            # Use the first road with a defined direction
            for road in scene_understanding.roads:
                if road.normal_direction and road.normal_direction != "unknown":
                    return road.normal_direction
        return "unknown"

    def _estimate_displacement(
        self,
        roi_first: np.ndarray,
        roi_last: np.ndarray,
        component_mask: np.ndarray,
    ) -> float:
        """Estimate vertical displacement of a moving object.

        The component_mask comes from the thresholded difference image,
        marking WHERE pixel change occurred between first and last frames.

        Strategy:
        1. Try Otsu thresholding on each frame masked to the component region.
           If both frames yield contours, return cy_last - cy_first.
        2. If only one frame yields a contour, the component represents either
           the "hole" (where object was) or the "new location" (where it is).
           Use the diff-component centroid as the missing endpoint.
        3. If neither yields a contour, fall back to the diff centroid.

        Returns:
            Vertical displacement in pixels (positive = downward).
        """
        # Compute per-pixel difference magnitude in the component region
        diff = cv2.absdiff(roi_first, roi_last)
        gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

        # Weighted centroid of the difference region (component_mask)
        weights = gray_diff.astype(np.float32) * (component_mask > 0)
        total_weight = weights.sum()
        cy_component: Optional[float] = None
        if total_weight > 1e-6:
            h = roi_first.shape[0]
            y_indices = np.arange(h, dtype=np.float32)
            cy_component = float(np.sum(y_indices * weights.sum(axis=1)) / total_weight)

        # Otsu threshold each frame within the component region
        _, thresh_first = cv2.threshold(
            cv2.cvtColor(roi_first, cv2.COLOR_BGR2GRAY),
            0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        _, thresh_last = cv2.threshold(
            cv2.cvtColor(roi_last, cv2.COLOR_BGR2GRAY),
            0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        thresh_first = cv2.bitwise_and(thresh_first, component_mask)
        thresh_last = cv2.bitwise_and(thresh_last, component_mask)

        cy_first = self._contour_centroid_y(thresh_first)
        cy_last = self._contour_centroid_y(thresh_last)

        if cy_first is not None and cy_last is not None:
            # Both frames have a detectable object in this component region
            return cy_last - cy_first

        # Partial detection: one frame has the object, the other doesn't.
        # The component_mask centroid tells us the position of the missing side.
        if cy_component is None:
            return 0.0

        if cy_first is not None and cy_last is None:
            # Object was here in first frame, gone in last.
            # Component centroid is near the NEW location (or the hole).
            # We need to determine which: compare cy_component to cy_first.
            # If cy_component > cy_first, the change is below the object
            #   -> object moved DOWN (positive displacement)
            # If cy_component < cy_first, the change is above the object
            #   -> object moved UP (negative displacement)
            return cy_component - cy_first

        if cy_first is None and cy_last is not None:
            # Object not here in first frame, appeared in last.
            # Component centroid is near the OLD location (the hole).
            # If cy_component > cy_last, the hole is below the new position
            #   -> object moved UP (negative displacement)
            # If cy_component < cy_last, the hole is above the new position
            #   -> object moved DOWN (positive displacement)
            return cy_last - cy_component

        # Neither frame has a contour in this component — no measurable displacement
        return 0.0

    def _contour_centroid_y(self, binary_mask: np.ndarray) -> Optional[float]:
        """Compute the y-coordinate of the centroid of the largest contour."""
        contours, _ = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # Use largest contour
        largest = max(contours, key=cv2.contourArea)
        M = cv2.moments(largest)
        if M["m00"] == 0:
            return None
        return M["m01"] / M["m00"]

    def _classify_direction(self, displacement_px: float, normal_direction: str) -> str:
        """Classify motion as normal or reversing based on displacement and normal flow.

        Args:
            displacement_px: Vertical displacement (positive = downward).
            normal_direction: Expected flow direction (toward_top or toward_bottom).

        Returns:
            "normal", "reversing", or "unknown".
        """
        if normal_direction == "unknown":
            return "unknown"

        # toward_top = moving upward (away from camera) = negative y displacement
        # toward_bottom = moving downward (toward camera) = positive y displacement
        if normal_direction == "toward_top":
            # Normal: upward (negative displacement)
            # Reversing: downward (positive displacement)
            return "reversing" if displacement_px > 0 else "normal"
        elif normal_direction == "toward_bottom":
            # Normal: downward (positive displacement)
            # Reversing: upward (negative displacement)
            return "reversing" if displacement_px < 0 else "normal"

        return "unknown"
