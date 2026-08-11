"""
Event detection and adjudication models for the traffic analyzer framework.

[文件说明]
作用:定义事件检测与裁决相关模型:EventCategory(事件类目定义)、EventInstance/EventResult(单类目检测结果,含 grounding_note/grounding_overturned 锚定核验字段)、EventCandidate(专家层原始候选)、AdjudicationRule、AuditEntry、AdjudicationResult(裁决输出)。
上游:models/schemas.py、models/context.py、models/report.py 引用;间接服务于 core/expert_agent.py、core/pipeline_steps.py、core/grounding_verification.py、core/report_generator.py、core/report_markdown_renderer.py。
下游:models/enums.py;pydantic。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .enums import ConfidenceLevel, DetectionMode


class EventCategory(BaseModel):
    """Definition of a detectable event category."""
    event_id: int = Field(..., ge=1, description="Global event number (annotation doc v4.5 action number); bit position in binary encoding")
    event_code: str = Field(..., description="Short code, e.g. 'A', 'B'")
    name: str = Field(..., description="Human-readable name")
    name_zh: str = Field(..., description="Chinese name")
    description: str = Field(..., description="What this event is")
    detection_mode: DetectionMode = DetectionMode.EXPERT_AGENT
    definition: str = Field("", description="Detailed definition for LLM prompt")
    visual_indicators: List[str] = Field(default_factory=list)
    confidence_threshold: float = 0.7
    prompt_template_id: Optional[str] = Field(
        None, description="Template ID for expert_agent mode. Required for expert_agent mode."
    )
    tools: List[str] = Field(
        default_factory=list,
        description="List of tool names available to this expert agent"
    )
    is_active: bool = True


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
    grounding_note: str = Field(default="", description="锚定核验层对该事件的分析说明")
    grounding_overturned: bool = Field(default=False, description="裁决检出但被锚定核验推翻")
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
