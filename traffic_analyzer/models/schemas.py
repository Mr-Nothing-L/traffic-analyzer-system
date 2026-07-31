"""
Core data models for the traffic analyzer framework.

All inter-module data contracts are defined here as Pydantic models
to ensure type safety and validation across the system.

This module is a compatibility shim that re-exports models from the
domain-specific submodules under :mod:`traffic_analyzer.models`.

[文件说明]
作用:纯数据模型的统一导出入口(兼容垫片),从 config/context/enums/event/llm/report/scene/video 各子模块再导出全部 Pydantic 模型,保证各模块间数据契约类型一致。
上游:全仓库消费方——traffic_analyzer/cli.py、core/*(config_manager、vlm_engine、video_preprocessor、pipeline_steps、grounding_verification、sft_label_rewrite、report_generator、report_markdown_renderer、expert_agent 系列、evidence_exporter、vlm_cache)、orchestrator/*(analysis_orchestrator、candidate_fallback、reject_report_factory、video_meta_extractor)及 tests/* 均经本文件导入。
下游:同包 config/context/enums/event/llm/report/scene/video 子模块;最终依赖 pydantic。
"""

from .config import LLMProviderConfig, SamplingConfig, SystemConfig
from .context import AnalysisContext
from .enums import ConfidenceLevel, DetectionMode
from .event import (
    AdjudicationResult,
    AdjudicationRule,
    AuditEntry,
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
from .scene import SceneInfo
from .video import Keyframe, KeyframeSequence, PrefilterResult, VideoMetadata

__all__ = [
    "DetectionMode",
    "ConfidenceLevel",
    "VideoMetadata",
    "Keyframe",
    "KeyframeSequence",
    "PrefilterResult",
    "SceneInfo",
    "EventCategory",
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
