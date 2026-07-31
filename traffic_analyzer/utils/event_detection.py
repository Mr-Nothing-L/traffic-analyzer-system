"""Shared event-detection helpers used by both the orchestrator and pipeline steps.

[文件说明]
作用:事件检测共享辅助函数:``select_event_images`` 按 vlm_max_frames 均匀抽取
    粗筛关键帧供 VLM 检测;``parse_expert_response`` 将 VLM 响应解析为
    EventCandidate(严格布尔解析,防 ``bool('false')`` 误判);
    ``reflect_expert_candidate`` 对专家候选做文本反思一致性检查(fail-open)。
上游:``select_event_images`` 被 core/pipeline_steps.py、core/sft_label_rewrite.py、
    core/grounding_verification.py 共用;解析/反思函数被 core/expert_agent.py、
    core/expert_agent_far_enhancement.py 使用。
下游:models/schemas.py 的 AnalysisContext/EventCandidate/EventCategory/EventInstance;
    VLM 引擎与反思 prompt 模板由调用方以参数传入。
"""

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


def _parse_strict_bool(value: Any) -> bool:
    """Strict boolean normalization for VLM outputs.

    Only ``True`` or the string ``'true'`` (case-insensitive, stripped) is
    treated as True; everything else is False. This prevents the classic
    ``bool('false') is True`` false positive.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert *value* to float, falling back to *default* on malformed input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        candidate.detected = _parse_strict_bool(data["detected"])

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
                    start_time_sec=_safe_float(inst.get("start_time_sec", 0.0)),
                    end_time_sec=_safe_float(inst.get("end_time_sec", 0.0)),
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


def select_event_images(context: AnalysisContext, vlm_max_frames: int) -> List[Any]:
    """Select up to *vlm_max_frames* coarse keyframes (evenly distributed) for VLM detection."""
    images: List[Any] = []
    if not context.keyframes:
        return images

    max_frames = vlm_max_frames if vlm_max_frames > 0 else 6

    coarse = context.keyframes.coarse_frames
    if len(coarse) > max_frames:
        if max_frames <= 1:
            # Degenerate config (e.g. VLM_MAX_FRAMES=1): take the middle frame
            # instead of dividing by (max_frames - 1) == 0.
            selected = [coarse[len(coarse) // 2]]
        else:
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
        detected = _parse_strict_bool(data.get("detected", False))
        instances_data = data.get("instances", [])
        instances = []
        if isinstance(instances_data, list):
            for inst in instances_data:
                if not isinstance(inst, dict):
                    logger.warning(
                        "[parse_expert_response] MALFORMED_INSTANCE | event_id=%d event_name=%s | not an object: %r",
                        category.event_id,
                        category.name_zh,
                        inst,
                    )
                    continue
                evidence_frames = inst.get("evidence_frames", [])
                if not isinstance(evidence_frames, list):
                    evidence_frames = []
                try:
                    instances.append(
                        EventInstance(
                            event_id=category.event_id,
                            event_name=category.name_zh,
                            start_time_sec=_safe_float(inst.get("start_time_sec", 0.0)),
                            end_time_sec=_safe_float(inst.get("end_time_sec", 0.0)),
                            evidence_frames=[
                                int(f) for f in evidence_frames if isinstance(f, (int, float))
                            ],
                            description=str(inst.get("description", "")),
                            reasoning=str(inst.get("reasoning", "")),
                        )
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "[parse_expert_response] MALFORMED_INSTANCE | event_id=%d event_name=%s | %s",
                        category.event_id,
                        category.name_zh,
                        exc,
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
        return candidate

    return EventCandidate(
        event_id=category.event_id,
        event_name=category.name_zh,
        detected=False,
        summary=f"VLM call failed or returned invalid data: {response.raw_text[:200]}",
        raw_vlm_response={"raw_text": response.raw_text} if hasattr(response, "raw_text") else {},
        raw_vlm_text=response.raw_text if hasattr(response, "raw_text") else "",
    )
