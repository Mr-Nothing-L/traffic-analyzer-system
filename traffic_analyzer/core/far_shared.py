"""Shared helpers and JSON schemas for far-enhancement strategy modules.

Extracted from :mod:`traffic_analyzer.core.expert_agent_far_enhancement` as
part of the strategy decomposition (Task F3).  Contains only items used by
two or more strategy modules.
"""

from __future__ import annotations

from typing import Any, Dict


def parse_roi_confidence(value: Any) -> float:
    """Normalize ROI confidence to a 0-1 float, handling string legacy values."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        legacy_map = {"high": 0.85, "medium": 0.55, "low": 0.15}
        return legacy_map.get(value.lower(), 0.0)
    return 0.0


# JSON schema expected from the VLM for expert-agent responses.
_EXPERT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "is_target_explicitly_four_wheel_vehicle": {"type": "boolean"},
        "target_type": {"type": "string"},
        "summary": {"type": "string"},
        "instances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_time_sec": {"type": "number"},
                    "end_time_sec": {"type": "number"},
                    "evidence_frames": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "description": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
            },
        },
    },
}
