"""
Configuration models for the traffic analyzer framework.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field


class SamplingConfig(BaseModel):
    """Video sampling configuration."""
    coarse_fps: float = Field(default_factory=lambda: float(os.getenv("SAMPLING_FPS", "1.0")))
    precision_fps: float = Field(default_factory=lambda: float(os.getenv("SAMPLING_FPS", "1.0")))
    coarse_quality_threshold: float = 0.05
    precision_quality_threshold: float = 0.1
    max_precision_segments: int = 10
    segment_padding_sec: float = 2.0
    prefilter_enabled: bool = Field(default_factory=lambda: os.getenv("PREFILTER_ENABLE", "false").lower() == "true")
    prefilter_brightness_threshold: float = Field(default_factory=lambda: float(os.getenv("PREFILTER_BRIGHTNESS_THRESHOLD", "50.0")))
    prefilter_min_bitrate: int = Field(default_factory=lambda: int(os.getenv("PREFILTER_MIN_BITRATE", "10000")))
    prefilter_min_duration_sec: float = Field(default_factory=lambda: float(os.getenv("PREFILTER_MIN_DURATION_SEC", "5.0")))
    prefilter_max_duration_sec: float = Field(default_factory=lambda: float(os.getenv("PREFILTER_MAX_DURATION_SEC", "15.0")))


class LLMProviderConfig(BaseModel):
    """LLM provider configuration."""
    provider: str = "anthropic"
    api_key: str = ""
    base_url: Optional[str] = None
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout: float = 300.0
    max_retries: int = 3
    enable_cache: bool = True
    cache_max_size: int = 128
    disk_cache_path: Optional[str] = None
    disk_cache_max_entries: int = 2000


class SystemConfig(BaseModel):
    """Complete system configuration."""
    llm_provider: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    output_dir: str = "./output"
    save_debug_frames: bool = False
    event_confidence_threshold: float = 0.7
    max_video_length_sec: float = 300.0
    log_level: str = "INFO"
    scene_understanding_min_frames: int = Field(
        default_factory=lambda: int(os.getenv("SCENE_UNDERSTANDING_MIN_FRAMES", "10"))
    )
    vlm_max_frames: int = Field(
        default_factory=lambda: int(os.getenv("VLM_MAX_FRAMES", "10"))
    )
