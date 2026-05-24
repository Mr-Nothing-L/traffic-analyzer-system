"""Shared event-detection helpers used by both the orchestrator and pipeline steps."""

from __future__ import annotations

from typing import Any, List

from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
)


def select_event_images(context: AnalysisContext, vlm_max_frames: int) -> List[Any]:
    """Select up to *vlm_max_frames* coarse keyframes (evenly distributed) for VLM detection."""
    images: List[Any] = []
    if not context.keyframes:
        return images

    max_frames = vlm_max_frames if vlm_max_frames > 0 else 6

    coarse = context.keyframes.coarse_frames
    if len(coarse) > max_frames:
        indices = [int(i * (len(coarse) - 1) / (max_frames - 1)) for i in range(max_frames)]
        selected = [coarse[i] for i in indices]
    else:
        selected = coarse

    images = [kf.image_data or kf.image_path for kf in selected]
    return [img for img in images if img is not None]


def parse_expert_response(response: Any, category: EventCategory) -> EventCandidate:
    """Parse a VLM response into an EventCandidate.

    This is the unified parser for ExpertAgent responses. It populates
    EventCandidate (which includes raw_vlm_response) rather than the older
    EventResult.
    """
    if response.success and isinstance(response.parsed_data, dict):
        data = response.parsed_data
        detected = bool(data.get("detected", False))
        instances_data = data.get("instances", [])
        instances = []
        if isinstance(instances_data, list):
            for inst in instances_data:
                if isinstance(inst, dict):
                    instances.append(
                        EventInstance(
                            event_id=category.event_id,
                            event_name=category.name_zh,
                            start_time_sec=float(inst.get("start_time_sec", 0.0)),
                            end_time_sec=float(inst.get("end_time_sec", 0.0)),
                            confidence=float(inst.get("confidence", 0.0)),
                            evidence_frames=inst.get("evidence_frames", []),
                            description=str(inst.get("description", "")),
                            reasoning=str(inst.get("reasoning", "")),
                        )
                    )
        return EventCandidate(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=detected,
            confidence=float(data.get("confidence", 0.0)),
            summary=str(data.get("summary", "")),
            instances=instances,
            raw_vlm_response=data,
            raw_vlm_text=response.raw_text if hasattr(response, "raw_text") else "",
        )

    return EventCandidate(
        event_id=category.event_id,
        event_name=category.name_zh,
        detected=False,
        summary=f"VLM call failed or returned invalid data: {response.raw_text[:200]}",
        raw_vlm_response={"raw_text": response.raw_text} if hasattr(response, "raw_text") else {},
        raw_vlm_text=response.raw_text if hasattr(response, "raw_text") else "",
    )


# Backward-compatible alias for code that still references the old name.
parse_direct_vlm_response = parse_expert_response
