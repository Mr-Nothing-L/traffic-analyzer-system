"""Pydantic data models for the traffic analyzer framework.

[文件说明]
作用:models 包入口,从 schemas 再导出全部数据模型(VideoMetadata、EventResult、AnalysisContext、Report、SystemConfig 等)。
上游:任何 `import traffic_analyzer.models.*` 的模块都会先执行本文件;实际消费方全仓统一经由 models/schemas.py 导入。
下游:models/schemas.py(真正的再导出层)。
"""

from .schemas import (
    VideoMetadata,
    Keyframe,
    KeyframeSequence,
    SceneInfo,
    EventCategory,
    EventResult,
    PromptTemplate,
    LLMResponse,
    LLMCallRecord,
    AnalysisContext,
    Report,
    SystemConfig,
)

__all__ = [
    "VideoMetadata",
    "Keyframe",
    "KeyframeSequence",
    "SceneInfo",
    "EventCategory",
    "EventResult",
    "PromptTemplate",
    "LLMResponse",
    "LLMCallRecord",
    "AnalysisContext",
    "Report",
    "SystemConfig",
]
