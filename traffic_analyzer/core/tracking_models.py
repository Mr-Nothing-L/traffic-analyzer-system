"""Data models for vehicle tracking evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class VehicleTrajectory:
    """Position history of a single tracked vehicle."""

    track_id: int
    # List of (timestamp_sec, x, y) where x,y are normalized [0,1]
    positions: List[Tuple[float, float, float]] = field(default_factory=list)
    # Bounding box history: (timestamp_sec, x1, y1, x2, y2) in normalized coords
    bboxes: List[Tuple[float, float, float, float, float]] = field(default_factory=list)

    @property
    def start_time_sec(self) -> float:
        if not self.positions:
            return 0.0
        return self.positions[0][0]

    @property
    def end_time_sec(self) -> float:
        if not self.positions:
            return 0.0
        return self.positions[-1][0]

    @property
    def duration_sec(self) -> float:
        return self.end_time_sec - self.start_time_sec

    @property
    def is_in_emergency_lane(self) -> bool:
        """Heuristic: vehicle centroid consistently in left 15% or right 15% of frame."""
        if not self.positions:
            return False
        edge_count = 0
        for _, x, _ in self.positions:
            if x < 0.15 or x > 0.85:
                edge_count += 1
        return edge_count / len(self.positions) > 0.5


@dataclass
class TrackedVehicle:
    """A single tracked vehicle with trajectory and direction classification."""

    track_id: int
    trajectory: VehicleTrajectory
    direction_classification: str = "uncertain"  # "normal", "reversing", "stationary", "uncertain"
    direction_confidence: float = 0.0  # [0, 1]
    avg_speed: float = 0.0  # normalized units per second
    vehicle_type: str = "unknown"  # "car", "truck", "bus", "motorcycle", "unknown"
    summary: str = ""  # Description for VLM prompt


@dataclass
class TrackingEvidence:
    """Complete tracking result for a video segment."""

    detected: bool = False
    vehicles: List[TrackedVehicle] = field(default_factory=list)
    reversing_vehicles: List[TrackedVehicle] = field(default_factory=list)
    evidence_text: str = ""  # Human-readable summary for prompt injection
    normal_direction: str = "unknown"
    roi_bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h in pixels
    frame_range: Tuple[int, int] = (0, 0)
    time_range_sec: Tuple[float, float] = (0.0, 0.0)
    frames_processed: int = 0
    error_message: str = ""

    def to_prompt_text(self) -> str:
        """Format tracking evidence as structured Chinese text for VLM prompt."""
        lines: List[str] = []
        lines.append("## 跟踪分析结果")
        lines.append(f"- 分析帧范围: {self.frame_range[0]} ~ {self.frame_range[1]}")
        lines.append(f"- 应急车道ROI坐标: ({self.roi_bounds[0]}, {self.roi_bounds[1]}, {self.roi_bounds[2]}, {self.roi_bounds[3]})")
        lines.append(f"- 正常流向: {self.normal_direction}")
        lines.append(f"- 跟踪到车辆总数: {len(self.vehicles)}")
        lines.append("")

        if self.vehicles:
            lines.append("### 各车辆详情")
            for v in self.vehicles:
                traj = v.trajectory
                if traj.positions:
                    x0, y0 = traj.positions[0][1], traj.positions[0][2]
                    x1, y1 = traj.positions[-1][1], traj.positions[-1][2]
                    pos_change = f"({x0:.3f},{y0:.3f}) -> ({x1:.3f},{y1:.3f})"
                else:
                    pos_change = "无位置数据"
                lines.append(
                    f"  - ID={v.track_id}, 类型={v.vehicle_type}, "
                    f"方向判定={v.direction_classification}, 置信度={v.direction_confidence:.2f}, "
                    f"平均速度={v.avg_speed:.4f}, 位置变化={pos_change}, 持续时间={traj.duration_sec:.2f}s"
                )
            lines.append("")

        if self.reversing_vehicles:
            lines.append("### 逆行/倒车嫌疑车辆")
            for v in self.reversing_vehicles:
                lines.append(
                    f"  - ID={v.track_id}, 类型={v.vehicle_type}, "
                    f"置信度={v.direction_confidence:.2f}, 判定={v.direction_classification}"
                )
            lines.append("")

        if self.error_message:
            lines.append(f"### 错误信息\n  {self.error_message}")

        return "\n".join(lines)
