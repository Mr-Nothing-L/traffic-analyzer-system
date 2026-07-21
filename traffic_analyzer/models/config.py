"""
Configuration models for the traffic analyzer framework.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


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
    llm_providers: List[LLMProviderConfig] = Field(default_factory=list)
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
    expert_enable_reflection: bool = Field(
        default_factory=lambda: os.getenv("EXPERT_ENABLE_REFLECTION", "true").lower()
        in ("1", "true", "yes", "on")
    )
    sft_label_enabled: bool = Field(
        default_factory=lambda: os.getenv("SFT_LABEL_ENABLE", "false").lower()
        in ("1", "true", "yes", "on")
    )
    sft_label_output_dir: str = Field(
        default_factory=lambda: os.getenv("SFT_LABEL_OUTPUT_DIR", "output/sft_labels")
    )

    @model_validator(mode="after")
    def _sync_llm_providers(self) -> "SystemConfig":
        if self.llm_providers and self.llm_provider:
            logger.warning(
                "Both llm_provider and llm_providers are set; llm_providers takes precedence."
            )
            self.llm_provider = self.llm_providers[0]
        elif not self.llm_providers and self.llm_provider:
            self.llm_providers = [self.llm_provider.model_copy()]
        return self
