"""
Video and keyframe models for the traffic analyzer framework.

[文件说明]
作用:定义视频元数据 VideoMetadata(时长/帧率/分辨率等,冻结模型)、Keyframe 与 KeyframeSequence(粗采样+精细采样关键帧序列)、PrefilterResult(预处理预筛结果)。
上游:models/schemas.py、models/context.py、models/report.py 引用;由 core/video_preprocessor.py 与 orchestrator/video_meta_extractor.py 产出,被 orchestrator/analysis_orchestrator.py、core/report_generator.py 消费。
下游:pydantic。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class PrefilterResult(BaseModel):
    """Result of video prefilter checks."""
    should_process: bool = True
    reason: str = ""
    checks: Dict[str, Any] = Field(default_factory=dict)
