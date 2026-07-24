"""
LLM/VLM call and prompt-template models for the traffic analyzer framework.

[文件说明]
作用:定义 PromptTemplate(prompt 模板,含远小目标增强配置 FarObjectEnhancementConfig)、LLMResponse(VLM 结构化响应)、LLMCallRecord(单次调用审计记录)。
上游:models/schemas.py(再导出);被 core/config_manager.py(加载 YAML prompt 模板)、core/vlm_engine.py、core/vlm_cache.py、core/expert_agent_far_enhancement.py 使用。
下游:pydantic。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class FarObjectEnhancementConfig(BaseModel):
    """Per-template configuration for far-distance object ROI enhancement.

    Defaults preserve the historical event_id=4 (non-motor vehicle) behaviour
    so that existing ``enable_far_object_enhancement: true`` YAML entries keep
    working without additional fields.
    """

    enabled: bool = False
    roi_template_id: str = "far_non_motor_roi_detection"
    min_area_px: int = 80
    max_aspect_ratio: float = 1.2
    enable_motion_filter: bool = True
    motion_score_threshold: float = 1.0
    motion_penalty: float = 5.0
    top_k: int = Field(default=2, ge=1)
    frame_selection: str = "all"  # "all" = per-frame ROI scan; "middle" = single middle frame (e.g. construction)


class PromptTemplate(BaseModel):
    """A reusable prompt template."""

    template_id: str
    name: str
    version: str = "1.0"
    system_prompt: str = ""
    user_prompt: str = ""
    output_format_hint: str = ""
    example_input: Optional[Dict[str, Any]] = None
    example_output: Optional[str] = None
    # A/B testing: percentage of traffic that uses this variant (0-100)
    # When multiple variants of the same template_id exist, one is selected
    # based on traffic_percentage. If not set, the latest version is used.
    traffic_percentage: Optional[int] = Field(None, ge=0, le=100)
    available_tools: List[str] = Field(
        default_factory=list,
        description="Tool names available in this prompt context (injected into prompt)"
    )
    far_object_enhancement: FarObjectEnhancementConfig = Field(
        default_factory=FarObjectEnhancementConfig,
        description="Far-distance object enhancement configuration"
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_enable_far_object_enhancement(cls, data: Any) -> Any:
        """Backward compatibility: convert legacy bool field to config object."""
        if isinstance(data, dict) and "enable_far_object_enhancement" in data:
            legacy_enabled = bool(data.pop("enable_far_object_enhancement"))
            if "far_object_enhancement" not in data:
                data["far_object_enhancement"] = {"enabled": legacy_enabled}
            else:
                fe = data["far_object_enhancement"]
                if isinstance(fe, dict) and "enabled" not in fe:
                    fe["enabled"] = legacy_enabled
        return data


class LLMResponse(BaseModel):
    """Structured response from a VLM call."""
    success: bool = True
    raw_text: str = ""
    parsed_data: Dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    retry_count: int = 0
    error_message: Optional[str] = None


class LLMCallRecord(BaseModel):
    """Audit record of a single LLM call."""
    call_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    template_id: str
    model: str
    provider: str = ""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    success: bool
    error_message: Optional[str] = None
