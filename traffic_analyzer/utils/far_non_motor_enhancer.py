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


# Confidence ordering from highest to lowest.
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


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
    """计算归一化 bbox 的宽高比 width / height。

    Args:
        bbox_norm: [x1, y1, x2, y2]，取值范围 [0.0, 1.0]

    Returns:
        宽高比，若高度为 0 则返回 float('inf')
    """
    x1, y1, x2, y2 = bbox_norm
    h = y2 - y1
    if h <= 0:
        return float("inf")
    return (x2 - x1) / h


def is_bbox_aspect_valid_for_non_motor(
    bbox_norm: List[float], max_ratio: float = 1.0
) -> bool:
    """判断 bbox 宽高比是否符合非机动车先验（高度 > 宽度，即 ratio < 1.0）。

    Args:
        bbox_norm: [x1, y1, x2, y2]
        max_ratio: 允许的最大宽高比，默认 1.0

    Returns:
        True 如果 width/height < max_ratio，否则 False
    """
    return compute_bbox_aspect_ratio(bbox_norm) < max_ratio


def load_image(frame: Union[np.ndarray, bytes, Image.Image]) -> Image.Image:
    """Convert various image inputs to an RGB PIL.Image.

    Args:
        frame: OpenCV BGR ``np.ndarray``, raw ``bytes``, or ``PIL.Image``.

    Returns:
        RGB ``PIL.Image.Image``.
    """
    if isinstance(frame, np.ndarray):
        # OpenCV BGR -> RGB
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
    """Compute the pixel area of a normalized bbox.

    Args:
        bbox_norm: Normalized bbox ``[x1, y1, x2, y2]``.
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        Bounding box area in pixels.
    """
    x1, y1, x2, y2 = _norm_to_px(bbox_norm, width, height)
    return max(0, x2 - x1) * max(0, y2 - y1)


def is_bbox_large_enough(
    bbox_norm: List[float],
    width: int,
    height: int,
    min_area_px: int = 80,
) -> bool:
    """Check whether a normalized bbox meets the minimum pixel area threshold.

    This filters out tiny noise points (e.g., a single pixel or small glare spot)
    that the VLM may hallucinate as a distant vehicle.

    Args:
        bbox_norm: Normalized bbox ``[x1, y1, x2, y2]``.
        width: Image width in pixels.
        height: Image height in pixels.
        min_area_px: Minimum acceptable area in pixels (default 80).

    Returns:
        True if the bbox area is at least ``min_area_px``.
    """
    return compute_bbox_area_px(bbox_norm, width, height) >= min_area_px


def create_composite(
    frame: Union[np.ndarray, bytes, Image.Image],
    bbox_norm: List[float],
    output_path: Optional[str] = None,
) -> Image.Image:
    """Create a side-by-side composite highlighting and magnifying a small ROI.

    Processing steps:
      1. Compute ``enlarged_bbox`` with scale ``2.0``.
      2. Draw a red rectangle (3 px) on the original frame over ``enlarged_bbox``.
      3. Mark the original bbox center with a yellow crosshair.
      4. Crop ``enlarged_bbox`` from the *unmarked* original frame.
      5. Upscale the crop so its height approaches the original frame height,
         preserving aspect ratio. Use NEAREST if the crop height < 50 px,
         otherwise LANCZOS. The upscaled width is capped at half the original
         frame width.
      6. Compose: left = marked original, right = upscaled crop, separated by
         a 3 px white vertical line centered vertically.

    Args:
        frame: Input image as OpenCV BGR ``np.ndarray``, ``bytes``, or PIL image.
        bbox_norm: Normalized bbox ``[x1, y1, x2, y2]`` of the target object.
        output_path: Optional path to save the resulting composite.

    Returns:
        The composite ``PIL.Image.Image`` in RGB mode.
    """
    original = load_image(frame)
    width, height = original.size

    enlarged_norm = compute_enlarged_bbox(bbox_norm, scale=2.0)
    enlarged_px = _norm_to_px(enlarged_norm, width, height)

    # Annotated copy for the left panel.
    annotated = original.copy()
    draw = ImageDraw.Draw(annotated)

    # Red rectangle around the enlarged ROI.
    draw.rectangle(enlarged_px, outline="red", width=3)

    # Yellow crosshair at the original bbox center.
    cx = int(round((bbox_norm[0] + bbox_norm[2]) / 2.0 * width))
    cy = int(round((bbox_norm[1] + bbox_norm[3]) / 2.0 * height))
    cross_len = max(6, int(round(min(width, height) * 0.015)))
    draw.line([(cx - cross_len, cy), (cx + cross_len, cy)], fill="yellow", width=2)
    draw.line([(cx, cy - cross_len), (cx, cy + cross_len)], fill="yellow", width=2)

    # Crop from the unmarked original image.
    crop = original.crop(enlarged_px)
    crop_w, crop_h = crop.size

    if crop_h <= 0 or crop_w <= 0:
        raise ValueError("Enlarged bbox produced an empty crop")

    # Upscale crop so its height is close to the original frame height.
    target_height = height
    scale_factor = target_height / crop_h
    target_width = int(round(crop_w * scale_factor))

    # Cap width at half the original frame width, preserving aspect ratio.
    max_width = width // 2
    if target_width > max_width:
        scale_factor = max_width / crop_w
        target_width = max_width
        target_height = int(round(crop_h * scale_factor))

    if crop_h < 50:
        resized = crop.resize((target_width, target_height), Image.NEAREST)
    else:
        resized = crop.resize((target_width, target_height), Image.LANCZOS)

    # Build composite: left (annotated original) + separator + right (zoomed).
    separator_width = 3
    composite_width = width + separator_width + target_width
    composite_height = height
    composite = Image.new("RGB", (composite_width, composite_height), color=(255, 255, 255))

    composite.paste(annotated, (0, 0))

    # Vertical separator is already white background; just paste zoom panel.
    zoom_x = width + separator_width
    zoom_y = (composite_height - target_height) // 2
    composite.paste(resized, (zoom_x, zoom_y))

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
    """Draw the original bbox and center crosshair on an enlarged ROI panel.

    Args:
        panel: The enlarged ROI panel.
        bbox_norm: Original normalized bbox ``[x1, y1, x2, y2]``.
        enlarged_px: Pixel coordinates ``[x1, y1, x2, y2]`` of the enlarged ROI
            in the original frame.
        frame_width: Width of the original frame.
        frame_height: Height of the original frame.

    Returns:
        The annotated panel.
    """
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
    draw.line(
        [(cx - cross_len, cy), (cx + cross_len, cy)],
        fill="#FFD700",
        width=2,
    )
    draw.line(
        [(cx, cy - cross_len), (cx, cy + cross_len)],
        fill="#FFD700",
        width=2,
    )

    return annotated


def create_motion_comparison_composite(
    frame,
    adjacent_frame,
    bbox_norm: List[float],
    scale: float = 2.0,
    output_path: Optional[str] = None,
) -> Image.Image:
    """Create a side-by-side comparison of the same ROI in two frames.

    Processing steps:
      1. Compute ``enlarged_bbox`` for each frame using ``scale``.
      2. Crop the enlarged ROI from both frames.
      3. Upscale the current-frame crop so its height approaches the original
         frame height (at least half the frame height or 4x the crop height,
         capped at the frame height), preserving aspect ratio.
      4. Resize the adjacent-frame crop to the same panel size.
      5. Draw a red rectangle (3 px) around the original bbox and a yellow
         crosshair at its center on each panel.
      6. Compose: left = current frame panel, right = adjacent frame panel,
         separated by a 3 px white vertical line.

    Args:
        frame: Current-frame image as OpenCV BGR ``np.ndarray``, ``bytes``,
            or PIL image.
        adjacent_frame: Adjacent-frame image in the same accepted formats.
        bbox_norm: Normalized bbox ``[x1, y1, x2, y2]`` of the target object.
        scale: Factor by which width and height are multiplied (default 2.0).
        output_path: Optional path to save the resulting composite.

    Returns:
        The composite ``PIL.Image.Image`` in RGB mode.
    """
    current = load_image(frame)
    adjacent = load_image(adjacent_frame)

    current_w, current_h = current.size
    adjacent_w, adjacent_h = adjacent.size

    current_enlarged_norm = compute_enlarged_bbox(bbox_norm, scale=scale)
    adjacent_enlarged_norm = compute_enlarged_bbox(bbox_norm, scale=scale)

    current_enlarged_px = _norm_to_px(current_enlarged_norm, current_w, current_h)
    adjacent_enlarged_px = _norm_to_px(adjacent_enlarged_norm, adjacent_w, adjacent_h)

    current_crop = current.crop(current_enlarged_px)
    adjacent_crop = adjacent.crop(adjacent_enlarged_px)

    if current_crop.size[0] <= 0 or current_crop.size[1] <= 0:
        raise ValueError("Enlarged bbox produced an empty current-frame crop")

    crop_w, crop_h = current_crop.size

    # Upscale so the panel is clearly visible but not larger than the frame.
    target_height = min(current_h, max(current_h // 2, crop_h * 4))
    target_width = int(round(crop_w * target_height / crop_h))

    resample = Image.NEAREST if crop_h < 50 else Image.LANCZOS

    panel_size = (target_width, target_height)
    current_panel = current_crop.resize(panel_size, resample)
    adjacent_panel = adjacent_crop.resize(panel_size, resample)

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

    separator_width = 3
    composite_width = target_width * 2 + separator_width
    composite_height = target_height
    composite = Image.new(
        "RGB",
        (composite_width, composite_height),
        color=(255, 255, 255),
    )

    composite.paste(left_panel, (0, 0))
    composite.paste(right_panel, (target_width + separator_width, 0))

    if output_path is not None:
        composite.save(output_path)

    return composite


def select_best_candidate(candidates: List[Dict]) -> Optional[Dict]:
    """Select the candidate with the highest confidence.

    Args:
        candidates: List of candidate dicts, each containing at least
            ``confidence`` (``"high"``, ``"medium"``, or ``"low"``) and
            ``bbox_norm``.

    Returns:
        The highest-confidence candidate, or ``None`` if the list is empty.
    """
    if not candidates:
        return None

    def _rank(candidate: Dict) -> int:
        conf = str(candidate.get("confidence", "low")).lower()
        return _CONFIDENCE_RANK.get(conf, len(_CONFIDENCE_RANK))

    return min(candidates, key=_rank)


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

    Args:
        frame: Current-frame image as OpenCV BGR ``np.ndarray``, ``bytes``,
            or PIL image.
        adjacent_frame: Adjacent-frame image in the same accepted formats.
        bbox_norm: Normalized bbox ``[x1, y1, x2, y2]`` of the target object.
        scale: Factor by which the ROI is enlarged before differencing
            (default 3.0).
        gaussian_kernel: Optional Gaussian blur kernel applied to both crops
            to suppress compression noise.  Use ``None`` to skip blurring.
        pixel_threshold: Grayscale absolute-difference threshold above which a
            pixel is considered to have changed (default 8.0).

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

    # Normalize to the same dimensions in case decoding produced mismatched
    # sizes (e.g. one frame was resized upstream).
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
        return {
            "mean_diff": 0.0,
            "fraction_above_threshold": 0.0,
            "motion_score": 0.0,
        }

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
            # If the crop is too small for the kernel, skip denoising.
            pass

    diff = cv2.absdiff(current_crop, adjacent_crop)
    mean_diff = float(cv2.mean(diff)[0])

    total_pixels = int(diff.size)
    if total_pixels == 0:
        return {
            "mean_diff": 0.0,
            "fraction_above_threshold": 0.0,
            "motion_score": 0.0,
        }

    _, binary_diff = cv2.threshold(
        diff, pixel_threshold, 255, cv2.THRESH_BINARY
    )
    above = int(cv2.countNonZero(binary_diff))
    fraction = above / total_pixels
    motion_score = mean_diff + fraction * 100.0

    return {
        "mean_diff": mean_diff,
        "fraction_above_threshold": fraction,
        "motion_score": motion_score,
    }
