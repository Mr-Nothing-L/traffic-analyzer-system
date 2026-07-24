"""
Analysis context model for the traffic analyzer framework.

[文件说明]
作用:定义 AnalysisContext——贯穿整条分析流水线的可变上下文,承载 video_meta、config、scene_understanding、keyframes、event_candidates、event_results、final_report、llm_call_log 及 local_vars 逻辑链变量。
上游:models/schemas.py(再导出);由 orchestrator/analysis_orchestrator.py 创建并传入 core/pipeline_steps.py、core/grounding_verification.py、core/sft_label_rewrite.py、core/evidence_exporter.py 等各步骤就地读写。
下游:同包 config/event/llm/report/scene/video 模块;pydantic。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .config import SystemConfig
from .event import EventCandidate, EventResult
from .llm import LLMCallRecord
from .report import Report
from .scene import SceneInfo
from .video import KeyframeSequence, VideoMetadata


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
    event_candidates: Dict[int, EventCandidate] = Field(default_factory=dict)
    event_results: Dict[int, EventResult] = Field(default_factory=dict)
    local_vars: Dict[str, Any] = Field(default_factory=dict)
    llm_call_log: List[LLMCallRecord] = Field(default_factory=list)
    final_report: Optional[Report] = None
    output_dir: Optional[str] = Field(
        default=None,
        description="Directory where the final report will be written. "
                    "Used to place auxiliary assets (e.g. far-enhancement "
                    "composites) under <output_dir>/tmp_img/<video_stem> next "
                    "to the report so markdown references resolve.",
    )

    def set_local(self, key: str, value: Any) -> None:
        """Set a local variable for logic chain execution."""
        self.local_vars[key] = value

    def get_local(self, key: str, default: Any = None) -> Any:
        """Get a local variable."""
        return self.local_vars.get(key, default)
