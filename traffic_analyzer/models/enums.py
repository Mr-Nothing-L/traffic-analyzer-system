"""
Enumeration types used across the traffic analyzer framework.
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
