"""Emergency lane occupancy detection evidence generation utilities.

This module produces the visual evidence images used in emergency lane
occupancy reports:

* ``02_masks_overlay.jpg`` – semi-transparent overlays for the emergency lane
  and chevron merge areas.
* ``03_vehicles_red_boxes.jpg`` – red bounding boxes and labels for detected
  vehicles.
* ``04_zoom_grid.jpg`` – zoomed-in grid of vehicle ROIs.
* ``zoom/V{id}_{label}_zoom4x.jpg`` – individual zoomed crops.

All drawing uses PIL / OpenCV and works without ``shapely``.

[文件说明]
作用:应急车道/导流区占用检测的可视化证据与结构化结果生成:掩膜叠加图、
    车辆红框标注图、ROI 放大网格与单车放大图(中文标签经 CJK 字体渲染),
    以及 bbox 与区域多边形的 overlap 计算 ``compute_roi_zone_overlap``
    和 summary.json 内容构建 ``build_occupancy_summary``。
上游:core/expert_agent_far_enhancement.py 的 ``_detect_emergency_lane_occupancy``。
下游:utils/bbox_geometry.py、utils/construction_evidence_gallery.py、
    utils/image_drawing.py;PIL/OpenCV;系统 CJK 字体(含 fc-list 回退探测)。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .bbox_geometry import compute_enlarged_bbox, _norm_to_px
from .construction_evidence_gallery import _compute_grid_layout
from .image_drawing import _draw_text_with_background, _load_scaled_font, load_image

logger = logging.getLogger(__name__)

JPEG_QUALITY: int = 95

_OVERLAY_ALPHA: float = 0.35
_EMERGENCY_COLOR: Tuple[int, int, int] = (0, 255, 0)      # green
_CHEVRON_COLOR: Tuple[int, int, int] = (255, 255, 0)      # yellow
_VEHICLE_BOX_COLOR: Tuple[int, int, int] = (255, 0, 0)    # red


def _polygon_to_abs_points(
    polygon_rel: List[List[float]],
    width: int,
    height: int,
) -> np.ndarray:
    """Convert a relative polygon to clipped absolute integer points."""
    if not polygon_rel or len(polygon_rel) < 3:
        return np.array([], dtype=np.int32)

    pts = np.array(
        [
            [int(round(x * width)), int(round(y * height))]
            for x, y in polygon_rel
        ],
        dtype=np.int32,
    )
    pts[:, 0] = np.clip(pts[:, 0], 0, width)
    pts[:, 1] = np.clip(pts[:, 1], 0, height)
    return pts


def _clip_px_box(
    px: List[int],
    width: int,
    height: int,
) -> Tuple[int, int, int, int]:
    """Clip a pixel bbox so it lies inside ``[0, width] x [0, height]``."""
    x1, y1, x2, y2 = px
    x1 = max(0, min(x1, width))
    x2 = max(0, min(x2, width))
    y1 = max(0, min(y1, height))
    y2 = max(0, min(y2, height))
    return x1, y1, x2, y2


def _resize_to_fill(
    crop: Image.Image,
    target_size: Tuple[int, int],
) -> Image.Image:
    """Resize ``crop`` so it completely covers ``target_size`` and center-crop.

    Uses bicubic resampling so that the emergency-lane zoom grid matches the
    requested ``scale`` quality.
    """
    target_w, target_h = target_size
    crop_w, crop_h = crop.size
    if crop_w <= 0 or crop_h <= 0:
        raise ValueError("Crop produced an empty image")

    scale = max(target_w / crop_w, target_h / crop_h)
    new_w = int(round(crop_w * scale))
    new_h = int(round(crop_h * scale))

    resample = Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
    resized = crop.resize((new_w, new_h), resample)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _sanitize_filename_part(text: str) -> str:
    """Keep letters, digits, underscores and CJK characters; fallback otherwise."""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]", "", str(text))
    return cleaned or "unknown"


def _load_cjk_font(size: int):
    """Load a system CJK font so Chinese vehicle labels render correctly.

    Tries a curated list of common Linux CJK fonts first, then falls back to
    ``fc-list :lang=zh`` to discover any available Chinese font. If nothing
    works, falls back to ``_load_scaled_font`` / PIL's default font and logs a
    warning.
    """
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/source-han/SourceHanSans-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue

    # Dynamic fallback: ask fontconfig for any font supporting Chinese.
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh", "-f", "%{file}\n"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                path = line.strip()
                if os.path.exists(path):
                    try:
                        return ImageFont.truetype(path, size=size)
                    except Exception:
                        continue
    except Exception:
        pass

    logger.warning(
        "No CJK font found on this system; Chinese labels may render as boxes. "
        "Falling back to default font."
    )
    return _load_scaled_font(size)


def generate_masks_overlay(
    frame: Union[np.ndarray, bytes, Image.Image],
    emergency_polygon_rel: Optional[List[List[float]]],
    chevron_polygon_rel: Optional[List[List[float]]],
    output_path: Optional[str] = None,
) -> Image.Image:
    """生成应急车道/导流区掩膜叠加图（02_masks_overlay.jpg）。

    应急车道用半透明绿色，导流区用半透明黄色。多边形为相对坐标
    ``[[x1,y1],[x2,y2],...]``。

    Returns:
        PIL ``Image.Image`` in RGB mode. If ``output_path`` is provided the
        image is also saved as a JPEG.
    """
    image = load_image(frame)
    width, height = image.size

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    alpha = int(round(255 * _OVERLAY_ALPHA))
    for polygon_rel, color in (
        (emergency_polygon_rel, _EMERGENCY_COLOR),
        (chevron_polygon_rel, _CHEVRON_COLOR),
    ):
        if not polygon_rel:
            continue
        pts = _polygon_to_abs_points(polygon_rel, width, height)
        if pts.size == 0:
            continue
        fill = color + (alpha,)
        draw.polygon([tuple(p) for p in pts], fill=fill)

    result = Image.alpha_composite(base, overlay).convert("RGB")

    if output_path is not None:
        try:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            result.save(output_path, "JPEG", quality=JPEG_QUALITY)
        except Exception:
            logger.exception("Failed to save masks overlay to %s", output_path)

    return result


def draw_vehicle_rois(
    frame: Union[np.ndarray, bytes, Image.Image],
    rois: List[Dict[str, Any]],
    output_path: Optional[str] = None,
) -> Image.Image:
    """生成红框标注的车辆图（03_vehicles_red_boxes.jpg）。

    ``rois`` 格式::

        [
            {
                "id": "V1",
                "label": "黄色工程车",
                "zone": "emergency_lane",
                "rel_box": [x1, y1, x2, y2],
            }
        ]

    用红色矩形框出 ROI，并在框上方标注
    ``"V1:黄色工程车 (emergency_lane)"``。
    """
    image = load_image(frame)
    width, height = image.size
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)

    font_size = max(12, min(24, height // 30))
    font = _load_cjk_font(font_size)

    for roi in rois:
        rel_box = roi.get("rel_box")
        if not rel_box or len(rel_box) != 4:
            logger.warning("Skipping ROI with invalid rel_box: %s", roi)
            continue

        x1, y1, x2, y2 = _clip_px_box(_norm_to_px(rel_box, width, height), width, height)
        if x2 <= x1 or y2 <= y1:
            continue

        draw.rectangle([x1, y1, x2, y2], outline=_VEHICLE_BOX_COLOR, width=3)

        label = (
            f"{roi.get('id', 'V?')}:"
            f"{roi.get('label', 'vehicle')} "
            f"({roi.get('zone', 'unknown')})"
        )
        text_y = max(0, y1 - font_size - 4)
        _draw_text_with_background(
            draw,
            label,
            (x1, text_y),
            fill=(255, 255, 255),
            background=_VEHICLE_BOX_COLOR,
            font=font,
        )

    if output_path is not None:
        try:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            annotated.save(output_path, "JPEG", quality=JPEG_QUALITY)
        except Exception:
            logger.exception("Failed to save vehicle ROI image to %s", output_path)

    return annotated


def create_zoom_grid(
    frame: Union[np.ndarray, bytes, Image.Image],
    rois: List[Dict[str, Any]],
    scale: int = 4,
    output_path: Optional[str] = None,
) -> Image.Image:
    """生成车辆 ROI 放大网格图（04_zoom_grid.jpg）。

    对每个 ROI 用 PIL ``Resampling.BICUBIC`` 放大 ``scale`` 倍，按网格排列
    （1/2/3/4 个分别采用合适布局，复用
    ``construction_evidence_gallery._compute_grid_layout``），每个格子标注
    ``id`` 和 ``label``。
    """
    image = load_image(frame)
    width, height = image.size
    n = len(rois)

    if n == 0:
        grid = Image.new("RGB", (width, height), color=(128, 128, 128))
        if output_path is not None:
            try:
                os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                grid.save(output_path, "JPEG", quality=JPEG_QUALITY)
            except Exception:
                logger.exception("Failed to save zoom grid to %s", output_path)
        return grid

    grid = Image.new("RGB", (width, height), color=(240, 240, 240))
    grid_draw = ImageDraw.Draw(grid)

    display_rois = rois[:4]
    positions = _compute_grid_layout(len(display_rois), width, height, gap=2)

    for idx, roi in enumerate(display_rois):
        rel_box = roi.get("rel_box")
        if not rel_box or len(rel_box) != 4:
            logger.warning("Skipping ROI with invalid rel_box in zoom grid: %s", roi)
            continue

        enlarged_norm = compute_enlarged_bbox(rel_box, scale=float(scale))
        enlarged_px = _clip_px_box(
            _norm_to_px(enlarged_norm, width, height), width, height
        )
        if enlarged_px[2] <= enlarged_px[0] or enlarged_px[3] <= enlarged_px[1]:
            continue

        crop = image.crop(enlarged_px)
        x, y, cell_w, cell_h = positions[idx]
        if cell_w <= 0 or cell_h <= 0:
            continue

        filled = _resize_to_fill(crop, (cell_w, cell_h))
        grid.paste(filled, (x, y))

        label = f"{roi.get('id', 'V?')}:{roi.get('label', 'vehicle')}"
        cell_font_size = max(10, min(28, cell_h // 12))
        font = _load_cjk_font(cell_font_size)
        _draw_text_with_background(
            grid_draw,
            label,
            (x + 4, y + 4),
            fill=(255, 255, 255),
            background=(0, 0, 0),
            font=font,
        )

    if output_path is not None:
        try:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            grid.save(output_path, "JPEG", quality=JPEG_QUALITY)
        except Exception:
            logger.exception("Failed to save zoom grid to %s", output_path)

    return grid


def create_single_zooms(
    frame: Union[np.ndarray, bytes, Image.Image],
    rois: List[Dict[str, Any]],
    scale: int = 4,
    output_dir: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """生成单个车辆放大图 ``zoom/V{id}_{label}_zoom4x.jpg``。

    Returns:
        ``[(roi_id, relative_path), ...]``. If ``output_dir`` is provided the
        images are written under ``output_dir/zoom/``.
    """
    image = load_image(frame)
    width, height = image.size

    results: List[Tuple[str, str]] = []
    zoom_dir = os.path.join(output_dir, "zoom") if output_dir else None
    if zoom_dir:
        os.makedirs(zoom_dir, exist_ok=True)

    for roi in rois:
        rel_box = roi.get("rel_box")
        if not rel_box or len(rel_box) != 4:
            logger.warning("Skipping ROI with invalid rel_box for single zoom: %s", roi)
            continue

        roi_id = str(roi.get("id", "V"))
        label = str(roi.get("label", "vehicle"))

        enlarged_norm = compute_enlarged_bbox(rel_box, scale=float(scale))
        enlarged_px = _clip_px_box(
            _norm_to_px(enlarged_norm, width, height), width, height
        )
        if enlarged_px[2] <= enlarged_px[0] or enlarged_px[3] <= enlarged_px[1]:
            continue

        crop = image.crop(enlarged_px)

        safe_id = _sanitize_filename_part(roi_id) or "V"
        safe_label = _sanitize_filename_part(label) or "vehicle"
        rel_path = f"zoom/{safe_id}_{safe_label}_zoom{scale}x.jpg"

        if zoom_dir:
            out_path = os.path.join(output_dir, rel_path)
            try:
                crop.save(out_path, "JPEG", quality=JPEG_QUALITY)
            except Exception:
                logger.exception("Failed to save single zoom to %s", out_path)

        results.append((roi_id, rel_path))

    return results


def compute_roi_zone_overlap(
    rel_box: List[float],
    zone_polygon_rel: List[List[float]],
    img_width: int,
    img_height: int,
) -> float:
    """计算车辆 bbox 与区域多边形的 overlap 比例（交集面积 / bbox 面积）。

    使用 OpenCV 的 ``fillPoly`` + ``bitwise_and`` 实现，不依赖 ``shapely``。
    返回 ``[0.0, 1.0]`` 的 float。
    """
    if not zone_polygon_rel or len(zone_polygon_rel) < 3:
        return 0.0

    x1, y1, x2, y2 = _clip_px_box(
        _norm_to_px(rel_box, img_width, img_height), img_width, img_height
    )
    if x2 <= x1 or y2 <= y1:
        return 0.0

    bbox_area = (x2 - x1) * (y2 - y1)
    if bbox_area <= 0:
        return 0.0

    mask_bbox = np.zeros((img_height, img_width), dtype=np.uint8)
    # cv2.rectangle 含端点,画到 (x2-1, y2-1) 使掩膜面积与 (x2-x1)*(y2-y1) 一致
    cv2.rectangle(mask_bbox, (x1, y1), (x2 - 1, y2 - 1), 1, thickness=-1)

    pts = _polygon_to_abs_points(zone_polygon_rel, img_width, img_height)
    if pts.size == 0:
        return 0.0

    mask_zone = np.zeros((img_height, img_width), dtype=np.uint8)
    cv2.fillPoly(mask_zone, [pts], 1)

    intersection = cv2.bitwise_and(mask_bbox, mask_zone)
    inter_area = float(cv2.countNonZero(intersection))
    overlap = inter_area / bbox_area
    return float(min(1.0, max(0.0, overlap)))


def build_occupancy_summary(
    video_stem: str,
    rois: List[Dict[str, Any]],
    overlaps: Dict[str, float],
) -> Dict[str, Any]:
    """构建 ``summary.json`` 的结构化内容。"""
    vehicles: List[Dict[str, Any]] = []
    occupied_count = 0
    zones: set = set()

    for roi in rois:
        roi_id = roi.get("id")
        overlap = float(overlaps.get(roi_id, 0.0))
        zone = roi.get("zone", "unknown")
        zones.add(zone)
        is_occupied = overlap > 0.5
        if is_occupied:
            occupied_count += 1

        vehicles.append(
            {
                "id": roi_id,
                "label": roi.get("label", "vehicle"),
                "zone": zone,
                "overlap": round(overlap, 4),
                "occupied": is_occupied,
            }
        )

    return {
        "video_stem": video_stem,
        "event_type": "emergency_lane_occupancy",
        "total_vehicles": len(rois),
        "occupied_count": occupied_count,
        "zones": sorted(zones),
        "summary_text": (
            f"在 {video_stem} 中检测到 {len(rois)} 辆车，"
            f"其中 {occupied_count} 辆占用应急车道/导流区"
        ),
        "vehicles": vehicles,
    }


# ---------------------------------------------------------------------------
# Simple self-test / usage example (run with ``python -m`` from repo root).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(level=logging.INFO)

    tmpdir = tempfile.mkdtemp(prefix="emergency_lane_test_")
    print(f"Emergency lane occupancy test outputs: {tmpdir}")

    # Synthetic 720p frame.
    frame = np.full((720, 1280, 3), (180, 180, 180), dtype=np.uint8)
    cv2.rectangle(frame, (0, 0), (1280, 720), (200, 200, 200), thickness=-1)

    emergency_polygon = [[0.65, 0.2], [0.95, 0.2], [0.95, 0.8], [0.65, 0.8]]
    chevron_polygon = [[0.45, 0.6], [0.65, 0.6], [0.55, 0.8]]

    generate_masks_overlay(
        frame,
        emergency_polygon_rel=emergency_polygon,
        chevron_polygon_rel=chevron_polygon,
        output_path=os.path.join(tmpdir, "02_masks_overlay.jpg"),
    )

    rois = [
        {
            "id": "V1",
            "label": "黄色工程车",
            "zone": "emergency_lane",
            "rel_box": [0.68, 0.35, 0.82, 0.55],
        },
        {
            "id": "V2",
            "label": "白色轿车",
            "zone": "chevron",
            "rel_box": [0.48, 0.62, 0.58, 0.72],
        },
    ]

    draw_vehicle_rois(
        frame,
        rois,
        output_path=os.path.join(tmpdir, "03_vehicles_red_boxes.jpg"),
    )

    create_zoom_grid(
        frame,
        rois,
        scale=4,
        output_path=os.path.join(tmpdir, "04_zoom_grid.jpg"),
    )

    single_paths = create_single_zooms(frame, rois, scale=4, output_dir=tmpdir)
    print("Single zoom paths:", single_paths)

    overlap_v1 = compute_roi_zone_overlap(
        rois[0]["rel_box"], emergency_polygon, 1280, 720
    )
    print(f"V1 vs emergency lane overlap: {overlap_v1:.4f}")

    summary = build_occupancy_summary(
        "test_video",
        rois,
        {"V1": overlap_v1, "V2": 0.0},
    )
    print("Summary:", summary)
