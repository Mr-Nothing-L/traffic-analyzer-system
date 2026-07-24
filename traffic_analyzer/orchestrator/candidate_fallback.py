"""Fallback conversion from expert candidates to event results.

[文件说明]
作用:兜底转换函数 fallback_candidates_to_event_results()——裁决步骤失败时,把专家层原始 EventCandidate 不做过滤地转成 EventResult,保证流水线仍能产出报告。
上游:orchestrator/analysis_orchestrator.py(裁决异常分支调用)。
下游:models/schemas.py(EventCandidate、EventResult)。
"""

from __future__ import annotations

from typing import List

from traffic_analyzer.models.schemas import EventCandidate, EventResult


def fallback_candidates_to_event_results(
    candidates: List[EventCandidate],
) -> List[EventResult]:
    """Fallback: convert raw expert candidates to EventResults (no filtering).

    This is used when the adjudication step fails and we need to fall back to
    the raw candidate outputs from the expert agent layer.

    Args:
        candidates: Raw expert candidates.

    Returns:
        EventResult objects derived from the candidates.
    """
    event_results: List[EventResult] = []
    for candidate in candidates:
        event_results.append(
            EventResult(
                event_id=candidate.event_id,
                event_name=candidate.event_name,
                detected=candidate.detected,
                summary=candidate.summary,
                instances=candidate.instances,
                expert_raw_description=candidate.raw_vlm_text,
                tool_results=candidate.tool_results,
            )
        )
    return event_results
