"""
Report and binary-encoding models for the traffic analyzer framework.

[文件说明]
作用:定义最终报告模型 Report(含 event_results、binary_encoding、adjudication_reasoning、audit_log、rejected/reject_reason 等)与二进制编码模型 BinaryEncoding。
上游:models/schemas.py、models/context.py 引用;由 core/report_generator.py 生成、core/report_markdown_renderer.py 渲染、orchestrator/analysis_orchestrator.py 与 reject_report_factory.py 返回。
下游:同包 event/scene/video 模块;pydantic。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from pydantic import BaseModel, Field, field_validator

from .event import AuditEntry, EventResult
from .scene import SceneInfo
from .video import VideoMetadata


class BinaryEncoding(BaseModel):
    """Binary encoding of detected events.

    Bit 9 is the normal indicator: set to 1 when no events detected, 0
    otherwise. An all-underscore encoding means the video was rejected by
    the prefilter (no analysis performed).
    """
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
    expert_candidates: List[Dict[str, Any]] = Field(default_factory=list, description="裁决前专家原始输出，用于debug")
    binary_encoding: BinaryEncoding = Field(default_factory=BinaryEncoding)
    final_classification: str = ""
    disposal_recommendations: List[str] = Field(default_factory=list)
    verification_results: Dict[str, str] = Field(default_factory=dict)
    llm_usage_stats: Dict[str, Any] = Field(default_factory=dict)
    analysis_duration_sec: float = 0.0
    generated_at: datetime = Field(default_factory=datetime.now)
    adjudication_reasoning: str = Field(default="", description="总体裁决推理")
    reasoning_chain: List[Dict[str, Any]] = Field(default_factory=list, description="逐事件推理链")
    audit_log: List[AuditEntry] = Field(default_factory=list, description="裁决审计日志")
    rejected: bool = Field(default=False, description="True if video was rejected by prefilter")
    reject_reason: str = Field(default="", description="Reason for prefilter rejection")
