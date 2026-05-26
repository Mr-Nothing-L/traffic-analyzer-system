"""YOLOv8 vehicle tracker for reversing detection."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from traffic_analyzer.core.tracking_models import (
    TrackedVehicle,
    TrackingEvidence,
    VehicleTrajectory,
)
from traffic_analyzer.models.schemas import AnalysisContext, SceneInfo

logger = logging.getLogger(__name__)


class YOLOVehicleTracker:
    """YOLOv8-based vehicle detector and tracker for highway emergency lane analysis."""

    # Configurable defaults
    DEFAULT_ROI_WIDTH_FRAC = 0.15
    MIN_TRACK_LENGTH = 3
    MIN_DISPLACEMENT_NORM = 0.01
    DEFAULT_TARGET_TRACKING_FPS = 5.0
    CONFIDENCE_THRESHOLD = 0.3
    TRACK_BUFFER = 30  # max missed frames before deleting a track
    IOU_THRESHOLD = 0.3

    # COCO vehicle class IDs
    YOLO_VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

    def __init__(
        self,
        model_path: str,
        target_fps: float = 5.0,
        device: str = "cpu",
        confidence_threshold: float = 0.3,
    ) -> None:
        self.model_path = model_path
        self.target_fps = target_fps
        self.device = device
        self.confidence_threshold = confidence_threshold
        self._model = None
        self._track_states: Dict[int, Dict] = {}  # track_id -> {bbox, cls, age, missed}
        self._next_track_id = 1
        self._last_frame_id = -1

    def _load_model(self) -> None:
        """Lazy-load YOLO model from self.model_path."""
        from ultralytics import YOLO

        if not Path(self.model_path).exists():
            raise RuntimeError(f"Model not found: {self.model_path}")
        self._model = YOLO(self.model_path)
        self._model.to(self.device)

    def detect(self, context: AnalysisContext) -> TrackingEvidence:
        """Main entry point. Open video, run detection+tracking, build evidence."""
        try:
            return self._detect_internal(context)
        except Exception as exc:
            logger.error("YOLOVehicleTracker error: %s", exc, exc_info=True)
            return TrackingEvidence(error_message=str(exc))

    def _detect_internal(self, context: AnalysisContext) -> TrackingEvidence:
        # 1. Validate
        if not context.video_meta:
            return TrackingEvidence(error_message="No video metadata in context")

        video_path = context.video_meta.file_path
        if not Path(video_path).exists():
            return TrackingEvidence(error_message=f"Video file not found: {video_path}")

        # 2. Load model (lazy)
        if self._model is None:
            self._load_model()

        # 3. Open video
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return TrackingEvidence(error_message=f"Cannot open video: {video_path}")

        original_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # 4. Determine ROI and normal_direction
        roi = self._determine_roi(frame_width, frame_height, context.scene_understanding)
        normal_direction = self._get_normal_direction(context.scene_understanding)

        # 5. Subsample frames
        interval = max(1, round(original_fps / self.target_fps))

        # 6. Run detection + tracking per sampled frame
        # track_id -> list of (frame_id, timestamp_sec, bbox, cls)
        # bbox = (x1, y1, x2, y2) in normalized coords
        raw_tracks: Dict[int, List[Tuple[int, float, np.ndarray, int]]] = {}

        frame_id = 0
        frames_processed = 0
        first_frame_id = -1
        last_frame_id = -1

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_id % interval != 0:
                frame_id += 1
                continue

            timestamp_sec = frame_id / original_fps if original_fps > 0 else 0.0

            # YOLO predict
            results = self._model.predict(
                frame,
                conf=self.confidence_threshold,
                classes=list(self.YOLO_VEHICLE_CLASSES.keys()),
                verbose=False,
                device=self.device,
            )

            detections = []
            if results and len(results) > 0:
                boxes = results[0].boxes
                if boxes is not None:
                    for i in range(len(boxes)):
                        xyxy = boxes.xyxy[i].cpu().numpy()
                        cls_id = int(boxes.cls[i].cpu().item())
                        conf = float(boxes.conf[i].cpu().item())
                        detections.append(
                            {
                                "bbox": xyxy,
                                "cls": cls_id,
                                "conf": conf,
                            }
                        )

            # Run tracking
            active_tracks = self._run_tracking(detections, frame_id)

            # Store track data
            for track_id, track_data in active_tracks.items():
                bbox = track_data["bbox"]  # pixel coords
                cls = track_data["cls"]
                # Normalize bbox
                x1, y1, x2, y2 = bbox
                norm_bbox = np.array([
                    x1 / frame_width,
                    y1 / frame_height,
                    x2 / frame_width,
                    y2 / frame_height,
                ])
                if track_id not in raw_tracks:
                    raw_tracks[track_id] = []
                raw_tracks[track_id].append((frame_id, timestamp_sec, norm_bbox, cls))

            if first_frame_id < 0:
                first_frame_id = frame_id
            last_frame_id = frame_id
            frames_processed += 1
            frame_id += 1

        cap.release()

        # 7. Build VehicleTrajectory objects
        vehicles = self._build_vehicles(raw_tracks, frame_width, frame_height)

        # 8. Filter to emergency lane ROI
        roi_vehicles = []
        for v in vehicles:
            if v.trajectory.is_in_emergency_lane:
                roi_vehicles.append(v)

        # 9. Classify direction per vehicle
        for v in roi_vehicles:
            self._classify_vehicle_direction(v, normal_direction)

        # 10. Build evidence
        reversing_vehicles = [
            v for v in roi_vehicles if v.direction_classification == "reversing"
        ]

        time_range = (0.0, 0.0)
        if frames_processed > 0 and original_fps > 0:
            time_range = (
                first_frame_id / original_fps,
                last_frame_id / original_fps,
            )

        evidence = TrackingEvidence(
            detected=len(reversing_vehicles) > 0,
            vehicles=roi_vehicles,
            reversing_vehicles=reversing_vehicles,
            normal_direction=normal_direction,
            roi_bounds=roi,
            frame_range=(first_frame_id, last_frame_id),
            time_range_sec=time_range,
            frames_processed=frames_processed,
        )
        evidence.evidence_text = evidence.to_prompt_text()
        return evidence

    def _run_tracking(
        self, detections: List[Dict], frame_id: int
    ) -> Dict[int, Dict]:
        """Multi-object tracking using Hungarian matching with IOU.

        Returns: {track_id: {"bbox": np.array, "cls": int}}
        """
        from scipy.optimize import linear_sum_assignment

        # Reset if frame goes backward (new video)
        if frame_id < self._last_frame_id:
            self._track_states.clear()
            self._next_track_id = 1

        self._last_frame_id = frame_id

        # Build cost matrix: IOU between existing tracks and new detections
        track_ids = list(self._track_states.keys())
        n_tracks = len(track_ids)
        n_dets = len(detections)

        if n_tracks == 0:
            # All detections become new tracks
            for det in detections:
                self._track_states[self._next_track_id] = {
                    "bbox": det["bbox"],
                    "cls": det["cls"],
                    "age": 1,
                    "missed": 0,
                }
                self._next_track_id += 1
            return {
                tid: {"bbox": s["bbox"], "cls": s["cls"]}
                for tid, s in self._track_states.items()
            }

        if n_dets == 0:
            # All tracks missed
            stale = []
            for tid in track_ids:
                self._track_states[tid]["missed"] += 1
                if self._track_states[tid]["missed"] > self.TRACK_BUFFER:
                    stale.append(tid)
            for tid in stale:
                del self._track_states[tid]
            return {
                tid: {"bbox": s["bbox"], "cls": s["cls"]}
                for tid, s in self._track_states.items()
            }

        # Compute IOU matrix
        cost_matrix = np.zeros((n_tracks, n_dets))
        for i, tid in enumerate(track_ids):
            track_bbox = self._track_states[tid]["bbox"]
            for j, det in enumerate(detections):
                cost_matrix[i, j] = -self._compute_iou(track_bbox, det["bbox"])

        # Hungarian matching
        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        matched_tracks = set()
        matched_dets = set()

        for i, j in zip(row_ind, col_ind):
            iou = -cost_matrix[i, j]
            if iou >= self.IOU_THRESHOLD:
                tid = track_ids[i]
                self._track_states[tid]["bbox"] = detections[j]["bbox"]
                self._track_states[tid]["cls"] = detections[j]["cls"]
                self._track_states[tid]["age"] += 1
                self._track_states[tid]["missed"] = 0
                matched_tracks.add(tid)
                matched_dets.add(j)

        # Unmatched detections -> new tracks
        for j, det in enumerate(detections):
            if j not in matched_dets:
                self._track_states[self._next_track_id] = {
                    "bbox": det["bbox"],
                    "cls": det["cls"],
                    "age": 1,
                    "missed": 0,
                }
                self._next_track_id += 1

        # Unmatched tracks -> increment missed, delete stale
        stale = []
        for tid in track_ids:
            if tid not in matched_tracks:
                self._track_states[tid]["missed"] += 1
                if self._track_states[tid]["missed"] > self.TRACK_BUFFER:
                    stale.append(tid)
        for tid in stale:
            del self._track_states[tid]

        return {
            tid: {"bbox": s["bbox"], "cls": s["cls"]}
            for tid, s in self._track_states.items()
        }

    def _build_vehicles(
        self,
        raw_tracks: Dict[int, List[Tuple[int, float, np.ndarray, int]]],
        frame_width: int,
        frame_height: int,
    ) -> List[TrackedVehicle]:
        """Convert raw track data to TrackedVehicle objects."""
        vehicles = []

        for track_id, track_data in raw_tracks.items():
            if len(track_data) < self.MIN_TRACK_LENGTH:
                continue

            # Vote on vehicle class
            cls_counts: Dict[int, int] = {}
            for _, _, _, cls_id in track_data:
                cls_counts[cls_id] = cls_counts.get(cls_id, 0) + 1
            most_common_cls = max(cls_counts, key=cls_counts.get)
            vehicle_type = self.YOLO_VEHICLE_CLASSES.get(most_common_cls, "unknown")

            # Build trajectory
            positions = []
            bboxes = []
            for frame_id, timestamp_sec, bbox, _ in track_data:
                # bbox is normalized (x1, y1, x2, y2)
                cx = (bbox[0] + bbox[2]) / 2.0
                cy = (bbox[1] + bbox[3]) / 2.0
                positions.append((timestamp_sec, cx, cy))
                bboxes.append((timestamp_sec, bbox[0], bbox[1], bbox[2], bbox[3]))

            trajectory = VehicleTrajectory(
                track_id=track_id,
                positions=positions,
                bboxes=bboxes,
            )

            # Compute avg speed
            avg_speed = self._compute_avg_speed(positions)

            vehicle = TrackedVehicle(
                track_id=track_id,
                trajectory=trajectory,
                vehicle_type=vehicle_type,
                avg_speed=avg_speed,
            )
            vehicles.append(vehicle)

        return vehicles

    def _classify_vehicle_direction(
        self, vehicle: TrackedVehicle, normal_direction: str
    ) -> None:
        """Classify direction based on step-by-step y displacement majority vote."""
        positions = vehicle.trajectory.positions
        if len(positions) < 2:
            vehicle.direction_classification = "uncertain"
            vehicle.direction_confidence = 0.0
            vehicle.summary = f"track_{vehicle.track_id}: {vehicle.vehicle_type}, uncertain (insufficient positions)"
            return

        # Count up/down votes
        up_votes = 0
        down_votes = 0
        significant_steps = 0
        net_dy = 0.0

        for i in range(1, len(positions)):
            dy = positions[i][2] - positions[i - 1][2]
            net_dy += dy
            if abs(dy) >= 0.001:
                significant_steps += 1
                if dy > 0:
                    down_votes += 1
                else:
                    up_votes += 1

        if significant_steps == 0:
            vehicle.direction_classification = "stationary"
            vehicle.direction_confidence = 0.5
            vehicle.summary = (
                f"track_{vehicle.track_id}: {vehicle.vehicle_type}, stationary, "
                f"dy={net_dy:.4f}, speed={vehicle.avg_speed:.4f}, consistency=0.00"
            )
            return

        # Majority vote
        if down_votes > up_votes:
            dominant = "down"
            consistency = down_votes / significant_steps
        elif up_votes > down_votes:
            dominant = "up"
            consistency = up_votes / significant_steps
        else:
            vehicle.direction_classification = "uncertain"
            vehicle.direction_confidence = 0.3
            vehicle.summary = (
                f"track_{vehicle.track_id}: {vehicle.vehicle_type}, uncertain (tie), "
                f"dy={net_dy:.4f}, speed={vehicle.avg_speed:.4f}, consistency=0.50"
            )
            return

        # Map to classification based on normal_direction
        if normal_direction == "toward_top":
            # up is normal
            classification = "reversing" if dominant == "down" else "normal"
        elif normal_direction == "toward_bottom":
            # down is normal
            classification = "reversing" if dominant == "up" else "normal"
        else:
            vehicle.direction_classification = "uncertain"
            vehicle.direction_confidence = 0.3
            vehicle.summary = (
                f"track_{vehicle.track_id}: {vehicle.vehicle_type}, uncertain (unknown normal), "
                f"dy={net_dy:.4f}, speed={vehicle.avg_speed:.4f}, consistency={consistency:.2f}"
            )
            return

        # Confidence
        displacement_score = min(abs(net_dy) / self.MIN_DISPLACEMENT_NORM, 1.0)
        confidence = consistency * 0.5 + displacement_score * 0.5

        vehicle.direction_classification = classification
        vehicle.direction_confidence = confidence
        vehicle.summary = (
            f"track_{vehicle.track_id}: {vehicle.vehicle_type}, {classification}, "
            f"dy={net_dy:.4f}, speed={vehicle.avg_speed:.4f}, consistency={consistency:.2f}"
        )

    def _compute_iou(self, box1: np.ndarray, box2: np.ndarray) -> float:
        """IOU for xyxy boxes."""
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])

        inter_w = max(0, x2 - x1)
        inter_h = max(0, y2 - y1)
        inter_area = inter_w * inter_h

        area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        union_area = area1 + area2 - inter_area

        if union_area <= 0:
            return 0.0
        return inter_area / union_area

    def _compute_avg_speed(
        self, positions: List[Tuple[float, float, float]]
    ) -> float:
        """Average speed in normalized units per second."""
        if len(positions) < 2:
            return 0.0

        total_dist = 0.0
        total_time = 0.0
        for i in range(1, len(positions)):
            dt = positions[i][0] - positions[i - 1][0]
            if dt <= 0:
                continue
            dx = positions[i][1] - positions[i - 1][1]
            dy = positions[i][2] - positions[i - 1][2]
            total_dist += (dx * dx + dy * dy) ** 0.5
            total_time += dt

        if total_time <= 0:
            return 0.0
        return total_dist / total_time

    def _determine_roi(
        self,
        frame_width: int,
        frame_height: int,
        scene_understanding: Optional[SceneInfo],
    ) -> Tuple[int, int, int, int]:
        """Return (x, y, w, h) in pixels."""
        roi_width = int(frame_width * self.DEFAULT_ROI_WIDTH_FRAC)

        if scene_understanding and scene_understanding.roads:
            for road in scene_understanding.roads:
                if road.has_emergency_lane and road.emergency_lane_side:
                    side = road.emergency_lane_side
                    if side == "left":
                        return (0, 0, roi_width, frame_height)
                    elif side == "right":
                        return (frame_width - roi_width, 0, roi_width, frame_height)
                    elif side == "both":
                        return (0, 0, roi_width, frame_height)

        # Fallback: left side
        return (0, 0, roi_width, frame_height)

    def _get_normal_direction(self, scene_understanding: Optional[SceneInfo]) -> str:
        """Extract from scene_understanding or default to toward_bottom."""
        if scene_understanding and scene_understanding.roads:
            for road in scene_understanding.roads:
                if road.normal_direction and road.normal_direction != "unknown":
                    return road.normal_direction
        return "toward_bottom"
