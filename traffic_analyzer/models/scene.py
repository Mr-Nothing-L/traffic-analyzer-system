"""
Scene understanding models for the traffic analyzer framework.

[文件说明]
作用:定义场景理解模型 SceneInfo(道路/天气/车流密度及行人、非机动车、抛洒物存在标志)及其多步方向分析子模型(RoadInfo、DirectionAnalysis、VehicleMotion、HeadOrientation、ConsistencyCheck、PerspectiveCheck、DirectionConclusion、DirectionEvidence)。
上游:models/schemas.py、models/context.py、models/report.py 引用;由 core/pipeline_steps.py 场景理解步骤填充,被 orchestrator/analysis_orchestrator.py、core/report_generator.py 消费。
下游:pydantic。
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field


class DirectionEvidence(BaseModel):
    """Evidence for direction determination of a single vehicle.

    Only movement (position change across frames) is recorded.
    Head orientation is intentionally omitted because VLM often misidentifies
    front vs rear of vehicles, especially trucks and distant cars.
    """
    vehicle: str = ""
    movement: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "stationary", "unknown"] = "unknown"
    location_earlier: str = ""
    location_later: str = ""
    frames_compared: str = ""  # e.g. "Frame 1 → Frame 2"


# ---------------------------------------------------------------------------
# Direction Analysis (multi-step VLM-based direction determination)
# ---------------------------------------------------------------------------

class VehicleMotion(BaseModel):
    """Motion vector for a single vehicle across frames."""
    vehicle_id: str = ""
    description: str = ""  # e.g. "白色轿车"
    displacement: str = ""  # e.g. "grid cell (row 2, col 3) → grid cell (row 1, col 3)"
    movement_direction: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "stationary", "unknown"] = "unknown"
    road_id: int = 0


class HeadOrientation(BaseModel):
    """Head orientation (front-facing direction) of a single vehicle."""
    vehicle_id: str = ""
    head_orientation: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "unknown"] = "unknown"
    evidence: str = ""  # e.g. "headlights visible, facing upward"


class ConsistencyCheck(BaseModel):
    """Consistency check between movement direction and head orientation."""
    vehicle_id: str = ""
    movement: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "stationary", "unknown"] = "unknown"
    head_orientation: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "unknown"] = "unknown"
    consistent: bool = True
    anomaly: bool = False  # True if head opposes movement (reversing)


class PerspectiveCheck(BaseModel):
    """Perspective consistency check for a single vehicle."""
    vehicle_id: str = ""
    size_change: str = ""  # e.g. "getting larger", "getting smaller", "no change"
    matches_direction: bool = True  # Does size change match movement direction?
    trajectory_parallel_to_lanes: bool = True


class DirectionConclusion(BaseModel):
    """Final direction conclusion for a single road."""
    road_id: int = 0
    name: str = ""  # e.g. "左侧道路"
    normal_direction: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "unknown"] = "unknown"
    confidence: float = 0.0
    evidence_summary: str = ""  # e.g. "5/5 vehicles moving upward..."


class DirectionAnalysis(BaseModel):
    """Complete multi-step direction analysis result."""
    anchor_points: List[Dict[str, str]] = Field(default_factory=list)
    vehicle_motions: List[VehicleMotion] = Field(default_factory=list)
    head_orientations: List[HeadOrientation] = Field(default_factory=list)
    consistency_check: List[ConsistencyCheck] = Field(default_factory=list)
    perspective_check: List[PerspectiveCheck] = Field(default_factory=list)
    conclusions: List[DirectionConclusion] = Field(default_factory=list)


class RoadInfo(BaseModel):
    """Information about a single road/lane group."""
    road_id: int
    name: str = ""
    pixel_bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    normal_direction: Literal["toward_top", "toward_bottom", "toward_left", "toward_right", "unknown"] = "unknown"
    direction_confidence: float = 0.0
    direction_evidence: List[DirectionEvidence] = Field(default_factory=list)
    lane_count: int = 0
    has_emergency_lane: bool = False
    emergency_lane_side: Optional[Literal["left", "right", "both", "none"]] = None


class SceneInfo(BaseModel):
    """Global scene understanding result from VLM."""
    road_count: int = 0
    roads: List[RoadInfo] = Field(default_factory=list)
    weather: str = "unknown"
    lighting: str = "unknown"
    traffic_density: str = "unknown"
    total_vehicles_estimate: int = 0
    scene_description: str = ""
    confidence: float = 0.0
    # Simple presence indicators (structured bools for unambiguous events)
    pedestrian_present: Optional[bool] = None
    non_motor_vehicle_present: Optional[bool] = None
    thrown_object_present: Optional[bool] = None
    # Full 6-step direction analysis result (populated by direction_analysis chain)
    direction_analysis: Optional[DirectionAnalysis] = None
