"""
Core data models for the traffic analyzer framework.

All inter-module data contracts are defined here as Pydantic models
to ensure type safety and validation across the system.
"""

from __future__ import annotations

import enum
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DetectionMode(str, enum.Enum):
    """How an event category is detected."""
    DIRECT_VLM = "direct_vlm"
    LOGIC_CHAIN = "logic_chain"
    SCENE_TAG = "scene_tag"


class ConfidenceLevel(str, enum.Enum):
    """Confidence level for event detection."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class StepType(str, enum.Enum):
    """Types of steps in a logic chain."""
    VLM_CALL = "vlm_call"
    COMPUTE = "compute"
    CONDITION = "condition"
    CV_FUSION = "cv_fusion"
    LOOP = "loop"
    AGGREGATE = "aggregate"


# ---------------------------------------------------------------------------
# Video & Frame Models
# ---------------------------------------------------------------------------

class VideoMetadata(BaseModel):
    """Basic information about the input video."""
    model_config = ConfigDict(frozen=True)

    file_path: str
    file_name: str
    duration_sec: float
    fps: float
    total_frames: int
    width: int
    height: int
    codec: str = ""
    bitrate: int = 0
    record_time: Optional[datetime] = None
    camera_id: Optional[str] = None


class Keyframe(BaseModel):
    """A single extracted keyframe from video."""
    model_config = ConfigDict(frozen=True)

    frame_id: int
    timestamp_sec: float
    image_path: Optional[str] = None
    image_data: Optional[bytes] = None
    quality_score: float = 0.0
    is_precision: bool = False


class KeyframeSequence(BaseModel):
    """A sequence of keyframes extracted from video."""
    coarse_frames: List[Keyframe] = Field(default_factory=list)
    precision_frames: List[Keyframe] = Field(default_factory=list)
    segment_frames: Dict[str, List[Keyframe]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scene Understanding
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Event Models
# ---------------------------------------------------------------------------

class EventCategory(BaseModel):
    """Definition of a detectable event category."""
    event_id: int = Field(..., ge=0, description="Zero-based index for binary encoding")
    event_code: str = Field(..., description="Short code, e.g. 'A', 'B'")
    name: str = Field(..., description="Human-readable name")
    name_zh: str = Field(..., description="Chinese name")
    description: str = Field(..., description="What this event is")
    detection_mode: DetectionMode = DetectionMode.DIRECT_VLM
    logic_chain_id: Optional[str] = Field(None, description="Reference to logic chain if mode=logic_chain")
    definition: str = Field("", description="Detailed definition for LLM prompt")
    visual_indicators: List[str] = Field(default_factory=list)
    confidence_threshold: float = 0.7
    prompt_template_id: Optional[str] = Field(
        None, description="Template ID for direct_vlm mode. Required when detection_mode=direct_vlm."
    )
    scene_boolean_field: Optional[str] = Field(
        None, description="SceneInfo boolean field name for scene_tag inference (e.g. 'pedestrian_present')"
    )
    scene_tag_key: Optional[str] = Field(
        None, description="Tag key in scene_description for scene_tag inference (e.g. '行人')"
    )
    is_active: bool = True


class CrossEventInferenceRule(BaseModel):
    """Rule for inferring a target event from a source event's detection result."""
    rule_id: str
    name: str = ""
    target_event_id: int          # 要推断的目标事件
    source_event_id: int          # 源事件（必须已检测到）
    source_description_keywords: List[str] = Field(
        default_factory=list,
        description="源事件实例描述中匹配任一关键词即触发推断",
    )
    confidence_multiplier: float = Field(0.9, ge=0.0, le=1.0)
    description_prefix: str = ""  # 推断实例的描述前缀
    reasoning: str = ""           # 推断理由


class EventInstance(BaseModel):
    """A single detected event instance."""
    event_id: int
    event_name: str
    event_name_en: str = ""
    vehicle_id: Optional[str] = None
    road_id: Optional[int] = None
    start_time_sec: float = 0.0
    end_time_sec: float = 0.0
    confidence: float = 0.0
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    evidence_frames: List[int] = Field(default_factory=list)
    description: str = ""
    reasoning: str = ""
    disposal_suggestion: str = ""


class EventResult(BaseModel):
    """Analysis result for a single event category."""
    event_id: int
    event_name: str
    event_name_en: str = ""
    detected: bool = False
    instances: List[EventInstance] = Field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    analysis_process: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logic Chain Models
# ---------------------------------------------------------------------------

class LogicStep(BaseModel):
    """A single step in a logic chain."""
    step_id: str
    step_type: StepType
    name: str = ""
    description: str = ""
    # For VLM_CALL steps
    prompt_template_id: Optional[str] = None
    input_images: Optional[List[str]] = None
    context_vars_mapping: Dict[str, str] = Field(default_factory=dict)
    output_key: str = ""
    response_schema: Optional[Dict[str, Any]] = None
    # For COMPUTE steps
    compute_expression: Optional[str] = None
    # For CONDITION steps
    condition_expression: Optional[str] = None
    true_next_step: Optional[str] = None
    false_next_step: Optional[str] = None
    # For LOOP steps
    loop_over_key: Optional[str] = None
    loop_body_chain_id: Optional[str] = None
    max_iterations: int = 10
    # For CV_FUSION steps
    cv_data_source: Optional[str] = None
    fusion_method: Optional[str] = None


class LogicChain(BaseModel):
    """A configurable multi-step logic chain for hard-case detection."""
    chain_id: str
    name: str
    name_zh: str = ""
    description: str = ""
    target_event_id: int
    precondition: Optional[str] = Field(None, description="Python expression that must evaluate to True for the chain to run. Can reference event_results, scene_understanding, video_meta, keyframes, local_vars.")
    steps: List[LogicStep]
    required_context_keys: List[str] = Field(default_factory=list)
    output_schema: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM/VLM Models
# ---------------------------------------------------------------------------

class PromptTemplate(BaseModel):
    """A reusable prompt template."""
    template_id: str
    name: str
    system_prompt: str = ""
    user_prompt: str = ""
    output_format_hint: str = ""
    example_input: Optional[Dict[str, Any]] = None
    example_output: Optional[str] = None


class LLMResponse(BaseModel):
    """Structured response from a VLM call."""
    success: bool = True
    raw_text: str = ""
    parsed_data: Dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    retry_count: int = 0


class LLMCallRecord(BaseModel):
    """Audit record of a single LLM call."""
    call_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    template_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    success: bool
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# CV Track Models
# ---------------------------------------------------------------------------

class Track(BaseModel):
    """Vehicle track from external CV system (e.g. merge_tracks.py)."""
    track_id: str
    road_id: Optional[int] = None
    boxes: List[List[float]] = Field(default_factory=list)
    enter_frame: int = 0
    exit_frame: int = 0
    total_displacement: float = 0.0
    lifetime_frames: int = 0
    lifetime_sec: float = 0.0
    merged_from: List[Dict[str, Any]] = Field(default_factory=list)
    appearance_feature: Optional[List[float]] = None


# ---------------------------------------------------------------------------
# Report Models
# ---------------------------------------------------------------------------

class BinaryEncoding(BaseModel):
    """Binary encoding of detected events."""
    encoding_string: str = ""
    event_count: int = 0
    detected_events: List[int] = Field(default_factory=list)

    @field_validator("encoding_string", mode="before")
    @classmethod
    def validate_encoding(cls, v: str) -> str:
        if v and not all(c in "01_" for c in v):
            raise ValueError("Encoding string must only contain 0, 1, or _")
        return v


class Report(BaseModel):
    """Final structured report."""
    video_info: VideoMetadata
    scene_summary: SceneInfo
    overall_traffic_description: str = ""
    event_results: List[EventResult] = Field(default_factory=list)
    binary_encoding: BinaryEncoding = Field(default_factory=BinaryEncoding)
    final_classification: str = ""
    disposal_recommendations: List[str] = Field(default_factory=list)
    verification_results: Dict[str, str] = Field(default_factory=dict)
    llm_usage_stats: Dict[str, Any] = Field(default_factory=dict)
    analysis_duration_sec: float = 0.0
    generated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Configuration Models
# ---------------------------------------------------------------------------

class SamplingConfig(BaseModel):
    """Video sampling configuration."""
    coarse_fps: float = 1.0
    precision_fps: float = 4.0
    coarse_quality_threshold: float = 0.05
    precision_quality_threshold: float = 0.1
    max_precision_segments: int = 10
    segment_padding_sec: float = 2.0


class LLMProviderConfig(BaseModel):
    """LLM provider configuration."""
    provider: str = "anthropic"
    api_key: str = ""
    base_url: Optional[str] = None
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout: float = 120.0
    max_retries: int = 3


class SystemConfig(BaseModel):
    """Complete system configuration."""
    llm_provider: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    output_dir: str = "./output"
    save_debug_frames: bool = False
    event_confidence_threshold: float = 0.7
    max_video_length_sec: float = 300.0
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Analysis Context
# ---------------------------------------------------------------------------

class AnalysisContext(BaseModel):
    """
    Mutable analysis context that flows through the entire pipeline.
    Passed between modules and updated incrementally.
    """
    model_config = ConfigDict(extra="allow")

    video_meta: Optional[VideoMetadata] = None
    config: Optional[SystemConfig] = None
    scene_understanding: Optional[SceneInfo] = None
    keyframes: Optional[KeyframeSequence] = None
    cv_tracks: Dict[str, Track] = Field(default_factory=dict)
    event_results: Dict[int, EventResult] = Field(default_factory=dict)
    local_vars: Dict[str, Any] = Field(default_factory=dict)
    llm_call_log: List[LLMCallRecord] = Field(default_factory=list)
    final_report: Optional[Report] = None

    def set_local(self, key: str, value: Any) -> None:
        """Set a local variable for logic chain execution."""
        self.local_vars[key] = value

    def get_local(self, key: str, default: Any = None) -> Any:
        """Get a local variable."""
        return self.local_vars.get(key, default)
