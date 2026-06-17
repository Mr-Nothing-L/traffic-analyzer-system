"""Far-distance non-motor vehicle ROI enlargement and composition utilities.

This module provides helpers to enlarge a small/normalized bounding box
around a distant non-motor vehicle and produce a side-by-side composite:
left = original frame with the enlarged ROI highlighted, right = magnified ROI.

Only PIL / numpy / cv2 are used; no ML detection models.
"""

from __future__ import annotations

import io
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw


def compute_enlarged_bbox(bbox_norm: List[float], scale: float = 2.0) -> List[float]:
    """Enlarge a normalized bbox around its center.

    Args:
        bbox_norm: Normalized bbox ``[x1, y1, x2, y2]`` in ``[0, 1]``.
        scale: Factor by which width and height are multiplied (default 2.0).

    Returns:
        Normalized enlarged bbox clipped to ``[0, 1]``.
    """
    x1, y1, x2, y2 = bbox_norm
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    w = x2 - x1
    h = y2 - y1

    new_w = w * scale
    new_h = h * scale

    nx1 = max(0.0, cx - new_w / 2.0)
    ny1 = max(0.0, cy - new_h / 2.0)
    nx2 = min(1.0, cx + new_w / 2.0)
    ny2 = min(1.0, cy + new_h / 2.0)

    return [nx1, ny1, nx2, ny2]


def compute_bbox_aspect_ratio(bbox_norm: List[float]) -> float:
    """Return width / height of a normalized bbox."""
    x1, y1, x2, y2 = bbox_norm
    h = y2 - y1
    if h <= 0:
        return float("inf")
    return (x2 - x1) / h


def is_bbox_aspect_valid_for_non_motor(
    bbox_norm: List[float], max_ratio: float = 1.0
) -> bool:
    """True if width/height is below ``max_ratio`` (non-motor vehicles are tall)."""
    return compute_bbox_aspect_ratio(bbox_norm) < max_ratio


def load_image(frame: Union[np.ndarray, bytes, Image.Image]) -> Image.Image:
    """Convert various image inputs to an RGB PIL.Image."""
    if isinstance(frame, np.ndarray):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)

    if isinstance(frame, bytes):
        return Image.open(io.BytesIO(frame)).convert("RGB")

    if isinstance(frame, Image.Image):
        if frame.mode != "RGB":
            return frame.convert("RGB")
        return frame

    raise TypeError(f"Unsupported frame type: {type(frame)}")


def _norm_to_px(bbox_norm: List[float], width: int, height: int) -> List[int]:
    """Convert normalized bbox to pixel coordinates."""
    x1, y1, x2, y2 = bbox_norm
    return [
        int(round(x1 * width)),
        int(round(y1 * height)),
        int(round(x2 * width)),
        int(round(y2 * height)),
    ]


def compute_bbox_area_px(bbox_norm: List[float], width: int, height: int) -> int:
    """Pixel area of a normalized bbox."""
    x1, y1, x2, y2 = _norm_to_px(bbox_norm, width, height)
    return max(0, x2 - x1) * max(0, y2 - y1)


def is_bbox_large_enough(
    bbox_norm: List[float],
    width: int,
    height: int,
    min_area_px: int = 80,
) -> bool:
    """True if the bbox pixel area is at least ``min_area_px``."""
    return compute_bbox_area_px(bbox_norm, width, height) >= min_area_px


def _draw_crosshair(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    cross_len: int,
    fill: str = "yellow",
    width: int = 2,
) -> None:
    """Draw a crosshair centered at (cx, cy)."""
    draw.line([(cx - cross_len, cy), (cx + cross_len, cy)], fill=fill, width=width)
    draw.line([(cx, cy - cross_len), (cx, cy + cross_len)], fill=fill, width=width)


def _load_crop(
    frame: Union[np.ndarray, bytes, Image.Image],
    bbox_norm: List[float],
    scale: float,
) -> Tuple[Image.Image, Image.Image, List[int], Tuple[int, int]]:
    """Load an image, compute the enlarged bbox, and crop it.

    Returns:
        (original_image, crop, enlarged_px, (width, height))
    """
    image = load_image(frame)
    width, height = image.size
    enlarged_norm = compute_enlarged_bbox(bbox_norm, scale=scale)
    enlarged_px = _norm_to_px(enlarged_norm, width, height)
    crop = image.crop(enlarged_px)
    return image, crop, enlarged_px, (width, height)


def _resize_crop(
    crop: Image.Image,
    target_height: int,
    max_width: Optional[int] = None,
    resample: Optional[int] = None,
) -> Image.Image:
    """Resize a crop to ``target_height`` while optionally capping width.

    Preserves aspect ratio. Chooses NEAREST for tiny crops unless overridden.
    """
    crop_w, crop_h = crop.size
    if crop_h <= 0 or crop_w <= 0:
        raise ValueError("Enlarged bbox produced an empty crop")

    scale_factor = target_height / crop_h
    target_width = int(round(crop_w * scale_factor))
    if max_width is not None and target_width > max_width:
        scale_factor = max_width / crop_w
        target_width = max_width
        target_height = int(round(crop_h * scale_factor))

    if resample is None:
        resample = Image.NEAREST if crop_h < 50 else Image.LANCZOS

    return crop.resize((target_width, target_height), resample)


def _assemble_side_by_side(
    left: Image.Image,
    right: Image.Image,
    separator_width: int = 3,
    background: Tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """Assemble two panels side-by-side with a vertical separator."""
    composite_width = left.width + separator_width + right.width
    composite_height = max(left.height, right.height)
    composite = Image.new("RGB", (composite_width, composite_height), color=background)
    composite.paste(left, (0, 0))
    y = (composite_height - right.height) // 2
    composite.paste(right, (left.width + separator_width, y))
    return composite


def create_composite(
    frame: Union[np.ndarray, bytes, Image.Image],
    bbox_norm: List[float],
    output_path: Optional[str] = None,
) -> Image.Image:
    """Create a side-by-side composite highlighting and magnifying a small ROI.

    Left panel: original frame with a red rectangle around the enlarged ROI
    and a yellow crosshair at the original bbox center.
    Right panel: magnified crop of the enlarged ROI.
    """
    original, crop, enlarged_px, (width, height) = _load_crop(frame, bbox_norm, scale=2.0)

    # Annotated original for the left panel.
    annotated = original.copy()
    draw = ImageDraw.Draw(annotated)
    draw.rectangle(enlarged_px, outline="red", width=3)

    cx = int(round((bbox_norm[0] + bbox_norm[2]) / 2.0 * width))
    cy = int(round((bbox_norm[1] + bbox_norm[3]) / 2.0 * height))
    cross_len = max(6, int(round(min(width, height) * 0.015)))
    _draw_crosshair(draw, cx, cy, cross_len)

    # Upscaled crop for the right panel.
    resized = _resize_crop(crop, target_height=height, max_width=width // 2)

    composite = _assemble_side_by_side(annotated, resized)

    if output_path is not None:
        composite.save(output_path)

    return composite


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

    cx = int(round((orig_px[0] + orig_px[2]) / 2.0 * scale_x))
    cy = int(round((orig_px[1] + orig_px[3]) / 2.0 * scale_y))
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
