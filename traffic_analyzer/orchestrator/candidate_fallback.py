"""Fallback conversion from expert candidates to event results."""

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
