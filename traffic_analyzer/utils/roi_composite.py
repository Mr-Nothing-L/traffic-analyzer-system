"""Single-ROI enhancement composite."""

from __future__ import annotations

from typing import List, Optional, Tuple, Union

import numpy as np
from PIL import Image, ImageDraw

from .bbox_geometry import compute_enlarged_bbox, _norm_to_px
from .image_drawing import _draw_crosshair, load_image


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
