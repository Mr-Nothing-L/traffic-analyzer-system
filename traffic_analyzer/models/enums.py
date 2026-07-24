"""
Enumeration types used across the traffic analyzer framework.

[文件说明]
作用:定义 DetectionMode(事件检测方式:direct_vlm / logic_chain / scene_tag / expert_agent)和 ConfidenceLevel(置信度:high/medium/low)两个枚举。
上游:models/event.py(EventCategory、EventInstance 使用);经 models/schemas.py 再导出给 core/config_manager.py 等。
下游:标准库 enum。
"""

from __future__ import annotations

import enum


class DetectionMode(str, enum.Enum):
    """How an event category is detected."""
    DIRECT_VLM = "direct_vlm"
    LOGIC_CHAIN = "logic_chain"
    SCENE_TAG = "scene_tag"
    EXPERT_AGENT = "expert_agent"


class ConfidenceLevel(str, enum.Enum):
    """Confidence level for event detection."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
