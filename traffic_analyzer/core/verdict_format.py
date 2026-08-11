"""Verdict and event-definition JSON serialization for VLM prompts.

Extracted from ``sft_label_rewrite.py`` so that both the SFT label rewrite step
and the grounding verification step can share these serializers without either
depending on the other.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence

from traffic_analyzer.models.schemas import EventCategory, EventResult


def build_verdicts_json(
    event_results: Mapping[int, EventResult],
    categories: Sequence[EventCategory],
    only_positive: bool = False,
) -> str:
    """Serialize adjudicated verdicts (privileged hints) for the prompt.

    ``only_positive=True`` 时只序列化裁决阳性事件(grounding 核验口径):
    不含 ``detected`` 字段,``event_name`` 取 EventResult 上的名称。
    """
    verdicts: List[Dict[str, Any]] = []
    for cat in sorted(categories, key=lambda c: c.event_id):
        if not cat.is_active:
            continue  # 未激活类别不进入 prompt
        er = event_results.get(cat.event_id)
        if only_positive:
            if er is None or not getattr(er, "detected", False):
                continue  # 非阳性事件不进入核验 prompt
            verdicts.append(
                {
                    "event_id": cat.event_id,
                    "event_name": er.event_name,
                    "summary": er.summary,
                    "instances": [
                        {
                            "start_time_sec": inst.start_time_sec,
                            "end_time_sec": inst.end_time_sec,
                            "description": inst.description,
                            "reasoning": inst.reasoning,
                        }
                        for inst in er.instances
                    ],
                }
            )
            continue
        verdicts.append(
            {
                "event_id": cat.event_id,
                "event_name": cat.name_zh,
                "detected": bool(er.detected) if er is not None else False,
                "summary": er.summary if er is not None else "",
                "instances": [
                    {
                        "start_time_sec": inst.start_time_sec,
                        "end_time_sec": inst.end_time_sec,
                        "description": inst.description,
                        "reasoning": inst.reasoning,
                    }
                    for inst in (er.instances if er is not None else [])
                ],
            }
        )
    return json.dumps(verdicts, ensure_ascii=False, indent=2)


def build_event_definitions_json(categories: Sequence[EventCategory]) -> str:
    """Serialize event definitions for the prompt."""
    definitions = [
        {
            "event_id": cat.event_id,
            "event_name": cat.name_zh,
            "definition": cat.definition,
        }
        for cat in sorted(categories, key=lambda c: c.event_id)
        if cat.is_active
    ]
    return json.dumps(definitions, ensure_ascii=False, indent=2)
