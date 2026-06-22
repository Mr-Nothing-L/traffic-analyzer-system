"""
Video and keyframe models for the traffic analyzer framework.
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
