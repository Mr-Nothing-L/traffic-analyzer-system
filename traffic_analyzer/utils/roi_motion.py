"""ROI motion scoring and adjacent-frame comparison composites.

[文件说明]
作用:``compute_roi_motion_score`` 在放大 ROI 内比较相邻帧灰度差,给出
    mean_diff/超阈像素占比/综合运动分;``create_motion_comparison_composite``
    生成同一 ROI 双帧对比拼图,辅助判定远距离目标是否在运动。
上游:utils/far_non_motor_enhancer.py 再导出后服务于
    core/expert_agent_far_enhancement.py(非机动目标运动判定)。
下游:utils/roi_composite.py、utils/bbox_geometry.py、utils/image_drawing.py;
    PIL/OpenCV/numpy。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .bbox_geometry import compute_enlarged_bbox, _norm_to_px
from .image_drawing import _draw_crosshair, load_image
from .roi_composite import _assemble_side_by_side, _load_crop, _resize_crop


_ZERO_MOTION_SCORE: Dict[str, float] = {
    "mean_diff": 0.0,
    "fraction_above_threshold": 0.0,
    "motion_score": 0.0,
}


def compute_roi_motion_score(
    frame: Union[np.ndarray, bytes, Image.Image],
    adjacent_frame: Union[np.ndarray, bytes, Image.Image],
    bbox_norm: List[float],
    scale: float = 3.0,
    gaussian_kernel: Optional[Tuple[int, int]] = (3, 3),
    pixel_threshold: float = 8.0,
) -> Dict[str, float]:
    """Compute a motion score for a ROI by comparing two adjacent frames.

    The comparison is performed inside an enlarged region around ``bbox_norm``
    so that small spatial shifts of distant targets are still captured.  No
    intermediate images are written to disk.

    Returns:
        Dict with scalar motion metrics:
            - ``mean_diff``: mean absolute grayscale difference in the ROI.
            - ``fraction_above_threshold``: fraction of ROI pixels whose
              absolute difference exceeds ``pixel_threshold``.
            - ``motion_score``: combined heuristic ``mean_diff`` +
              ``fraction_above_threshold * 100``.
    """
    current = load_image(frame)
    adjacent = load_image(adjacent_frame)

    if current.size != adjacent.size:
        adjacent = adjacent.resize(current.size, Image.LANCZOS)

    current_np = np.array(current)
    adjacent_np = np.array(adjacent)

    current_gray = cv2.cvtColor(current_np, cv2.COLOR_RGB2GRAY)
    adjacent_gray = cv2.cvtColor(adjacent_np, cv2.COLOR_RGB2GRAY)

    width, height = current.size
    enlarged_norm = compute_enlarged_bbox(bbox_norm, scale=scale)
    enlarged_px = _norm_to_px(enlarged_norm, width, height)
    x1, y1, x2, y2 = enlarged_px
    x1 = max(0, min(x1, width))
    y1 = max(0, min(y1, height))
    x2 = max(0, min(x2, width))
    y2 = max(0, min(y2, height))

    if x2 <= x1 or y2 <= y1:
        return _ZERO_MOTION_SCORE.copy()

    current_crop = current_gray[y1:y2, x1:x2]
    adjacent_crop = adjacent_gray[y1:y2, x1:x2]

    if (
        gaussian_kernel is not None
        and len(gaussian_kernel) == 2
        and gaussian_kernel[0] > 0
        and gaussian_kernel[1] > 0
    ):
        try:
            current_crop = cv2.GaussianBlur(current_crop, gaussian_kernel, 0)
            adjacent_crop = cv2.GaussianBlur(adjacent_crop, gaussian_kernel, 0)
        except cv2.error:
            pass

    diff = cv2.absdiff(current_crop, adjacent_crop)
    mean_diff = float(cv2.mean(diff)[0])

    total_pixels = int(diff.size)
    if total_pixels == 0:
        return _ZERO_MOTION_SCORE.copy()

    _, binary_diff = cv2.threshold(diff, pixel_threshold, 255, cv2.THRESH_BINARY)
    above = int(cv2.countNonZero(binary_diff))
    fraction = above / total_pixels
    motion_score = mean_diff + fraction * 100.0

    return {
        "mean_diff": mean_diff,
        "fraction_above_threshold": fraction,
        "motion_score": motion_score,
    }


def _draw_panel_annotations(
    panel: Image.Image,
    bbox_norm: List[float],
    enlarged_px: List[int],
    frame_width: int,
    frame_height: int,
) -> Image.Image:
    """Draw the original bbox and center crosshair on an enlarged ROI panel."""
    panel_w, panel_h = panel.size
    crop_w = enlarged_px[2] - enlarged_px[0]
    crop_h = enlarged_px[3] - enlarged_px[1]

    if crop_w <= 0 or crop_h <= 0:
        return panel

    scale_x = panel_w / crop_w
    scale_y = panel_h / crop_h

    orig_px = _norm_to_px(bbox_norm, frame_width, frame_height)
    rel_box = [
        orig_px[0] - enlarged_px[0],
        orig_px[1] - enlarged_px[1],
        orig_px[2] - enlarged_px[0],
        orig_px[3] - enlarged_px[1],
    ]
    draw_box = [
        int(round(rel_box[0] * scale_x)),
        int(round(rel_box[1] * scale_y)),
        int(round(rel_box[2] * scale_x)),
        int(round(rel_box[3] * scale_y)),
    ]

    annotated = panel.copy()
    draw = ImageDraw.Draw(annotated)
    draw.rectangle(draw_box, outline="#FF0000", width=3)

    cx = int(round(((orig_px[0] + orig_px[2]) / 2.0 - enlarged_px[0]) * scale_x))
    cy = int(round(((orig_px[1] + orig_px[3]) / 2.0 - enlarged_px[1]) * scale_y))
    cross_len = max(6, int(round(min(panel_w, panel_h) * 0.015)))
    _draw_crosshair(draw, cx, cy, cross_len, fill="#FFD700")

    return annotated


def create_motion_comparison_composite(
    frame,
    adjacent_frame,
    bbox_norm: List[float],
    scale: float = 2.0,
    output_path: Optional[str] = None,
) -> Image.Image:
    """Create a side-by-side comparison of the same ROI in two frames.

    Left panel: current-frame enlarged ROI with original bbox highlighted.
    Right panel: adjacent-frame enlarged ROI with original bbox highlighted.
    """
    current, current_crop, current_enlarged_px, (current_w, current_h) = _load_crop(
        frame, bbox_norm, scale=scale
    )
    adjacent, adjacent_crop, adjacent_enlarged_px, (adjacent_w, adjacent_h) = _load_crop(
        adjacent_frame, bbox_norm, scale=scale
    )

    target_height = min(current_h, max(current_h // 2, current_crop.size[1] * 4))
    current_panel = _resize_crop(current_crop, target_height=target_height)
    adjacent_panel = _resize_crop(adjacent_crop, target_height=target_height)

    left_panel = _draw_panel_annotations(
        current_panel,
        bbox_norm,
        current_enlarged_px,
        current_w,
        current_h,
    )
    right_panel = _draw_panel_annotations(
        adjacent_panel,
        bbox_norm,
        adjacent_enlarged_px,
        adjacent_w,
        adjacent_h,
    )

    composite = _assemble_side_by_side(left_panel, right_panel)

    if output_path is not None:
        composite.save(output_path)

    return composite
