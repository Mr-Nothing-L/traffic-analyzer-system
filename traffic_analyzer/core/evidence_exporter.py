"""
Evidence exporter for the traffic analyzer framework.

Optional post-adjudication helper (``sft_label`` mode). Assembles one editable
``evidence.json`` per video for the web UI: the adjudicated verdicts joined
with the far-enhancement coordinate data stored in the expert candidates'
``raw_vlm_response`` (calibration polygons, evidence regions, zoom/gallery
image refs). Referenced artifact images are copied into an ``images/``
subdirectory next to the JSON and referenced as ``images/<filename>`` so the
exported workspace is self-contained.

Fail-open by design: missing data is logged at WARNING and whatever is
available is written; the function never raises.

[文件说明]
作用:证据导出器(export_evidence),为每个视频生成可编辑的 evidence.json 并拷贝
     引用图像到 images/ 子目录,供 web UI 的 SFT 标注工作流使用;设计上永不抛异常。
上游:orchestrator/analysis_orchestrator.py(sft_label 模式下,裁决完成后调用)。
下游:读取 AnalysisContext 中专家候选的 raw_vlm_response(坐标与图像引用),
     写出 evidence.json 及 images/ 图像文件;依赖 models/schemas.py 的 AnalysisContext。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.models.schemas import AnalysisContext

logger = logging.getLogger(__name__)

_EVIDENCE_SCHEMA_VERSION = 1

# Raw-response keys holding single artifact image refs.
_SINGLE_IMAGE_KEYS = (
    "composite_image_path",
    "motion_composite_image_path",
    "gallery_image_path",
    "mask_overlay_image_path",
    "vehicle_boxes_image_path",
    "zoom_grid_image_path",
)


def _is_box(value: Any) -> bool:
    """True for a normalized ``[x1, y1, x2, y2]`` box."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(v, (int, float)) for v in value)
    )


def _single_zoom_map(raw: Dict[str, Any]) -> Dict[str, str]:
    """Map ROI id -> zoom image ref from ``single_zoom_image_paths``.

    Entries are stored as ``(roi_id, ref)`` tuples; tolerate lists so the
    exporter also works on raw responses that went through a JSON round-trip.
    """
    zoom_map: Dict[str, str] = {}
    for item in raw.get("single_zoom_image_paths") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], str):
            zoom_map[str(item[0])] = item[1]
    return zoom_map


def _raw_response_for(
    context: AnalysisContext, event_id: int, event_result: Any
) -> Dict[str, Any]:
    """Best-effort lookup of the raw VLM response holding coordinate data.

    Coordinate data is produced by the expert agents, so the primary source is
    the event candidate; an attribute on the event result itself wins when a
    future pipeline attaches it there.
    """
    raw = getattr(event_result, "raw_vlm_response", None)
    if isinstance(raw, dict) and raw:
        return raw
    candidate = (context.event_candidates or {}).get(event_id)
    raw = getattr(candidate, "raw_vlm_response", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _build_event_entry(event_result: Any, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Build one ``events[]`` entry from an adjudicated result + raw response.

    Image fields still hold the raw refs here; they are rewritten to
    ``images/<filename>`` once the files have been copied.
    """
    calibration: Dict[str, Any] = {
        "frame_index": None,
        "emergency_polygon_rel": None,
        "chevron_polygon_rel": None,
    }
    evidence_regions: List[Dict[str, Any]] = []
    gallery_refs: List[str] = []

    # --- Emergency lane occupancy branch (event_id=1): calibration polygons,
    # per-vehicle ROI boxes with single zooms, and overlay images. ----------
    occupancy = raw.get("occupancy_detection") or {}
    if occupancy:
        frame_index = occupancy.get("selected_frame_index")
        calibration = {
            "frame_index": frame_index,
            "emergency_polygon_rel": occupancy.get("emergency_polygon_rel") or None,
            "chevron_polygon_rel": occupancy.get("chevron_polygon_rel") or None,
        }
        zoom_map = _single_zoom_map(raw)
        for roi in occupancy.get("rois") or []:
            rel_box = roi.get("rel_box")
            if not _is_box(rel_box):
                continue
            evidence_regions.append(
                {
                    "frame_index": frame_index,
                    "box_rel": [float(v) for v in rel_box],
                    "label": str(roi.get("label", "")),
                    "image": zoom_map.get(str(roi.get("id"))),
                }
            )
        for key in (
            "mask_overlay_image_path",
            "vehicle_boxes_image_path",
            "zoom_grid_image_path",
        ):
            if raw.get(key):
                gallery_refs.append(raw[key])

    # --- Far-enhancement branches: construction gallery regions and the
    # generic single-ROI box (pedestrian / non-motor vehicle). --------------
    far_enhancement = raw.get("far_enhancement") or {}
    if far_enhancement:
        frame_index = far_enhancement.get("selected_frame_index")
        gallery_ref = raw.get("gallery_image_path")
        for region in far_enhancement.get("evidence_regions") or []:
            bbox = region.get("bbox_norm")
            if not _is_box(bbox):
                continue
            evidence_regions.append(
                {
                    "frame_index": frame_index,
                    "box_rel": [float(v) for v in bbox],
                    "label": str(region.get("tag", "")),
                    "image": gallery_ref,
                }
            )
        bbox = far_enhancement.get("bbox_norm")
        if _is_box(bbox):
            evidence_regions.append(
                {
                    "frame_index": frame_index,
                    "box_rel": [float(v) for v in bbox],
                    "label": str(far_enhancement.get("reason", "")),
                    "image": raw.get("composite_image_path"),
                }
            )
        for key in (
            "gallery_image_path",
            "composite_image_path",
            "motion_composite_image_path",
        ):
            if raw.get(key):
                gallery_refs.append(raw[key])

    return {
        "event_id": event_result.event_id,
        "name": event_result.event_name,
        "detected": bool(event_result.detected),
        "calibration": calibration,
        "evidence_regions": evidence_regions,
        "gallery_images": gallery_refs,
    }


def _iter_event_refs(event: Dict[str, Any]) -> List[str]:
    """All distinct image refs of one event entry, in stable order."""
    refs: List[str] = []
    for ref in event["gallery_images"]:
        if ref and ref not in refs:
            refs.append(ref)
    for region in event["evidence_regions"]:
        ref = region.get("image")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _resolve_image_source(ref: str, context: AnalysisContext) -> Optional[Path]:
    """Resolve a stored image ref to an existing file on disk.

    Refs are either relative to the report output dir (``tmp_img/...``),
    relative to the project root (fallback ``output/tmp_img/...``), or
    absolute (tests / patched output dirs).
    """
    ref_path = Path(ref)
    candidates: List[Path] = []
    if ref_path.is_absolute():
        candidates.append(ref_path)
    else:
        output_dir = getattr(context, "output_dir", None)
        if output_dir:
            candidates.append(Path(output_dir) / ref_path)
        candidates.append(ref_path)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _copy_image(
    ref: str,
    context: AnalysisContext,
    images_dir: Path,
    video_stem: str,
) -> Optional[str]:
    """Copy one referenced artifact into ``images_dir``.

    Returns the workspace-relative ``images/<filename>`` reference, or None
    when the source is missing or cannot be copied.
    """
    source = _resolve_image_source(ref, context)
    if source is None:
        logger.warning("[evidence_exporter] IMAGE_MISSING | ref=%s", ref)
        return None
    # Composite/gallery names already embed the video stem; scope the generic
    # occupancy names (e.g. ``02_masks_overlay.jpg``) so several videos can
    # share one images/ directory without overwriting each other.
    dest_name = (
        source.name if video_stem in source.name else f"{video_stem}__{source.name}"
    )
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, images_dir / dest_name)
    except Exception as exc:
        logger.warning(
            "[evidence_exporter] IMAGE_COPY_ERROR | ref=%s | %s", ref, exc
        )
        return None
    return f"images/{dest_name}"


def export_evidence(context: AnalysisContext, out_dir: Path) -> Optional[Path]:
    """Write ``<out_dir>/<video_stem>_evidence.json`` for the web UI.

    Assembles adjudicated verdicts plus far-enhancement coordinate data and
    copies the referenced artifact images into ``<out_dir>/images/``.

    Fail-open: missing data is logged at WARNING and whatever is available is
    written. Returns the written path, or None when there is literally
    nothing to export or the write itself fails.
    """
    out_dir = Path(out_dir)
    video_meta = context.video_meta
    event_results = context.event_results or {}

    if video_meta is None and not event_results:
        logger.warning(
            "[evidence_exporter] SKIP | no video_meta and no event_results"
        )
        return None

    if video_meta is None:
        logger.warning(
            "[evidence_exporter] NO_VIDEO_META | video block left empty"
        )
        video_stem = "unknown_video"
        video_block: Dict[str, Any] = {
            "file_name": None,
            "duration_sec": None,
            "fps": None,
            "width": None,
            "height": None,
        }
    else:
        video_stem = Path(video_meta.file_path).stem or "unknown_video"
        video_block = {
            "file_name": video_meta.file_name,
            "duration_sec": video_meta.duration_sec,
            "fps": video_meta.fps,
            "width": video_meta.width,
            "height": video_meta.height,
        }

    events: List[Dict[str, Any]] = []
    for event_id in sorted(event_results):
        event_result = event_results[event_id]
        raw = _raw_response_for(context, event_id, event_result)
        events.append(_build_event_entry(event_result, raw))

    # Copy referenced images once, then rewrite refs to images/<filename>.
    images_dir = out_dir / "images"
    copied: Dict[str, Optional[str]] = {}
    for event in events:
        for ref in _iter_event_refs(event):
            if ref not in copied:
                copied[ref] = _copy_image(ref, context, images_dir, video_stem)
    for event in events:
        event["gallery_images"] = [
            copied[ref] for ref in event["gallery_images"] if copied.get(ref)
        ]
        for region in event["evidence_regions"]:
            ref = region.get("image")
            region["image"] = copied.get(ref) if ref else None

    payload = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "video": video_block,
        "events": events,
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"{video_stem}_evidence.json"
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(
            "[evidence_exporter] WRITE_ERROR | dir=%s | %s",
            out_dir,
            exc,
            exc_info=True,
        )
        return None

    logger.info(
        "[evidence_exporter] EVIDENCE_WRITTEN | path=%s events=%d",
        file_path,
        len(events),
    )
    return file_path
