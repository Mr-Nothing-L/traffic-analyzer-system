"""Emergency-lane-occupancy strategy for far-distance enhancement (event_id=2).

Extracted verbatim from ``FarEnhancementDetector._detect_emergency_lane_occupancy``
as part of the strategy decomposition (Task F3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.far_shared import _EXPERT_RESPONSE_SCHEMA
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    PromptTemplate,
)
from traffic_analyzer.utils.emergency_lane_occupancy import (
    build_occupancy_summary,
    compute_roi_zone_overlap,
    create_single_zooms,
    create_zoom_grid,
    draw_vehicle_rois,
    generate_masks_overlay,
)
from traffic_analyzer.utils.event_detection import parse_expert_response
from traffic_analyzer.utils.image_drawing import load_image
from traffic_analyzer.utils.progress import get_reporter as _get_progress_reporter

logger = logging.getLogger(__name__)


# JSON schema for the emergency lane / chevron polygon calibration used by event_id=2.
_EMERGENCY_LANE_CALIBRATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["emergency_polygon_rel", "chevron_polygon_rel"],
    "properties": {
        "emergency_polygon_rel": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                {"type": "null"},
            ]
        },
        "chevron_polygon_rel": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                {"type": "null"},
            ]
        },
        "summary": {"type": "string"},
    },
}

# JSON schema for the vehicle ROI detection inside calibrated emergency lane / chevron polygons.
_EMERGENCY_LANE_VEHICLE_ROI_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["rois", "summary"],
    "properties": {
        "rois": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "label", "zone", "rel_box"],
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "zone": {"type": "string"},
                    "reason": {"type": "string"},
                    "rel_box": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
            },
        },
        "summary": {"type": "string"},
    },
}


def detect_emergency_lane_occupancy(
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
    """Far-distance enhancement branch for emergency lane occupancy (event_id=2).

    1. Select the middle frame.
    2. Run the ``emergency_lane_calibration`` template to obtain the
       emergency lane / chevron polygons.
    3. Run the ``emergency_lane_vehicle_roi`` template to detect vehicles
       inside the calibrated polygons.
    4. Generate mask overlay, red-box vehicle annotation, zoom grid and
       per-vehicle zoom crops.
    5. Compute each ROI's overlap with its declared zone polygon.
    6. Run the final ``emergency_lane_occupancy_detection`` classifier on
       the annotated vehicle image and zoom grid.
    7. Return an EventCandidate that always includes the occupancy evidence
       paths, even when the classifier is negative.

    ``roi_template`` is kept in the signature for caller compatibility but
    the two helper templates are loaded explicitly by ID.
    """
    if not images:
        return None

    selected_index = len(images) // 2
    frame = images[selected_index]

    logger.info(
        "[expert_agent:_detect_emergency_lane_occupancy] START | event_id=%d event_name=%s frame=%d",
        category.event_id,
        category.name_zh,
        selected_index,
    )

    # --- Load the two helper templates explicitly ----------------------
    try:
        calibration_template = config_manager.get_prompt_template(
            "emergency_lane_calibration"
        )
        vehicle_roi_template = config_manager.get_prompt_template(
            "emergency_lane_vehicle_roi"
        )
    except (KeyError, RuntimeError) as exc:
        logger.warning(
            "[expert_agent:_detect_emergency_lane_occupancy] TEMPLATE_LOAD_ERROR | event_id=%d | %s",
            category.event_id,
            exc,
        )
        return None

    # --- Step A: polygon calibration on the middle frame ---------------
    try:
        calibration_response = vlm_engine.call(
            template=calibration_template,
            images=[frame],
            context_vars=context_vars,
            response_schema=_EMERGENCY_LANE_CALIBRATION_SCHEMA,
        )
    except FatalAPIError:
        raise
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_emergency_lane_occupancy] CALIBRATION_CALL_ERROR | event_id=%d frame=%d | %s",
            category.event_id,
            selected_index,
            exc,
            exc_info=True,
        )
        return None

    if not calibration_response.success or not isinstance(
        calibration_response.parsed_data, dict
    ):
        logger.warning(
            "[expert_agent:_detect_emergency_lane_occupancy] CALIBRATION_PARSE_ERROR | event_id=%d frame=%d",
            category.event_id,
            selected_index,
        )
        return None

    calibration_parsed = calibration_response.parsed_data
    emergency_polygon_rel = calibration_parsed.get("emergency_polygon_rel") or None
    chevron_polygon_rel = calibration_parsed.get("chevron_polygon_rel") or None
    calibration_summary = str(calibration_parsed.get("summary", ""))

    occupancy_detection: Dict[str, Any] = {
        "selected_frame_index": selected_index,
        "emergency_polygon_rel": emergency_polygon_rel,
        "chevron_polygon_rel": chevron_polygon_rel,
        "calibration_summary": calibration_summary,
        "rois": [],
        "calibration_reasoning": calibration_summary,
    }

    # --- Step B: early negative when no zone is calibrated -------------
    if not emergency_polygon_rel and not chevron_polygon_rel:
        logger.info(
            "[expert_agent:_detect_emergency_lane_occupancy] NO_ZONES | event_id=%d frame=%d",
            category.event_id,
            selected_index,
        )
        return EventCandidate(
            detected=False,
            event_id=category.event_id,
            event_name=category.name_zh,
            summary=f"未检测到{category.name_zh}。",
            raw_vlm_response={"occupancy_detection": occupancy_detection},
            raw_vlm_text=getattr(calibration_response, "raw_text", "") or "",
        )

    # --- Step C: vehicle ROI detection inside calibrated polygons ------
    roi_context_vars = {
        **context_vars,
        "emergency_polygon_rel": emergency_polygon_rel,
        "chevron_polygon_rel": chevron_polygon_rel,
    }
    try:
        roi_response = vlm_engine.call(
            template=vehicle_roi_template,
            images=[frame],
            context_vars=roi_context_vars,
            response_schema=_EMERGENCY_LANE_VEHICLE_ROI_SCHEMA,
        )
    except FatalAPIError:
        raise
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_emergency_lane_occupancy] VEHICLE_ROI_CALL_ERROR | event_id=%d frame=%d | %s",
            category.event_id,
            selected_index,
            exc,
            exc_info=True,
        )
        return None

    if not roi_response.success or not isinstance(roi_response.parsed_data, dict):
        logger.warning(
            "[expert_agent:_detect_emergency_lane_occupancy] VEHICLE_ROI_PARSE_ERROR | event_id=%d frame=%d",
            category.event_id,
            selected_index,
        )
        return None

    roi_parsed = roi_response.parsed_data
    rois = roi_parsed.get("rois", []) or []
    vehicle_roi_summary = str(roi_parsed.get("summary", ""))

    occupancy_detection["rois"] = rois
    occupancy_detection["vehicle_roi_summary"] = vehicle_roi_summary
    occupancy_detection["calibration_reasoning"] = (
        f"{calibration_summary}；{vehicle_roi_summary}".strip("；")
    )

    # --- No ROI: return negative candidate with calibration data -------
    if not rois:
        logger.info(
            "[expert_agent:_detect_emergency_lane_occupancy] NO_ROIS | event_id=%d frame=%d",
            category.event_id,
            selected_index,
        )
        return EventCandidate(
            detected=False,
            event_id=category.event_id,
            event_name=category.name_zh,
            summary=f"未检测到{category.name_zh}。",
            raw_vlm_response={"occupancy_detection": occupancy_detection},
            raw_vlm_text=getattr(roi_response, "raw_text", "") or "",
        )

    # --- Prepare output directory --------------------------------------
    occupancy_dir = output_dir / f"{video_stem}_event_1_occupancy"
    try:
        occupancy_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_emergency_lane_occupancy] OUTPUT_DIR_ERROR | event_id=%d path=%s | %s",
            category.event_id,
            occupancy_dir,
            exc,
            exc_info=True,
        )
        return None

    try:
        frame_pil = load_image(frame)
        img_width, img_height = frame_pil.size
    except Exception as exc:
        logger.warning(
            "[expert_agent:_detect_emergency_lane_occupancy] LOAD_FRAME_ERROR | event_id=%d | %s",
            category.event_id,
            exc,
        )
        return None

    # --- Generate visual evidence --------------------------------------
    masks_filename = "02_masks_overlay.jpg"
    vehicles_filename = "03_vehicles_red_boxes.jpg"
    zoom_grid_filename = "04_zoom_grid.jpg"

    masks_path = str(occupancy_dir / masks_filename)
    vehicles_path = str(occupancy_dir / vehicles_filename)
    zoom_grid_path = str(occupancy_dir / zoom_grid_filename)

    masks_ref = f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{masks_filename}"
    vehicles_ref = f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{vehicles_filename}"
    zoom_grid_ref = f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{zoom_grid_filename}"

    try:
        generate_masks_overlay(
            frame,
            emergency_polygon_rel=emergency_polygon_rel,
            chevron_polygon_rel=chevron_polygon_rel,
            output_path=masks_path,
        )
        draw_vehicle_rois(frame, rois, output_path=vehicles_path)
        create_zoom_grid(frame, rois, scale=4, output_path=zoom_grid_path)
        single_zoom_results = create_single_zooms(
            frame, rois, scale=4, output_dir=str(occupancy_dir)
        )
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_emergency_lane_occupancy] VISUAL_EVIDENCE_ERROR | event_id=%d | %s",
            category.event_id,
            exc,
            exc_info=True,
        )
        return None

    single_zoom_refs: List[tuple] = [
        (
            roi_id,
            f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{rel_path}",
        )
        for roi_id, rel_path in single_zoom_results
    ]

    logger.info(
        "[expert_agent:_detect_emergency_lane_occupancy] EVIDENCE_CREATED | event_id=%d rois=%d dir=%s",
        category.event_id,
        len(rois),
        occupancy_dir,
    )

    # --- Compute per-ROI zone overlap ----------------------------------
    vehicle_overlaps: Dict[str, float] = {}
    for roi in rois:
        roi_id = roi.get("id")
        zone = str(roi.get("zone", ""))
        rel_box = roi.get("rel_box")
        if not roi_id or not rel_box or len(rel_box) != 4:
            continue

        zone_polygon = None
        if zone == "emergency_lane":
            zone_polygon = emergency_polygon_rel
        elif zone == "chevron":
            zone_polygon = chevron_polygon_rel

        if not zone_polygon:
            vehicle_overlaps[roi_id] = 0.0
            continue

        try:
            overlap = compute_roi_zone_overlap(
                rel_box, zone_polygon, img_width, img_height
            )
            vehicle_overlaps[roi_id] = overlap
        except Exception as exc:
            logger.warning(
                "[expert_agent:_detect_emergency_lane_occupancy] OVERLAP_ERROR | event_id=%d roi=%s | %s",
                category.event_id,
                roi_id,
                exc,
            )
            vehicle_overlaps[roi_id] = 0.0

    occupancy_detection["vehicle_overlaps"] = vehicle_overlaps
    occupancy_detection["summary"] = build_occupancy_summary(
        video_stem, rois, vehicle_overlaps
    )

    # --- Final classifier on annotated vehicles + zoom grid ------------
    try:
        _get_progress_reporter().phase("reclassify")
        response = vlm_engine.call(
            template=template,
            images=[masks_path, vehicles_path, zoom_grid_path],
            context_vars=context_vars,
            response_schema=_EXPERT_RESPONSE_SCHEMA,
        )
    except FatalAPIError:
        raise
    except Exception as exc:
        logger.error(
            "[expert_agent:_detect_emergency_lane_occupancy] FINAL_CALL_ERROR | event_id=%d | %s",
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
                "mask_overlay_image_path": masks_ref,
                "vehicle_boxes_image_path": vehicles_ref,
                "zoom_grid_image_path": zoom_grid_ref,
                "single_zoom_image_paths": single_zoom_refs,
                "occupancy_detection": occupancy_detection,
            },
            raw_vlm_text="",
        )

    occupancy_detection["final_classifier_raw_text"] = getattr(
        response, "raw_text", ""
    )

    candidate = parse_expert_response(response, category)
    # Ensure the selected frame is recorded as evidence.
    if candidate.instances:
        for inst in candidate.instances:
            if selected_index not in (inst.evidence_frames or []):
                inst.evidence_frames = (inst.evidence_frames or []) + [selected_index]
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

    # Merge occupancy evidence into the candidate's raw response.
    candidate.raw_vlm_response["mask_overlay_image_path"] = masks_ref
    candidate.raw_vlm_response["vehicle_boxes_image_path"] = vehicles_ref
    candidate.raw_vlm_response["zoom_grid_image_path"] = zoom_grid_ref
    candidate.raw_vlm_response["single_zoom_image_paths"] = single_zoom_refs
    candidate.raw_vlm_response["occupancy_detection"] = occupancy_detection
    return candidate
