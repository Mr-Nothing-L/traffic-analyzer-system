"""Per-frame ROI strategy for far-distance enhancement (generic events).

Extracted verbatim from ``FarEnhancementDetector._detect_with_far_enhancement``
(else branch) and its exclusive helpers as part of the strategy decomposition
(Task F3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from traffic_analyzer.core.car_semantic_veto import (
    is_no_structure_reasoning,
    select_car_veto_check,
    should_veto_as_car,
)
from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.far_shared import _EXPERT_RESPONSE_SCHEMA, parse_roi_confidence
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    PromptTemplate,
)
from traffic_analyzer.utils.bbox_geometry import (
    compute_bbox_area_px,
    compute_bbox_aspect_ratio,
    is_bbox_aspect_valid,
    is_bbox_large_enough,
)
from traffic_analyzer.utils.event_detection import (
    _parse_strict_bool,
    _safe_float,
)
from traffic_analyzer.utils.image_drawing import load_image
from traffic_analyzer.utils.roi_composite import create_composite
from traffic_analyzer.utils.roi_motion import (
    compute_roi_motion_score,
    create_motion_comparison_composite,
)
from traffic_analyzer.utils.progress import get_reporter as _get_progress_reporter

logger = logging.getLogger(__name__)

# Default far-enhancement parameters.  Most are overridden per-template via
# ``PromptTemplate.far_object_enhancement``; these constants act as fallback
# defaults and as fixed values that are not exposed in the config object.
_FAR_MOTION_ENLARGE_SCALE = 3.0
_FAR_MOTION_GAUSSIAN_KERNEL = (3, 3)
_FAR_MOTION_PIXEL_THRESHOLD = 8.0

# JSON schema for the far-distance per-frame ROI detection.
_ROI_DETECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["bbox_norm"],
    "properties": {
        "bbox_norm": {
            "anyOf": [
                {"type": "array", "items": {"type": "number"}},
                {"type": "null"},
            ]
        },
        "occluded": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
}

# JSON schema for the far-distance object final classifier.
# This is intentionally separate from the shared _EXPERT_RESPONSE_SCHEMA because
# the final classifier returns a minimal {detected, reason} object.
_FAR_ENHANCEMENT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "is_target_explicitly_four_wheel_vehicle": {"type": "boolean"},
        "target_type": {"type": "string"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}


def _build_minimal_final_classifier_template(
    category: EventCategory,
    template: PromptTemplate,
) -> PromptTemplate:
    """Build a concise retry prompt when the first classifier response is unparseable."""
    if category.event_id == 5:
        example_json = (
            '{\n'
            '  "detected": <true|false>,\n'
            '  "is_target_explicitly_four_wheel_vehicle": <true|false>,\n'
            '  "target_type": "<汽车|摩托车|电动车|非机动车|施工元素|行人|无法确定>",\n'
            '  "reason": "<一句话判断理由>"\n'
            '}'
        )
    else:
        # Pedestrian and construction use the full expert response shape.
        example_json = (
            '{\n'
            '  "detected": <true|false>,\n'
            '  "is_target_explicitly_four_wheel_vehicle": <true|false>,\n'
            '  "target_type": "<汽车|摩托车|电动车|非机动车|施工元素|行人|无法确定>",\n'
            '  "instances": [...],\n'
            '  "summary": "<总体评估>"\n'
            '}'
        )
    minimal_user = (
        "你刚才的输出格式不正确，无法按 JSON schema 解析。请仅根据图像重新输出合法 JSON，"
        "不要包含 markdown 代码块、解释或其他任何文字。\n\n"
        "必须包含以下字段：\n"
        f"{example_json}\n\n"
        "重要提示：\n"
        "- is_target_explicitly_four_wheel_vehicle 只回答红色方框内的目标本身是否是四轮机动车（汽车/SUV/货车/面包车）。\n"
        "- 如果目标是行人、摩托车、电动车、非机动车、施工元素，必须填 false。\n"
        "- 如果只是背景中提到汽车、被汽车取代、与汽车对比，必须填 false。"
    )
    return PromptTemplate(
        template_id=template.template_id,
        name=template.name,
        version=template.version,
        system_prompt=template.system_prompt,
        user_prompt=minimal_user,
        output_format_hint="JSON",
        example_input=None,
        example_output=None,
        traffic_percentage=template.traffic_percentage,
        available_tools=[],
        far_object_enhancement=template.far_object_enhancement,
    )


def _build_far_candidate(
    category: EventCategory,
    frame_info: Dict[str, Any],
    reason: str,
    frame_analysis_log: List[Dict[str, Any]],
    raw_text: Optional[str] = None,
    fallback: bool = False,
) -> EventCandidate:
    """Build a positive EventCandidate from a far-enhancement frame_info dict."""
    global_index = frame_info["global_index"]
    adjacent_index = frame_info["adjacent_index"]
    bbox_norm = frame_info["bbox_norm"]
    composite_ref = frame_info["composite_ref"]
    motion_composite_ref = frame_info["motion_composite_ref"]

    far_enhancement: Dict[str, Any] = {
        "selected_frame_index": global_index,
        "bbox_norm": bbox_norm,
        "reason": reason,
        "frame_analysis_log": frame_analysis_log,
    }
    if fallback:
        far_enhancement["fallback"] = True

    raw_text = raw_text if raw_text is not None else reason
    return EventCandidate(
        event_id=category.event_id,
        event_name=category.name_zh,
        detected=True,
        summary=f"检测到{category.name_zh}：{reason}",
        instances=[
            EventInstance(
                event_id=category.event_id,
                event_name=category.name_zh,
                start_time_sec=0.0,
                end_time_sec=0.0,
                evidence_frames=[global_index, adjacent_index],
                description=reason,
                reasoning=reason,
            )
        ],
        raw_vlm_response={
            "composite_image_path": composite_ref,
            "motion_composite_image_path": motion_composite_ref,
            "far_enhancement": far_enhancement,
        },
        raw_vlm_text=raw_text,
    )


def _run_final_classifier(
    category: EventCategory,
    vlm_engine: VLMInferenceEngine,
    frame_info: Dict[str, Any],
    template: PromptTemplate,
    context_vars: Dict[str, Any],
) -> Optional[EventCandidate]:
    """Run the final far-distance classifier on a candidate's composites.

    The expected response format depends on the event category:
    - event_id=4 (pedestrian) uses the full expert response schema with
      ``detected`` / ``instances`` / ``summary``.
    - event_id=5 (non-motor vehicle) and other categories use the minimal
      ``{detected, reason}`` classifier schema.

    The classifier is now expected to emit a structured veto field
    ``is_target_explicitly_four_wheel_vehicle``. When the field is missing
    or the response cannot be parsed, we fall back to the legacy regex
    checks and, as a last resort, retry once with a shorter prompt.
    """
    global_index = frame_info["global_index"]

    _get_progress_reporter().phase("reclassify")

    # Pedestrian final classifier returns a full expert response so that
    # the adjudication layer receives the same structured instances as
    # other expert agents.
    if category.event_id == 4:
        response_schema = _EXPERT_RESPONSE_SCHEMA
    else:
        response_schema = _FAR_ENHANCEMENT_RESPONSE_SCHEMA

    images = [
        frame_info["composite_path"],
        frame_info["motion_composite_path"],
    ]

    def _call_classifier(
        prompt_template: PromptTemplate,
    ) -> Any:
        return vlm_engine.call(
            template=prompt_template,
            images=images,
            context_vars=context_vars,
            response_schema=response_schema,
        )

    try:
        response = _call_classifier(template)
    except FatalAPIError:
        raise
    except Exception as exc:
        logger.error(
            "[expert_agent:_run_final_classifier] FINAL_CALL_ERROR | event_id=%d frame=%d | %s",
            category.event_id,
            global_index,
            exc,
            exc_info=True,
        )
        return None

    # Retry once with a minimal prompt if the first response is unparseable.
    if not response.success or not isinstance(response.parsed_data, dict):
        logger.warning(
            "[expert_agent:_run_final_classifier] PARSE_RETRY | event_id=%d frame=%d success=%s error=%s",
            category.event_id,
            global_index,
            response.success,
            getattr(response, "raw_text", "")[:200],
        )
        retry_template = _build_minimal_final_classifier_template(category, template)
        try:
            retry_response = _call_classifier(retry_template)
        except Exception as exc:
            logger.error(
                "[expert_agent:_run_final_classifier] RETRY_CALL_ERROR | event_id=%d frame=%d | %s",
                category.event_id,
                global_index,
                exc,
                exc_info=True,
            )
            retry_response = None

        if (
            retry_response is not None
            and retry_response.success
            and isinstance(retry_response.parsed_data, dict)
        ):
            logger.info(
                "[expert_agent:_run_final_classifier] RETRY_SUCCESS | event_id=%d frame=%d",
                category.event_id,
                global_index,
            )
            response = retry_response
        else:
            logger.warning(
                "[expert_agent:_run_final_classifier] RETRY_FAILED | event_id=%d frame=%d",
                category.event_id,
                global_index,
            )
            # Preserve the raw text so fallback can still apply regex.
            frame_info["negative_final_reason"] = str(
                getattr(response, "raw_text", "")
            )[:2000]
            return None

    parsed = response.parsed_data
    detected = _parse_strict_bool(parsed.get("detected", False))

    # Preserve the classifier's raw output before any car-semantic override.
    # This lets fallback distinguish "classifier was negative" from
    # "classifier was positive but vetoed because of a car keyword".
    frame_info["raw_final_detected"] = detected
    frame_info["is_target_explicitly_four_wheel_vehicle"] = parsed.get(
        "is_target_explicitly_four_wheel_vehicle"
    )
    frame_info["target_type"] = parsed.get("target_type", "")

    # ------------------------------------------------------------------
    # Pedestrian branch: keep instances/summary from the classifier.
    # ------------------------------------------------------------------
    if category.event_id == 4:
        final_summary = str(parsed.get("summary", ""))
        frame_info["raw_final_reason"] = final_summary
        final_instances = parsed.get("instances") or []
        if not isinstance(final_instances, list):
            final_instances = []
        final_instances = [
            inst for inst in final_instances if isinstance(inst, dict)
        ]

        # Structured car veto: if the classifier explicitly says the boxed
        # target is a four-wheel vehicle, override detected=False. Fallback
        # to regex checks only when the structured field is missing.
        text_for_veto = " ".join(
            [
                final_summary,
                *(
                    str(inst.get("description", ""))
                    + " "
                    + str(inst.get("reasoning", ""))
                    for inst in final_instances
                ),
            ]
        )
        if detected and should_veto_as_car(
            parsed, text_for_veto, category.event_id
        ):
            logger.info(
                "[expert_agent:_run_final_classifier] CAR_OVERRIDDEN | event_id=%d frame=%d summary=%s",
                category.event_id,
                global_index,
                final_summary,
            )
            detected = False

        if detected:
            normalized_instances: List[EventInstance] = []
            for inst in final_instances:
                evidence_frames = inst.get("evidence_frames")
                if not isinstance(evidence_frames, list):
                    evidence_frames = []
                normalized_instances.append(
                    EventInstance(
                        event_id=category.event_id,
                        event_name=category.name_zh,
                        start_time_sec=_safe_float(inst.get("start_time_sec", 0.0)),
                        end_time_sec=_safe_float(inst.get("end_time_sec", 0.0)),
                        evidence_frames=[
                            int(f) for f in evidence_frames if isinstance(f, (int, float))
                        ]
                        or [global_index, frame_info["adjacent_index"]],
                        description=str(inst.get("description", ""))
                        or final_summary,
                        reasoning=str(inst.get("reasoning", "")) or final_summary,
                    )
                )
            if not normalized_instances:
                normalized_instances = [
                    EventInstance(
                        event_id=category.event_id,
                        event_name=category.name_zh,
                        evidence_frames=[global_index, frame_info["adjacent_index"]],
                        description=final_summary,
                        reasoning=final_summary,
                    )
                ]

            far_enhancement: Dict[str, Any] = {
                "selected_frame_index": global_index,
                "bbox_norm": frame_info["bbox_norm"],
                "reason": final_summary,
                "frame_analysis_log": frame_info.get("frame_analysis_log", []),
            }
            return EventCandidate(
                event_id=category.event_id,
                event_name=category.name_zh,
                detected=True,
                summary=final_summary
                or f"检测到{category.name_zh}",
                instances=normalized_instances,
                raw_vlm_response={
                    "composite_image_path": frame_info["composite_ref"],
                    "motion_composite_image_path": frame_info["motion_composite_ref"],
                    "far_enhancement": far_enhancement,
                },
                raw_vlm_text=response.raw_text,
                is_target_explicitly_four_wheel_vehicle=False,
                target_type=parsed.get("target_type", "行人"),
            )

        negative_reason = final_summary or "未检测到高速公路行人。"
        frame_info["negative_final_reason"] = negative_reason
        return None

    # ------------------------------------------------------------------
    # Non-motor / minimal branch: {detected, reason}.
    # ------------------------------------------------------------------
    final_reason = str(parsed.get("reason", ""))
    frame_info["raw_final_reason"] = final_reason

    if detected and should_veto_as_car(
        parsed, final_reason, category.event_id
    ):
        logger.info(
            "[expert_agent:_run_final_classifier] CAR_OVERRIDDEN | event_id=%d frame=%d reason=%s",
            category.event_id,
            global_index,
            final_reason,
        )
        detected = False

    if detected:
        candidate = _build_far_candidate(
            category,
            frame_info,
            final_reason,
            frame_info.get("frame_analysis_log", []),
            raw_text=response.raw_text,
        )
        candidate.is_target_explicitly_four_wheel_vehicle = False
        candidate.target_type = parsed.get("target_type", "非机动车")
        return candidate
    # Preserve the negative classifier reason so fallback logic can still
    # apply the car-semantic veto.
    frame_info["negative_final_reason"] = final_reason
    return None


def _accept_fallback(
    category: EventCategory,
    frame_info: Dict[str, Any],
    frame_analysis_log: List[Dict[str, Any]],
) -> Optional[EventCandidate]:
    """Promote a previously negative candidate to detected=True if safe."""
    if frame_info.get("occluded"):
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_OCCLUDED | event_id=%d frame=%d",
            category.event_id,
            frame_info["global_index"],
        )
        return None

    # Pedestrians require a higher confidence bar before we override the
    # final classifier, because the ROI detector is intentionally permissive.
    confidence_threshold = 0.7 if category.event_id == 4 else 0.5
    confidence = parse_roi_confidence(frame_info.get("confidence", 0.0))
    if confidence < confidence_threshold:
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_CONFIDENCE | event_id=%d frame=%d confidence=%s threshold=%s",
            category.event_id,
            frame_info["global_index"],
            confidence,
            confidence_threshold,
        )
        return None

    # For pedestrians, re-validate that the ROI itself passed the configured
    # size/aspect filters. These checks already happened during candidate
    # collection, but repeating them here makes the fallback self-contained.
    if category.event_id == 4:
        if not frame_info.get("area_px"):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_NO_AREA | event_id=%d frame=%d",
                category.event_id,
                frame_info["global_index"],
            )
            return None
        aspect_ratio = float(frame_info.get("aspect_ratio", 0.0))
        if aspect_ratio <= 0.0:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_ASPECT | event_id=%d frame=%d aspect=%s",
                category.event_id,
                frame_info["global_index"],
                aspect_ratio,
            )
            return None

    # Apply car-semantic veto to the classifier's negative reasoning. The
    # ROI reason may legitimately mention surrounding cars for comparison,
    # so only the final classifier's explicit car description is used here.
    # Pedestrians use a stricter veto because a pedestrian near a vehicle is
    # still a pedestrian.
    negative_reason = str(frame_info.get("negative_final_reason", ""))
    structured_is_car = frame_info.get("is_target_explicitly_four_wheel_vehicle")
    if structured_is_car is True:
        logger.info(
            "[expert_agent:_accept_fallback] FALLBACK_REJECT_CAR_STRUCTURED | event_id=%d frame=%d",
            category.event_id,
            frame_info["global_index"],
        )
        return None
    if structured_is_car is None:
        # Structured field missing: fall back to regex reasoning checks.
        car_veto_check = select_car_veto_check(category.event_id)
        if car_veto_check(negative_reason):
            logger.info(
                "[expert_agent:_accept_fallback] FALLBACK_REJECT_CAR_REGEX | event_id=%d frame=%d negative_reason=%s",
                category.event_id,
                frame_info["global_index"],
                negative_reason,
            )
            return None
    # The "no structure" veto is specific to non-motor vehicles (event_id=5):
    # it blocks fallback when the classifier says the box lacks identifiable
    # vehicle-structure evidence (wheels, handlebars, etc.). For pedestrians
    # the ROI detector already verified an upright human silhouette, so a
    # vague "cannot confirm" classifier reason should not block fallback.
    if category.event_id == 5 and is_no_structure_reasoning(negative_reason):
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_NO_STRUCTURE | event_id=%d frame=%d negative_reason=%s",
            category.event_id,
            frame_info["global_index"],
            negative_reason,
        )
        return None

    # Build a self-consistent summary. For pedestrians, anchor the summary
    # in the ROI detector's own reason so the report's "expert raw output"
    # matches the per-frame ROI evidence table.
    if category.event_id == 4:
        roi_reason = str(frame_info.get("reason", ""))
        fallback_reason = (
            f"检测到高速公路行人。第{frame_info['global_index']}帧红色方框内"
            f"{'，' + roi_reason if roi_reason else '目标位于道路区域，直立人形轮廓'}"
        )
    else:
        fallback_reason = (
            f"检测到远距离{category.name_zh}。第{frame_info['global_index']}帧红色方框内目标位于道路区域，"
            f"尺寸与宽高比符合{category.name_zh}特征。"
        )
    candidate = _build_far_candidate(
        category,
        frame_info,
        fallback_reason,
        frame_analysis_log,
        raw_text=fallback_reason,
        fallback=True,
    )
    candidate.is_target_explicitly_four_wheel_vehicle = False
    candidate.target_type = "行人" if category.event_id == 4 else "非机动车"
    return candidate


def _score_far_candidates(
    candidates: List[Dict[str, Any]],
    motion_score_threshold: float = 1.0,
    motion_penalty: float = 5.0,
) -> List[Dict[str, Any]]:
    """Rank ROI candidates by confidence, area, aspect, occlusion and motion."""
    if not candidates:
        return []
    max_area = max(c["area_px"] for c in candidates)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for candidate in candidates:
        conf = 3.0 * parse_roi_confidence(candidate.get("confidence", 0.0))
        area_score = (
            candidate["area_px"] / max_area if max_area > 0 else 0.0
        )
        aspect_penalty = max(0.0, candidate["aspect_ratio"] - 1.0)
        occlusion_penalty = 2.0 if candidate.get("occluded") else 0.0
        motion_score = float(
            candidate.get("motion_score", {}).get("motion_score", 0.0)
        )
        applied_motion_penalty = (
            motion_penalty
            if motion_score < motion_score_threshold
            else 0.0
        )
        score = (
            conf
            + area_score
            - aspect_penalty
            - occlusion_penalty
            - applied_motion_penalty
        )
        candidate["score"] = score
        scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in scored]


def _log_frame(
    frame_analysis_log: List[Dict[str, Any]],
    frame_log: Dict[str, Any],
    reason: Optional[str] = None,
) -> None:
    """Append a frame log, optionally setting its reason first."""
    if reason is not None:
        frame_log["reason"] = reason
    frame_analysis_log.append(frame_log)


def _generate_low_confidence_evidence(
    category: EventCategory,
    candidates: List[Dict[str, Any]],
    images: List[Any],
    output_dir: Path,
    image_ref_prefix: str,
    video_stem: str,
    frame_analysis_log: List[Dict[str, Any]],
    motion_score_threshold: float,
    motion_penalty: float,
) -> Dict[str, Any]:
    """Generate evidence composites from the best candidate below a gate.

    Even when no candidate passes the confidence gate, we still want to show
    the best available ROI in the report so users can see what was analysed
    and rejected. If ``candidates`` is empty, only the frame analysis log is
    returned.
    """
    raw_response: Dict[str, Any] = {
        "far_enhancement": {
            "frame_analysis_log": frame_analysis_log,
        }
    }
    if not candidates:
        return raw_response

    scored = _score_far_candidates(
        candidates,
        motion_score_threshold=motion_score_threshold,
        motion_penalty=motion_penalty,
    )
    best = scored[0]
    global_index = best["global_index"]
    adjacent_index = best["adjacent_index"]
    composite_filename = (
        f"{video_stem}_event_{category.event_id}_frame_{global_index}_composite.jpg"
    )
    composite_path = str(output_dir / composite_filename)
    composite_ref = f"{image_ref_prefix}/{composite_filename}"
    motion_composite_filename = (
        f"{video_stem}_event_{category.event_id}_frame_{global_index}_motion_{adjacent_index}.jpg"
    )
    motion_composite_path = str(output_dir / motion_composite_filename)
    motion_composite_ref = f"{image_ref_prefix}/{motion_composite_filename}"

    try:
        create_composite(
            best["frame"], best["bbox_norm"], output_path=composite_path
        )
        create_motion_comparison_composite(
            best["frame"],
            images[adjacent_index],
            best["bbox_norm"],
            scale=3.0,
            output_path=motion_composite_path,
        )
        raw_response["composite_image_path"] = composite_ref
        raw_response["motion_composite_image_path"] = motion_composite_ref
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] LOW_CONFIDENCE_EVIDENCE | "
            "event_id=%d frame=%d composite=%s motion=%s",
            category.event_id,
            global_index,
            composite_path,
            motion_composite_path,
        )
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_with_far_enhancement] LOW_CONFIDENCE_EVIDENCE_ERROR | "
            "event_id=%d frame=%d | %s",
            category.event_id,
            global_index,
            exc,
            exc_info=True,
        )
    return raw_response


def detect_per_frame(
    category: EventCategory,
    vlm_engine: VLMInferenceEngine,
    config_manager: ConfigManager,
    context: AnalysisContext,
    images: List[Any],
    template: PromptTemplate,
    context_vars: Dict[str, Any],
    roi_template: PromptTemplate,
    output_dir: Path,
    image_ref_prefix: str,
    video_stem: str,
    far_cfg: Any,
) -> Optional[EventCandidate]:
    """Run the generic per-frame far-distance enhancement flow.

    Two-pass design:
    1. Collect ROI candidates from every input frame. Valid ROIs must pass
       the configured minimum-area check and aspect-ratio filter.
    2. Score all candidates and keep the top ``far_object_enhancement.top_k``.
    3. For each top candidate generate the dual composites
       (single-frame + motion-comparison) and run the final classifier.
    4. Return the highest-scoring positive result. If none of the top-K
       candidates is positive, return detected=False. An optional fallback
       promotion is applied to the highest-scored candidate when it is safe
       (not occluded, high/medium confidence, not explicitly a car).

    If no valid candidate is found after all frames are exhausted, a
    detected=False EventCandidate is returned. Fatal API errors are re-raised.
    """
    min_area_px = far_cfg.min_area_px
    max_aspect_ratio = far_cfg.max_aspect_ratio
    enable_motion_filter = far_cfg.enable_motion_filter
    motion_score_threshold = far_cfg.motion_score_threshold
    motion_penalty = far_cfg.motion_penalty
    top_k = far_cfg.top_k

    # Per-frame ROI analysis log, attached to every EventCandidate produced by
    # this flow so the report can render a frame-by-frame ROI summary.
    frame_analysis_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # First pass: collect all valid ROI candidates and a per-frame log.
    # ------------------------------------------------------------------
    candidates: List[Dict[str, Any]] = []
    for global_index, frame in enumerate(images):
        frame_log: Dict[str, Any] = {
            "frame": global_index,
            "has_candidate": False,
            "bbox_norm": None,
            "area_px": None,
            "aspect_ratio": None,
            "confidence": None,
            "motion_score": None,
            "reason": "",
        }

        try:
            roi_response = vlm_engine.call(
                template=roi_template,
                images=[frame],
                context_vars=context_vars,
                response_schema=_ROI_DETECTION_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            _log_frame(
                frame_analysis_log,
                frame_log,
                reason=f"ROI detection failed: {exc}",
            )
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] ROI_CALL_ERROR | event_id=%d frame=%d | %s",
                category.event_id,
                global_index,
                exc,
            )
            continue

        if not roi_response.success or not isinstance(
            roi_response.parsed_data, dict
        ):
            _log_frame(
                frame_analysis_log,
                frame_log,
                reason="ROI response parsing failed",
            )
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] ROI_PARSE_ERROR | event_id=%d frame=%d success=%s",
                category.event_id,
                global_index,
                roi_response.success,
            )
            continue

        parsed = roi_response.parsed_data
        bbox_norm = parsed.get("bbox_norm")
        reason = parsed.get("reason", "")
        occluded = bool(parsed.get("occluded", False))
        confidence = parse_roi_confidence(parsed.get("confidence", 0.0))

        if bbox_norm is None:
            _log_frame(
                frame_analysis_log,
                frame_log,
                reason=reason or "ROI returned no candidate",
            )
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] NO_CANDIDATE | event_id=%d frame=%d reason=%s",
                category.event_id,
                global_index,
                reason,
            )
            continue

        if not isinstance(bbox_norm, list) or len(bbox_norm) != 4:
            _log_frame(
                frame_analysis_log,
                frame_log,
                reason=f"invalid bbox from ROI: {bbox_norm}",
            )
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] INVALID_BBOX | event_id=%d frame=%d bbox=%s",
                category.event_id,
                global_index,
                bbox_norm,
            )
            continue

        try:
            frame_pil = load_image(frame)
            img_width, img_height = frame_pil.size
            bbox_area = compute_bbox_area_px(bbox_norm, img_width, img_height)
            aspect_ratio = compute_bbox_aspect_ratio(bbox_norm)
            if not is_bbox_large_enough(
                bbox_norm, img_width, img_height, min_area_px=min_area_px
            ):
                _log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason=f"ROI candidate too small: area_px={bbox_area} < {min_area_px}",
                )
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] ROI_TOO_SMALL | event_id=%d frame=%d area_px=%d < %d",
                    category.event_id,
                    global_index,
                    bbox_area,
                    min_area_px,
                )
                continue
            if not is_bbox_aspect_valid(
                bbox_norm, max_ratio=max_aspect_ratio
            ):
                _log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason=f"ROI candidate aspect ratio rejected: {aspect_ratio:.2f}",
                )
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] ASPECT_REJECT | event_id=%d frame=%d bbox=%s ratio=%.2f",
                    category.event_id,
                    global_index,
                    bbox_norm,
                    aspect_ratio,
                )
                continue
        except Exception as exc:
            _log_frame(
                frame_analysis_log,
                frame_log,
                reason=f"ROI candidate size check failed: {exc}",
            )
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] SIZE_CHECK_ERROR | event_id=%d frame=%d | %s",
                category.event_id,
                global_index,
                exc,
            )
            continue

        # Compute adjacent-frame motion inside the enlarged ROI when
        # configured.  This is used to penalise static foreground objects
        # (camera brackets, poles, wires) that the VLM occasionally returns
        # as false ROIs.
        adjacent_index = (
            global_index - 1
            if global_index == len(images) - 1
            else global_index + 1
        )
        if enable_motion_filter:
            try:
                motion_score = compute_roi_motion_score(
                    frame,
                    images[adjacent_index],
                    bbox_norm,
                    scale=_FAR_MOTION_ENLARGE_SCALE,
                    gaussian_kernel=_FAR_MOTION_GAUSSIAN_KERNEL,
                    pixel_threshold=_FAR_MOTION_PIXEL_THRESHOLD,
                )
            except Exception as exc:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_SCORE_ERROR | event_id=%d frame=%d | %s",
                    category.event_id,
                    global_index,
                    exc,
                )
                motion_score = {
                    "mean_diff": 0.0,
                    "fraction_above_threshold": 0.0,
                    "motion_score": 0.0,
                }
        else:
            motion_score = {
                "mean_diff": 0.0,
                "fraction_above_threshold": 0.0,
                "motion_score": 0.0,
            }

        motion_score_value = motion_score.get("motion_score", 0.0)
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] FRAME_CANDIDATE | event_id=%d frame=%d area_px=%d aspect=%.2f confidence=%s motion_score=%.3f",
            category.event_id,
            global_index,
            bbox_area,
            aspect_ratio,
            confidence,
            motion_score_value,
        )

        frame_log.update(
            {
                "has_candidate": True,
                "bbox_norm": bbox_norm,
                "area_px": bbox_area,
                "aspect_ratio": aspect_ratio,
                "confidence": confidence,
                "motion_score": motion_score_value,
                "reason": reason,
            }
        )
        frame_analysis_log.append(frame_log)

        candidates.append(
            {
                "global_index": global_index,
                "frame": frame,
                "bbox_norm": bbox_norm,
                "area_px": bbox_area,
                "aspect_ratio": aspect_ratio,
                "occluded": occluded,
                "confidence": confidence,
                "reason": reason,
                "motion_score": motion_score,
                "adjacent_index": adjacent_index,
                "frame_analysis_log": frame_analysis_log,
            }
        )

    if not candidates:
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] NO_VALID_CANDIDATES | event_id=%d",
            category.event_id,
        )
        return EventCandidate(
            detected=False,
            event_id=category.event_id,
            event_name=category.name_zh,
            summary=f"未检测到{category.name_zh}。",
            raw_vlm_response={
                "far_enhancement": {
                    "frame_analysis_log": frame_analysis_log,
                }
            },
        )

    # ------------------------------------------------------------------
    # Confidence gate for pedestrians (event_id=4) and non-motor vehicles
    # (event_id=5): only ROIs with confidence >= 0.6 enter the final
    # classifier. This reduces false positives from distant, low-confidence
    # enhancements. When the gate drops every candidate, keep the best ROI
    # as evidence so the report shows what was analysed and rejected.
    # ------------------------------------------------------------------
    if category.event_id in (4, 5):
        total_candidates = len(candidates)
        gated_candidates = [c for c in candidates if c.get("confidence", 0.0) >= 0.6]
        if not gated_candidates:
            entity = "高速公路行人" if category.event_id == 4 else "非机动车"
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] LOW_CONFIDENCE_FILTER | "
                "event_id=%d kept=0 total=%d",
                category.event_id,
                total_candidates,
            )
            negative_raw_response = _generate_low_confidence_evidence(
                category,
                candidates,
                images,
                output_dir,
                image_ref_prefix,
                video_stem,
                frame_analysis_log,
                motion_score_threshold,
                motion_penalty,
            )
            return EventCandidate(
                detected=False,
                event_id=category.event_id,
                event_name=category.name_zh,
                summary=f"未检测到{entity}。所有远距离候选ROI置信度均低于0.6。",
                raw_vlm_response=negative_raw_response,
            )
        candidates = gated_candidates

    # ------------------------------------------------------------------
    # Rank candidates and keep the top K.
    # ------------------------------------------------------------------
    ranked_candidates = _score_far_candidates(
        candidates,
        motion_score_threshold=motion_score_threshold,
        motion_penalty=motion_penalty,
    )
    top_candidates = ranked_candidates[:top_k]
    logger.info(
        "[expert_agent:_detect_with_far_enhancement] TOP_CANDIDATES | event_id=%d total=%d selected=%d",
        category.event_id,
        len(ranked_candidates),
        len(top_candidates),
    )

    # ------------------------------------------------------------------
    # Second pass: classify the top-K candidates.
    # ------------------------------------------------------------------
    for candidate in top_candidates:
        global_index = candidate["global_index"]
        composite_filename = f"{video_stem}_event_{category.event_id}_frame_{global_index}_composite.jpg"
        composite_path = str(output_dir / composite_filename)
        composite_ref = f"{image_ref_prefix}/{composite_filename}"
        adjacent_index = candidate["adjacent_index"]
        motion_composite_filename = (
            f"{video_stem}_event_{category.event_id}_frame_{global_index}_motion_{adjacent_index}.jpg"
        )
        motion_composite_path = str(output_dir / motion_composite_filename)
        motion_composite_ref = (
            f"{image_ref_prefix}/{motion_composite_filename}"
        )

        logger.info(
            "[expert_agent:_detect_with_far_enhancement] DUAL_COMPOSITE | event_id=%d frame=%d adjacent=%d composite=%s motion=%s",
            category.event_id,
            global_index,
            adjacent_index,
            composite_path,
            motion_composite_path,
        )

        try:
            create_composite(
                candidate["frame"], candidate["bbox_norm"], output_path=composite_path
            )
            create_motion_comparison_composite(
                candidate["frame"],
                images[adjacent_index],
                candidate["bbox_norm"],
                scale=3.0,
                output_path=motion_composite_path,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement] COMPOSITE_ERROR | event_id=%d frame=%d | %s",
                category.event_id,
                global_index,
                exc,
                exc_info=True,
            )
            continue

        candidate.update(
            {
                "composite_path": composite_path,
                "motion_composite_path": motion_composite_path,
                "composite_ref": composite_ref,
                "motion_composite_ref": motion_composite_ref,
            }
        )

        final_candidate = _run_final_classifier(
            category, vlm_engine, candidate, template=template, context_vars=context_vars
        )
        if final_candidate is not None:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] COMPLETE | event_id=%d detected=True frame=%d composite=%s motion=%s",
                category.event_id,
                global_index,
                composite_path,
                motion_composite_path,
            )
            return final_candidate

        logger.info(
            "[expert_agent:_detect_with_far_enhancement] CLASSIFIER_NEGATIVE | event_id=%d frame=%d score=%.2f reason=%s",
            category.event_id,
            global_index,
            candidate.get("score", 0.0),
            candidate.get("negative_final_reason", ""),
        )

    # ------------------------------------------------------------------
    # Optional fallback on the highest-scored candidate.
    # Fallback is used for far-distance object categories (event_id=4
    # pedestrians and event_id=5 non-motor vehicles). When the final
    # classifier is over-conservative but the ROI detector produced a
    # high-confidence, unoccluded, well-shaped candidate, promote it to
    # detected=True so the expert raw output stays consistent with the
    # per-frame ROI evidence table.
    # ------------------------------------------------------------------
    if not top_candidates:
        # top_k <= 0 (or no candidates survived scoring): nothing to
        # classify, return the standard negative candidate.
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] NO_POSITIVE_CANDIDATES | event_id=%d",
            category.event_id,
        )
        return EventCandidate(
            detected=False,
            event_id=category.event_id,
            event_name=category.name_zh,
            summary=f"未检测到{category.name_zh}。",
            raw_vlm_response={
                "far_enhancement": {
                    "frame_analysis_log": frame_analysis_log,
                }
            },
        )
    best_candidate = top_candidates[0]
    if "composite_path" in best_candidate and category.event_id in (4, 5):
        fallback_candidate = _accept_fallback(
            category, best_candidate, frame_analysis_log
        )
        if fallback_candidate is not None:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_ACCEPT | event_id=%d frame=%d",
                category.event_id,
                best_candidate["global_index"],
            )
            return fallback_candidate

    logger.info(
        "[expert_agent:_detect_with_far_enhancement] NO_POSITIVE_CANDIDATES | event_id=%d",
        category.event_id,
    )
    # Preserve the best candidate's composite paths so the report can still
    # show what was analyzed and rejected.
    negative_raw_response: Dict[str, Any] = {
        "far_enhancement": {
            "frame_analysis_log": frame_analysis_log,
        }
    }
    if best_candidate.get("composite_ref"):
        negative_raw_response["composite_image_path"] = best_candidate["composite_ref"]
    if best_candidate.get("motion_composite_ref"):
        negative_raw_response["motion_composite_image_path"] = best_candidate["motion_composite_ref"]
    return EventCandidate(
        detected=False,
        event_id=category.event_id,
        event_name=category.name_zh,
        summary=f"未检测到{category.name_zh}。",
        raw_vlm_response=negative_raw_response,
        is_target_explicitly_four_wheel_vehicle=best_candidate.get(
            "is_target_explicitly_four_wheel_vehicle"
        ),
        target_type=str(best_candidate.get("target_type", "")),
    )
