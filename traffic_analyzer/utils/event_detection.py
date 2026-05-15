"""Shared event-detection helpers used by both the orchestrator and pipeline steps."""

from __future__ import annotations

from typing import Any, List

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.logic_engine import LogicEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCategory,
    EventInstance,
    EventResult,
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


def parse_direct_vlm_response(response: Any, category: EventCategory) -> EventResult:
    """Parse a direct_vlm VLM response into an EventResult."""
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
        return EventResult(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=detected,
            instances=instances,
            summary=str(data.get("summary", "")),
            confidence=float(data.get("confidence", 0.0)),
        )

    return EventResult(
        event_id=category.event_id,
        event_name=category.name_zh,
        detected=False,
        summary=f"VLM call failed or returned invalid data: {response.raw_text[:200]}",
    )


def detect_logic_chain(
    category: EventCategory,
    context: AnalysisContext,
    config_manager: ConfigManager,
    logic_engine: LogicEngine,
) -> EventResult:
    """Detect an event by executing its configured logic chain."""
    if not category.logic_chain_id:
        return EventResult(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=False,
            summary="Logic chain ID not configured",
        )

    logic_chain = config_manager.get_logic_chain(category.logic_chain_id)
    if not logic_chain:
        return EventResult(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=False,
            summary=f"Logic chain '{category.logic_chain_id}' not found",
        )

    return logic_engine.execute(logic_chain, context)

def dispatch_sequential_event(
    category: EventCategory,
    context: AnalysisContext,
    config_manager: ConfigManager,
    logic_engine: LogicEngine,
) -> EventResult:
    """Dispatch a single sequential event (logic_chain or scene_tag) to the correct handler.

    This is the unified entry point for non-direct_vlm events. Adding a new
    detection mode only requires updating this function.
    """
    if category.detection_mode == "logic_chain":
        return detect_logic_chain(category, context, config_manager, logic_engine)
    elif category.detection_mode == "scene_tag":
        return EventResult(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=False,
            summary="等待场景标签后处理",
        )
    else:
        return EventResult(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=False,
            summary=f"Unknown detection mode: {category.detection_mode}",
        )

