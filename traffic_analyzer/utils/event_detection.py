"""Shared event-detection helpers used by both the orchestrator and pipeline steps."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
)

logger = logging.getLogger(__name__)


# JSON schema for the expert-response reflection step.
# Mirrors _EXPERT_RESPONSE_SCHEMA but is intentionally local so that
# event_detection.py does not depend on expert_agent internals.
_REFLECTION_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
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


def reflect_expert_candidate(
    candidate: EventCandidate,
    category: EventCategory,
    vlm_engine: Any,
    reflection_template: Any,
) -> EventCandidate:
    """Run a text-only reflection/consistency check on an ExpertAgent candidate.

    The reflection VLM checks whether ``detected`` matches the textual
    ``summary`` and ``instances`` reasoning. If they are inconsistent it
    returns a corrected candidate; otherwise it returns the original JSON.

    This function is fail-open: any parsing or VLM failure returns the
    original candidate unchanged.
    """
    candidate_json: str
    try:
        candidate_dict = {
            "event_id": candidate.event_id,
            "event_name": candidate.event_name,
            "detected": candidate.detected,
            "summary": candidate.summary,
            "instances": [
                {
                    "start_time_sec": inst.start_time_sec,
                    "end_time_sec": inst.end_time_sec,
                    "evidence_frames": inst.evidence_frames,
                    "description": inst.description,
                    "reasoning": inst.reasoning,
                }
                for inst in candidate.instances
            ],
        }
        candidate_json = json.dumps(candidate_dict, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning(
            "[reflect_expert_candidate] SERIALIZE_ERROR | event_id=%d event_name=%s | %s",
            candidate.event_id,
            candidate.event_name,
            exc,
            exc_info=True,
        )
        return candidate

    context_vars = {
        "event_name": category.name_zh,
        "event_definition": category.definition,
        "candidate_json": candidate_json,
    }

    # Render once for diagnostics; the engine call renders again internally.
    try:
        rendered_system, rendered_user = vlm_engine.render_prompt(
            reflection_template, context_vars
        )
        logger.debug(
            "[reflect_expert_candidate] RENDERED | event_id=%d event_name=%s system_len=%d user_len=%d",
            candidate.event_id,
            candidate.event_name,
            len(rendered_system),
            len(rendered_user),
        )
    except Exception as exc:
        logger.warning(
            "[reflect_expert_candidate] RENDER_WARNING | event_id=%d event_name=%s | %s",
            candidate.event_id,
            candidate.event_name,
            exc,
        )

    try:
        response = vlm_engine.call(
            template=reflection_template,
            images=[],
            context_vars=context_vars,
            response_schema=_REFLECTION_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        logger.warning(
            "[reflect_expert_candidate] VLM_ERROR | event_id=%d event_name=%s | %s",
            candidate.event_id,
            candidate.event_name,
            exc,
            exc_info=True,
        )
        return candidate

    if not response.success or not isinstance(response.parsed_data, dict):
        logger.warning(
            "[reflect_expert_candidate] PARSE_ERROR | event_id=%d event_name=%s raw_text=%s",
            candidate.event_id,
            candidate.event_name,
            getattr(response, "raw_text", "")[:200],
        )
        return candidate

    data = response.parsed_data
    original_detected = candidate.detected

    # Update detected flag if present.
    if "detected" in data:
        candidate.detected = bool(data["detected"])

    # Update summary if provided, preserving original if empty/missing.
    if data.get("summary"):
        candidate.summary = str(data["summary"])

    # Update instances if provided, preserving original if empty/missing.
    instances_data = data.get("instances")
    if isinstance(instances_data, list):
        new_instances = []
        for inst in instances_data:
            if not isinstance(inst, dict):
                continue
            evidence_frames = inst.get("evidence_frames", [])
            if not isinstance(evidence_frames, list):
                evidence_frames = []
            new_instances.append(
                EventInstance(
                    event_id=category.event_id,
                    event_name=category.name_zh,
                    start_time_sec=float(inst.get("start_time_sec", 0.0)),
                    end_time_sec=float(inst.get("end_time_sec", 0.0)),
                    evidence_frames=[int(f) for f in evidence_frames if isinstance(f, (int, float))],
                    description=str(inst.get("description", "")),
                    reasoning=str(inst.get("reasoning", "")),
                )
            )
        if new_instances:
            candidate.instances = new_instances

    if candidate.detected != original_detected:
        logger.warning(
            "[reflect_expert_candidate] AUTO_CORRECT detected=%s→%s | event_id=%d event_name=%s",
            original_detected,
            candidate.detected,
            candidate.event_id,
            candidate.event_name,
        )
    else:
        logger.info(
            "[reflect_expert_candidate] CONSISTENT | event_id=%d event_name=%s detected=%s",
            candidate.event_id,
            candidate.event_name,
            candidate.detected,
        )

    return candidate


def _sanitize_candidate(candidate: EventCandidate) -> EventCandidate:
    """Post-process candidate to fix common VLM inconsistencies.

    Handles the case where VLM outputs detected=true but the summary or
    all instance reasonings explicitly deny the event.
    """
    if not candidate.detected:
        return candidate

    summary = candidate.summary or ""
    instances = candidate.instances

    # Case 1: detected=true but summary explicitly says "not detected"
    denial_markers = ["未检测到", "没有检测到", "未出现", "不存在"]
    if any(m in summary for m in denial_markers) and not instances:
        logger.warning(
            "[sanitize] AUTO_CORRECT detected=true→false | event_id=%d event_name=%s | "
            "summary denies detection and no instances",
            candidate.event_id,
            candidate.event_name,
        )
        candidate.detected = False
        return candidate

    # Case 2: detected=true but every instance reasoning ends with "正常"
    # (e.g. "...一致 → 正常" or "...未进入应急车道")
    if instances:
        all_normal = True
        for inst in instances:
            reasoning = inst.reasoning or ""
            # If any instance clearly confirms the event, keep detected=true
            if reasoning and (
                "逆行" in reasoning
                or "违停" in reasoning
                or "停车" in reasoning
                or "事故" in reasoning
                or "占用" in reasoning
                or "施工" in reasoning
                or "拥堵" in reasoning
                or "抛洒" in reasoning
                or "变道" in reasoning
                or "行人" in reasoning
                or "摩托" in reasoning
            ):
                all_normal = False
                break
        if all_normal:
            logger.warning(
                "[sanitize] AUTO_CORRECT detected=true→false | event_id=%d event_name=%s | "
                "all instance reasonings describe normal conditions",
                candidate.event_id,
                candidate.event_name,
            )
            candidate.detected = False
            return candidate

    return candidate


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
                            evidence_frames=inst.get("evidence_frames", []),
                            description=str(inst.get("description", "")),
                            reasoning=str(inst.get("reasoning", "")),
                        )
                    )
        candidate = EventCandidate(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=detected,
            summary=str(data.get("summary", "")),
            instances=instances,
            raw_vlm_response=data,
            raw_vlm_text=response.raw_text if hasattr(response, "raw_text") else "",
        )
        return _sanitize_candidate(candidate)

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
