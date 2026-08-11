"""
Analysis context model for the traffic analyzer framework.

[文件说明]
作用:定义 AnalysisContext——贯穿整条分析流水线的可变上下文,承载 video_meta、config、scene_understanding、keyframes、event_candidates、event_results 及 output_dir。
上游:models/schemas.py(再导出);由 orchestrator/analysis_orchestrator.py 创建并传入 core/pipeline_steps.py、core/grounding_verification.py、core/sft_label_rewrite.py、core/evidence_exporter.py 等各步骤就地读写。
下游:同包 config/event/scene/video 模块;pydantic。
"""

from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, Field

from .config import SystemConfig
from .event import EventCandidate, EventResult
from .scene import SceneInfo
from .video import KeyframeSequence, VideoMetadata


class AnalysisContext(BaseModel):
    """
    Mutable analysis context that flows through the entire pipeline.
    Passed between modules and updated incrementally.
    """

    video_meta: Optional[VideoMetadata] = None
    config: Optional[SystemConfig] = None
    scene_understanding: Optional[SceneInfo] = None
    keyframes: Optional[KeyframeSequence] = None
    event_candidates: Dict[int, EventCandidate] = Field(default_factory=dict)
    event_results: Dict[int, EventResult] = Field(default_factory=dict)
    output_dir: Optional[str] = Field(
        default=None,
        description="Directory where the final report will be written. "
                    "Used to place auxiliary assets (e.g. far-enhancement "
                    "composites) under <output_dir>/tmp_img/<video_stem> next "
                    "to the report so markdown references resolve.",
    )
