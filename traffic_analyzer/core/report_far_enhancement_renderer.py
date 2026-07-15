"""Far-distance object enhancement renderer for traffic analysis reports.

Renders composite images, motion-reflection composites, per-frame ROI tables,
and construction-specific evidence tables produced by the far-enhancement
expert agents.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _render_far_enhancement(
    candidate: Optional[Dict[str, Any]],
    event_id: int,
) -> List[str]:
    """Render far-distance object enhancement evidence for an event."""
    if not candidate:
        return []

    if event_id == 4:
        title = "远距离非机动车增强证据"
        composite_alt = "远距离非机动车增强"
    elif event_id == 3:
        title = "远距离行人增强证据"
        composite_alt = "远距离行人增强"
    elif event_id == 6:
        title = "施工证据合成图"
        composite_alt = "施工证据合成图"
    elif event_id == 1:
        title = "应急车道占用增强证据"
        composite_alt = "应急车道占用增强证据"
    else:
        title = "远距离目标增强证据"
        composite_alt = "远距离目标增强"

    lines: List[str] = []
    raw_vlm_response = candidate.get("raw_vlm_response", {})
    composite_path = raw_vlm_response.get("composite_image_path")
    motion_composite_path = raw_vlm_response.get("motion_composite_image_path")
    gallery_path = raw_vlm_response.get("gallery_image_path")
    has_header = False

    if gallery_path and event_id == 6:
        lines.append(f"#### {title}")
        lines.append(f"**施工证据合成图**: `{gallery_path}`")
        lines.append("")
        lines.append(f"![{composite_alt}]({gallery_path})")
        lines.append("")
        has_header = True

    if composite_path:
        lines.append(f"#### {title}")
        lines.append(f"**远距离增强合成图**: `{composite_path}`")
        lines.append("")
        lines.append(f"![{composite_alt}]({composite_path})")
        lines.append("")
        has_header = True

    if motion_composite_path:
        if not has_header:
            lines.append(f"#### {title}")
            lines.append("")
        lines.append(f"**运动反射验证合成图**: `{motion_composite_path}`")
        lines.append("")
        lines.append(f"![运动反射验证]({motion_composite_path})")
        lines.append("")

    far_enhancement = raw_vlm_response.get("far_enhancement", {}) or {}

    # Construction evidence region table.
    if event_id == 6 and far_enhancement.get("evidence_regions"):
        lines.append("#### 证据区域表")
        lines.append("")
        lines.append("| tag | bbox | confidence | 面积(px) | 宽高比 | 说明 |")
        lines.append("|-----|------|------------|----------|--------|------|")
        for region in far_enhancement["evidence_regions"]:
            bbox_str = str(region.get("bbox_norm", "—"))
            confidence_val = region.get("confidence")
            confidence_str = f"{float(confidence_val):.2f}" if isinstance(confidence_val, (int, float)) else "—"
            area_str = str(region.get("area_px", "—"))
            aspect_val = region.get("aspect_ratio")
            aspect_str = f"{aspect_val:.2f}" if aspect_val is not None else "—"
            summary_str = str(far_enhancement.get("summary", ""))
            lines.append(
                f"| {region.get('tag', '—')} | {bbox_str} | {confidence_str} | {area_str} | {aspect_str} | {summary_str} |"
            )
        lines.append("")

    frame_analysis_log = far_enhancement.get("frame_analysis_log")
    if frame_analysis_log and event_id != 6:
        lines.append("#### 逐帧 ROI 分析")
        lines.append("")
        lines.append("| 帧号 | 是否有候选 | bbox | 面积(px) | 宽高比 | 置信度 | 运动分数 | 原因 |")
        lines.append("|------|------------|------|----------|--------|--------|----------|------|")
        for entry in frame_analysis_log:
            has_candidate = bool(entry.get("has_candidate", False))
            has_candidate_str = "是" if has_candidate else "否"
            if has_candidate:
                bbox_str = str(entry.get("bbox_norm", "—"))
                area_str = str(entry.get("area_px", "—"))
                aspect_val = entry.get("aspect_ratio")
                aspect_str = f"{aspect_val:.2f}" if aspect_val is not None else "—"
                confidence_val = entry.get("confidence")
                if confidence_val is None:
                    confidence_str = "—"
                elif isinstance(confidence_val, (int, float)):
                    confidence_str = f"{float(confidence_val):.2f}"
                else:
                    # Preserve legacy string values for backward compatibility.
                    confidence_str = str(confidence_val)
                motion_val = entry.get("motion_score")
                motion_str = f"{motion_val:.3f}" if motion_val is not None else "—"
            else:
                bbox_str = area_str = aspect_str = confidence_str = motion_str = "—"
            reason_str = str(entry.get("reason", ""))
            lines.append(
                f"| {entry.get('frame', '—')} | {has_candidate_str} | {bbox_str} | {area_str} | {aspect_str} | {confidence_str} | {motion_str} | {reason_str} |"
            )
        lines.append("")

    # Emergency lane occupancy enhancement evidence.
    if event_id == 1:
        occupancy = raw_vlm_response.get("occupancy_detection") or {}
        mask_overlay = raw_vlm_response.get("mask_overlay_image_path")
        vehicle_boxes = raw_vlm_response.get("vehicle_boxes_image_path")
        zoom_grid = raw_vlm_response.get("zoom_grid_image_path")
        single_zooms = raw_vlm_response.get("single_zoom_image_paths") or []
        calibration_reasoning = occupancy.get("calibration_reasoning")
        rois = occupancy.get("rois") or []
        has_any = (
            mask_overlay
            or vehicle_boxes
            or zoom_grid
            or rois
            or single_zooms
            or calibration_reasoning
        )
        if has_any:
            lines.append(f"#### {title}")
            lines.append("")

        if mask_overlay:
            lines.append(f"**应急车道/导流区掩膜叠加图**: `{mask_overlay}`")
            lines.append("")
            lines.append(f"![应急车道掩膜叠加]({mask_overlay})")
            lines.append("")

        if vehicle_boxes:
            lines.append(f"**车辆 ROI 标注图**: `{vehicle_boxes}`")
            lines.append("")
            lines.append(f"![车辆 ROI 标注]({vehicle_boxes})")
            lines.append("")

        if zoom_grid:
            lines.append(f"**车辆 ROI 放大网格图**: `{zoom_grid}`")
            lines.append("")
            lines.append(f"![车辆 ROI 放大网格]({zoom_grid})")
            lines.append("")

        if rois:
            vehicle_overlaps = occupancy.get("vehicle_overlaps") or {}
            lines.append("| 车辆ID | 标签 | 区域 | bbox | overlap | 标定理由 |")
            lines.append("|--------|------|------|------|---------|----------|")
            for roi in rois:
                roi_id = roi.get("id", "—")
                label = roi.get("label", "—")
                zone = roi.get("zone", "—")
                rel_box = str(roi.get("rel_box", "—"))
                overlap = vehicle_overlaps.get(roi_id)
                overlap_str = f"{float(overlap):.2f}" if isinstance(overlap, (int, float)) else "—"
                reason = roi.get("reason", "—")
                lines.append(
                    f"| {roi_id} | {label} | {zone} | {rel_box} | {overlap_str} | {reason} |"
                )
            lines.append("")

        if single_zooms:
            for vehicle_id, zoom_path in single_zooms:
                lines.append(f"![{vehicle_id} 放大图]({zoom_path})")
            lines.append("")

        if calibration_reasoning:
            lines.append("**复核理由**:")
            lines.append("")
            lines.append(str(calibration_reasoning))
            lines.append("")

    return lines
