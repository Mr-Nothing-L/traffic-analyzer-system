"""
Core data models for the traffic analyzer framework.

All inter-module data contracts are defined here as Pydantic models
to ensure type safety and validation across the system.

This module is a compatibility shim that re-exports models from the
domain-specific submodules under :mod:`traffic_analyzer.models`.
"""

from .config import LLMProviderConfig, SamplingConfig, SystemConfig
from .context import AnalysisContext
from .enums import ConfidenceLevel, DetectionMode
from .event import (
    AdjudicationResult,
    AdjudicationRule,
    AuditEntry,
    CrossEventInferenceRule,
    EventCandidate,
    EventCategory,
    EventInstance,
    EventResult,
)
from .llm import (
    FarObjectEnhancementConfig,
    LLMCallRecord,
    LLMResponse,
    PromptTemplate,
)
from .report import BinaryEncoding, Report
from .scene import (
    ConsistencyCheck,
    DirectionAnalysis,
    DirectionConclusion,
    DirectionEvidence,
    HeadOrientation,
    PerspectiveCheck,
    RoadInfo,
    SceneInfo,
    VehicleMotion,
)
from .video import Keyframe, KeyframeSequence, PrefilterResult, VideoMetadata

__all__ = [
    "DetectionMode",
    "ConfidenceLevel",
    "VideoMetadata",
    "Keyframe",
    "KeyframeSequence",
    "PrefilterResult",
    "DirectionEvidence",
    "VehicleMotion",
    "HeadOrientation",
    "ConsistencyCheck",
    "PerspectiveCheck",
    "DirectionConclusion",
    "DirectionAnalysis",
    "RoadInfo",
    "SceneInfo",
    "EventCategory",
    "CrossEventInferenceRule",
    "AdjudicationRule",
    "EventInstance",
    "EventResult",
    "EventCandidate",
    "AuditEntry",
    "AdjudicationResult",
    "FarObjectEnhancementConfig",
    "PromptTemplate",
    "LLMResponse",
    "LLMCallRecord",
    "BinaryEncoding",
    "Report",
    "SamplingConfig",
    "LLMProviderConfig",
    "SystemConfig",
    "AnalysisContext",
]
