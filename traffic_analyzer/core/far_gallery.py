"""Multi-ROI gallery strategy for far-distance enhancement (middle-frame events).

Extracted verbatim from ``FarEnhancementDetector._detect_with_far_enhancement_gallery``
and its exclusive helpers as part of the strategy decomposition (Task F3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from traffic_analyzer.utils.construction_evidence_gallery import (
    create_multi_roi_gallery,
)
from traffic_analyzer.utils.event_detection import parse_expert_response
from traffic_analyzer.utils.image_drawing import load_image
from traffic_analyzer.utils.progress import get_reporter as _get_progress_reporter

# Re-exported so callers that used FarEnhancementDetector.apply_structured_veto_to_candidate
# can still import it from the far-enhancement module.
from traffic_analyzer.core.car_semantic_veto import apply_structured_veto_to_candidate

logger = logging.getLogger(__name__)


# JSON schema for the multi-evidence ROI detection used by event_id=7.
_MULTI_ROI_DETECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["evidence_regions"],
    "properties": {
        "evidence_regions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["bbox_norm", "tag", "confidence"],
                "properties": {
                    "bbox_norm": {
                        "type": "array",
                        "items": {"type": "number"},
                    },
                    "tag": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "on_ground": {"type": "boolean"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


def _has_construction_evidence(
    category: EventCategory, regions: List[Dict[str, Any]]
) -> bool:
    """Check if ROI evidence regions satisfy the construction work-zone definition.

    A valid construction scene requires at least one of the following:
    - at least one grounded cone plus at least one worker or vehicle;
    - at least three cones (continuous or grouped arrangement);
    - at least two barriers forming a clear lane closure;
    - at least one sign plus at least one worker or vehicle.

    Worker + vehicle alone is NOT sufficient; ground-based construction
    elements (cone, barrier, sign) must be present.

    Only regions with confidence >= 0.5 are counted.
    """
    tags = [
        str(r.get("tag", "")).lower()
        for r in regions
        if parse_roi_confidence(r.get("confidence", 0.0)) >= 0.5
    ]
    cone_count = tags.count("cone")
    worker_count = tags.count("worker")
    vehicle_count = tags.count("vehicle")
    barrier_count = tags.count("barrier")
    sign_count = tags.count("sign")

    if cone_count >= 1 and (worker_count + vehicle_count) >= 1:
        return True
    if cone_count >= 3:
        return True
    if barrier_count >= 2:
        return True
    if sign_count >= 1 and (worker_count + vehicle_count) >= 1:
        return True
    return False


def _build_construction_fallback_candidate(
    category: EventCategory,
    candidate: EventCandidate,
    display_regions: List[Dict[str, Any]],
    valid_regions: List[Dict[str, Any]],
    selected_index: int,
    gallery_ref: str,
    roi_summary: str,
) -> EventCandidate:
    """Promote a negative construction candidate when evidence clearly supports it."""
    tag_counts: Dict[str, int] = {}
    for region in valid_regions:
        tag = str(region.get("tag", "unknown")).lower()
        if parse_roi_confidence(region.get("confidence", 0.0)) >= 0.5:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    present_tags: List[str] = []
    if tag_counts.get("cone", 0) > 0:
        present_tags.append(f"锥桶×{tag_counts['cone']}")
    if tag_counts.get("worker", 0) > 0:
        present_tags.append(f"施工人员×{tag_counts['worker']}")
    if tag_counts.get("vehicle", 0) > 0:
        present_tags.append(f"施工车辆×{tag_counts['vehicle']}")
    if tag_counts.get("barrier", 0) > 0:
        present_tags.append(f"隔离栏/围挡×{tag_counts['barrier']}")
    if tag_counts.get("sign", 0) > 0:
        present_tags.append(f"施工标志牌×{tag_counts['sign']}")

    tags_str = "、".join(present_tags) if present_tags else "施工元素"
    summary = (
        f"检测到道路施工。证据合成图中出现 {tags_str} 等施工元素，"
        f"满足施工作业区定义。"
    )

    candidate.detected = True
    candidate.summary = summary
    candidate.instances = [
        EventInstance(
            event_id=category.event_id,
            event_name=category.name_zh,
            evidence_frames=[selected_index],
            description=summary,
            reasoning=summary,
        )
    ]

    candidate.raw_vlm_response["gallery_image_path"] = gallery_ref
    candidate.raw_vlm_response.setdefault("far_enhancement", {})
    candidate.raw_vlm_response["far_enhancement"].update(
        {
            "selected_frame_index": selected_index,
            "evidence_regions": display_regions,
            "summary": roi_summary,
            "fallback": True,
        }
    )
    return candidate


def _filter_grounded_construction_regions(
    category: EventCategory, regions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Remove cone regions that are not resting on the ground/road surface.

    Cones placed on vehicle roofs or in truck beds are not valid road-
    construction evidence. When the VLM does not provide ``on_ground``,
    fall back to a positional check: the bottom of the cone bbox should be
    in the lower half of the image (y2 > 0.5).
    """
    filtered: List[Dict[str, Any]] = []
    for region in regions:
        tag = str(region.get("tag", "")).lower()
        if tag != "cone":
            filtered.append(region)
            continue

        on_ground = region.get("on_ground")
        if on_ground is False:
            logger.info(
                "[expert_agent:_filter_grounded_construction_regions] CONE_NOT_ON_GROUND | "
                "event_id=%d on_ground=false",
                category.event_id,
            )
            continue

        if on_ground is None:
            bbox_norm = region.get("bbox_norm")
            if bbox_norm and len(bbox_norm) >= 4 and bbox_norm[3] <= 0.5:
                logger.info(
                    "[expert_agent:_filter_grounded_construction_regions] CONE_POSITION_REJECT | "
                    "event_id=%d y2=%.2f",
                    category.event_id,
                    bbox_norm[3],
                )
                continue

        filtered.append(region)
    return filtered


def detect_gallery(
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
    """Multi-ROI gallery branch for static, evidence-rich events (e.g. construction).

    1. Select the middle frame.
    2. Run the multi-ROI template to get evidence_regions.
    3. Filter ROIs by area/aspect and keep the top ``max_regions`` by confidence.
    4. Build a gallery composite (annotated original + zoom grid).
    5. Run the final classifier on the gallery.
    6. Return an EventCandidate that always includes the gallery path, even
       when the classifier is negative.
    """
    if not images:
        return None

    selected_index = len(images) // 2
    frame = images[selected_index]

    logger.info(
        "[expert_agent:_detect_with_far_enhancement_gallery] START | event_id=%d event_name=%s frame=%d",
        category.event_id,
        category.name_zh,
        selected_index,
    )

    # --- ROI detection on the middle frame ------------------------------
    try:
        roi_response = vlm_engine.call(
            template=roi_template,
            images=[frame],
            context_vars=context_vars,
            response_schema=_MULTI_ROI_DETECTION_SCHEMA,
        )
    except FatalAPIError:
        raise
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_with_far_enhancement_gallery] ROI_CALL_ERROR | event_id=%d frame=%d | %s",
            category.event_id,
            selected_index,
            exc,
            exc_info=True,
        )
        return None

    if not roi_response.success or not isinstance(roi_response.parsed_data, dict):
        logger.warning(
            "[expert_agent:_detect_with_far_enhancement_gallery] ROI_PARSE_ERROR | event_id=%d frame=%d",
            category.event_id,
            selected_index,
        )
        return None

    parsed = roi_response.parsed_data
    evidence_regions = parsed.get("evidence_regions", []) or []
    roi_summary = str(parsed.get("summary", ""))

    # --- Validate/filter regions ----------------------------------------
    min_area_px = far_cfg.min_area_px
    max_aspect_ratio = far_cfg.max_aspect_ratio
    valid_regions: List[Dict[str, Any]] = []
    try:
        frame_pil = load_image(frame)
        img_width, img_height = frame_pil.size
    except Exception as exc:
        logger.warning(
            "[expert_agent:_detect_with_far_enhancement_gallery] LOAD_FRAME_ERROR | event_id=%d | %s",
            category.event_id,
            exc,
        )
        return None

    for region in evidence_regions:
        bbox_norm = region.get("bbox_norm")
        tag = str(region.get("tag", "unknown"))
        confidence = parse_roi_confidence(region.get("confidence", 0.0))
        if not bbox_norm or len(bbox_norm) != 4:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement_gallery] INVALID_BBOX | event_id=%d bbox=%s",
                category.event_id,
                bbox_norm,
            )
            continue
        try:
            area_px = compute_bbox_area_px(bbox_norm, img_width, img_height)
            aspect_ratio = compute_bbox_aspect_ratio(bbox_norm)
            if not is_bbox_large_enough(
                bbox_norm, img_width, img_height, min_area_px=min_area_px
            ):
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement_gallery] ROI_TOO_SMALL | event_id=%d area_px=%d < %d",
                    category.event_id,
                    area_px,
                    min_area_px,
                )
                continue
            if not is_bbox_aspect_valid(bbox_norm, max_ratio=max_aspect_ratio):
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement_gallery] ASPECT_REJECT | event_id=%d ratio=%.2f",
                    category.event_id,
                    aspect_ratio,
                )
                continue
        except Exception as exc:
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement_gallery] SIZE_CHECK_ERROR | event_id=%d | %s",
                category.event_id,
                exc,
            )
            continue

        valid_regions.append(
            {
                "bbox_norm": bbox_norm,
                "tag": tag,
                "confidence": confidence,
                "area_px": area_px,
                "aspect_ratio": aspect_ratio,
                "on_ground": region.get("on_ground"),
            }
        )

    # Construction-specific: cones must be on the ground.
    valid_regions = _filter_grounded_construction_regions(category, valid_regions)

    if not valid_regions:
        logger.info(
            "[expert_agent:_detect_with_far_enhancement_gallery] NO_VALID_REGIONS | event_id=%d",
            category.event_id,
        )
        return EventCandidate(
            detected=False,
            event_id=category.event_id,
            event_name=category.name_zh,
            summary=f"未检测到{category.name_zh}。",
            raw_vlm_response={
                "far_enhancement": {
                    "selected_frame_index": selected_index,
                    "evidence_regions": [],
                    "summary": roi_summary,
                }
            },
        )

    # Keep highest-confidence regions for the gallery.
    valid_regions.sort(key=lambda r: r["confidence"], reverse=True)
    display_regions = valid_regions[:4]

    # --- Build gallery composite ----------------------------------------
    gallery_filename = f"{video_stem}_event_{category.event_id}_frame_{selected_index}_gallery.jpg"
    gallery_path = str(output_dir / gallery_filename)
    gallery_ref = f"{image_ref_prefix}/{gallery_filename}"

    try:
        create_multi_roi_gallery(
            frame, display_regions, output_path=gallery_path, max_regions=4
        )
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_with_far_enhancement_gallery] GALLERY_ERROR | event_id=%d | %s",
            category.event_id,
            exc,
            exc_info=True,
        )
        return None

    logger.info(
        "[expert_agent:_detect_with_far_enhancement_gallery] GALLERY_CREATED | event_id=%d path=%s regions=%d",
        category.event_id,
        gallery_path,
        len(display_regions),
    )

    # --- Final classifier on the gallery --------------------------------
    try:
        _get_progress_reporter().phase("reclassify")
        response = vlm_engine.call(
            template=template,
            images=[gallery_path],
            context_vars=context_vars,
            response_schema=_EXPERT_RESPONSE_SCHEMA,
        )
    except FatalAPIError:
        raise
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_with_far_enhancement_gallery] FINAL_CALL_ERROR | event_id=%d | %s",
            category.event_id,
            exc,
            exc_info=True,
        )
        return EventCandidate(
            detected=False,
            event_id=category.event_id,
            event_name=category.name_zh,
            summary=f"{category.name_zh}增强分类失败。",
            raw_vlm_response={
                "gallery_image_path": gallery_ref,
                "far_enhancement": {
                    "selected_frame_index": selected_index,
                    "evidence_regions": display_regions,
                    "summary": roi_summary,
                },
            },
        )

    candidate = parse_expert_response(response, category)
    # Ensure the selected frame is recorded as evidence.
    if candidate.instances:
        for inst in candidate.instances:
            if not inst.evidence_frames:
                inst.evidence_frames = [selected_index]
    else:
        candidate.instances = [
            EventInstance(
                event_id=category.event_id,
                event_name=category.name_zh,
                evidence_frames=[selected_index],
                description=candidate.summary,
                reasoning=candidate.summary,
            )
        ]

    # Apply structured car veto (and capture target_type) for the gallery
    # classifier, just like the per-frame far-enhancement path.
    candidate = apply_structured_veto_to_candidate(candidate)

    # ------------------------------------------------------------------
    # Construction fallback: if the final classifier rejected the scene
    # but the ROI evidence clearly satisfies the work-zone definition,
    # promote the candidate to detected=True. This keeps the expert
    # output consistent with the evidence table and gallery image.
    # ------------------------------------------------------------------
    if (
        category.event_id == 7
        and not candidate.detected
        and _has_construction_evidence(category, valid_regions)
    ):
        logger.info(
            "[expert_agent:_detect_with_far_enhancement_gallery] CONSTRUCTION_FALLBACK | "
            "event_id=%d frame=%d regions=%d",
            category.event_id,
            selected_index,
            len(valid_regions),
        )
        candidate = _build_construction_fallback_candidate(
            category=category,
            candidate=candidate,
            display_regions=display_regions,
            valid_regions=valid_regions,
            selected_index=selected_index,
            gallery_ref=gallery_ref,
            roi_summary=roi_summary,
        )

    # Merge the gallery metadata into the candidate's raw response.
    candidate.raw_vlm_response["gallery_image_path"] = gallery_ref
    candidate.raw_vlm_response.setdefault("far_enhancement", {})
    candidate.raw_vlm_response["far_enhancement"].update(
        {
            "selected_frame_index": selected_index,
            "evidence_regions": display_regions,
            "summary": roi_summary,
        }
    )
    return candidate
