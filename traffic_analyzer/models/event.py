"""
Event detection and adjudication models for the traffic analyzer framework.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .enums import ConfidenceLevel, DetectionMode


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
    tools: List[str] = Field(
        default_factory=list,
        description="List of tool names available to this expert agent"
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


class AdjudicationRule(BaseModel):
    """Business rule for the adjudication step to resolve conflicts among expert agents."""
    rule_id: str
    name: str = ""
    description: str = ""
    priority: int = Field(50, ge=0, le=1000, description="Priority for rule ordering (higher = more important)")


class EventInstance(BaseModel):
    """A single detected event instance."""
    event_id: int
    event_name: str
    event_name_en: str = ""
    vehicle_id: Optional[str] = None
    road_id: Optional[int] = None
    start_time_sec: float = 0.0
    end_time_sec: float = 0.0
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
    reasoning: str = ""
    analysis_process: List[str] = Field(default_factory=list)
    adjudication_reasoning: str = Field(default="", description="裁决层对该事件的详细推理过程")
    expert_raw_description: str = Field(default="", description="ExpertAgent原始自然语言描述")
    cv_evidence: str = Field(default="", description="CV帧差检测证据")
    tool_results: List[Dict[str, Any]] = Field(default_factory=list, description="工具调用结果列表")


class EventCandidate(BaseModel):
    """Raw detection candidate from an ExpertAgent (before adjudication)."""
    event_id: int
    event_name: str
    detected: bool = False
    summary: str = ""
    instances: List[EventInstance] = Field(default_factory=list)
    raw_vlm_response: Dict[str, Any] = Field(default_factory=dict)
    raw_vlm_text: str = Field(default="", description="VLM原始自然语言回复全文")
    cv_evidence: str = Field(default="", description="CV帧差检测证据")
    tool_results: List[Dict[str, Any]] = Field(default_factory=list, description="工具调用结果列表")
    # Structured veto fields populated by the far-enhancement final classifier.
    # is_target_explicitly_four_wheel_vehicle=true means the boxed target is a
    # car/SUV/truck/van and the event should be vetoed.
    is_target_explicitly_four_wheel_vehicle: Optional[bool] = None
    target_type: str = Field(default="", description="结构化目标类型，如行人/非机动车/汽车/施工元素等")


class AuditEntry(BaseModel):
    """Single exclusion/inclusion decision record from adjudication."""
    event_id: int
    event_name: str = ""
    action: Literal["included", "excluded"] = "included"
    reason: str = ""
    rule_id: Optional[str] = None
    reasoning: str = Field(default="", description="详细的裁决思考过程")
    rule_description: str = Field(default="", description="引用的规则/事件名称详细描述")


class AdjudicationResult(BaseModel):
    """Output of the adjudication step."""
    event_results: List[EventResult] = Field(default_factory=list)
    audit_log: List[AuditEntry] = Field(default_factory=list)
    adjudication_reasoning: str = ""
    reasoning_chain: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="裁决完整推理链，每条记录包含event_id、event_name、思考过程、决策、依据"
    )
    raw_vlm_text: str = Field(default="", description="裁决VLM原始自然语言回复全文")
