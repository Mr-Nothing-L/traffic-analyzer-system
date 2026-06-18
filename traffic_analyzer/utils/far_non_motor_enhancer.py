"""Far-distance non-motor vehicle ROI enlargement and composition utilities.

This module provides helpers to enlarge a small/normalized bounding box
around a distant non-motor vehicle and produce a side-by-side composite:
left = original frame with the enlarged ROI highlighted, right = magnified ROI.

Only PIL / numpy / cv2 are used; no ML detection models.
"""

from __future__ import annotations

import io
import os
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


def is_bbox_aspect_valid(
    bbox_norm: List[float], max_ratio: float = 1.0
) -> bool:
    """True if width/height is below ``max_ratio`` (tall / thin objects)."""
    return compute_bbox_aspect_ratio(bbox_norm) < max_ratio


# Backward-compatible alias for existing callers.
is_bbox_aspect_valid_for_non_motor = is_bbox_aspect_valid


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


# Colour palette for construction evidence tags.
_CONSTRUCTION_TAG_COLORS = {
    "cone": (255, 0, 0),      # red
    "worker": (0, 255, 0),    # green
    "vehicle": (0, 0, 255),   # blue
    "barrier": (255, 165, 0), # orange
    "sign": (255, 0, 255),    # magenta
}


def _draw_text_with_background(
    draw: ImageDraw.ImageDraw,
    text: str,
    pos: Tuple[int, int],
    fill: Tuple[int, int, int] = (255, 255, 255),
    background: Tuple[int, int, int] = (0, 0, 0),
    font: Optional["ImageFont.FreeTypeFont"] = None,
) -> None:
    """Draw text with a small background box so it is readable on any image."""
    if font is None:
        try:
            from PIL import ImageFont
            font = ImageFont.load_default()
        except Exception:
            font = None

    bbox = draw.textbbox(pos, text, font=font) if font else None
    if bbox:
        draw.rectangle(bbox, fill=background)
    draw.text(pos, text, fill=fill, font=font)


def _load_scaled_font(size: int):
    """Load a scalable system font, falling back to PIL's default bitmap font."""
    try:
        from PIL import ImageFont
    except Exception:  # pragma: no cover
        return None

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.otf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:  # pragma: no cover
        return None


def _compute_grid_layout(
    n: int,
    width: int,
    height: int,
    gap: int = 2,
) -> List[Tuple[int, int, int, int]]:
    """Return (x, y, cell_w, cell_h) for each ROI in the bottom panel.

    Layouts are chosen to maximize cell area for the actual number of ROIs:
      * 1 ROI  -> full panel
      * 2 ROIs -> side-by-side, full height
      * 3 ROIs -> two cells on top, one wide cell spanning the bottom row
      * 4 ROIs -> 2x2 grid
    """
    if n <= 0:
        return []
    if n == 1:
        return [(0, 0, width, height)]
    if n == 2:
        left_w = (width - gap) // 2
        right_w = width - left_w - gap
        return [(0, 0, left_w, height), (left_w + gap, 0, right_w, height)]
    if n == 3:
        top_h = (height - gap) // 2
        bottom_h = height - top_h - gap
        left_w = (width - gap) // 2
        right_w = width - left_w - gap
        return [
            (0, 0, left_w, top_h),
            (left_w + gap, 0, right_w, top_h),
            (0, top_h + gap, width, bottom_h),
        ]
    # n >= 4 -> 2x2 grid, capped by the caller to top-4.
    cols, rows = 2, 2
    cell_w = (width - gap * (cols - 1)) // cols
    cell_h = (height - gap * (rows - 1)) // rows
    positions: List[Tuple[int, int, int, int]] = []
    for idx in range(min(n, cols * rows)):
        col = idx % cols
        row = idx // cols
        x = col * (cell_w + gap)
        y = row * (cell_h + gap)
        w = width - x if col == cols - 1 else cell_w
        h = height - y if row == rows - 1 else cell_h
        positions.append((x, y, w, h))
    return positions


def _resize_crop_to_fill(
    crop: Image.Image,
    target_size: Tuple[int, int],
) -> Image.Image:
    """Resize ``crop`` so it completely covers ``target_size`` and center-crop.

    This fills the cell with the ROI (possibly cropping a little context) and
    avoids the large grey borders produced by ``thumbnail`` fitting.
    """
    target_w, target_h = target_size
    crop_w, crop_h = crop.size
    if crop_w <= 0 or crop_h <= 0:
        raise ValueError("Enlarged bbox produced an empty crop")

    scale = max(target_w / crop_w, target_h / crop_h)
    new_w = int(round(crop_w * scale))
    new_h = int(round(crop_h * scale))

    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    resized = crop.resize((new_w, new_h), resample)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def create_multi_roi_gallery(
    original_image: Union[np.ndarray, bytes, Image.Image],
    regions: List[Dict[str, Any]],
    output_path: Optional[str] = None,
    max_regions: int = 4,
) -> Image.Image:
    """Create a top/bottom gallery for multiple evidence ROIs.

    Top half: original frame with all ``regions`` bounding boxes drawn in
    tag-specific colours and a short label.
    Bottom half: enlarged crops of the ROIs arranged dynamically to fill the
    available space (1x1, 1x2, 2x2 with a wide bottom cell for 3 ROIs, or 2x2).
    Each crop panel is captioned with ``tag`` and ``confidence``.

    Args:
        original_image: Source frame (PIL Image, bytes, or numpy array).
        regions: List of dicts with keys ``bbox_norm``, ``tag``, ``confidence``.
            ``bbox_norm`` is normalised ``[x1, y1, x2, y2]``.
        output_path: Where to save the resulting JPEG. If ``None``, the image
            is only returned.
        max_regions: Maximum number of regions to show. Higher-confidence
            regions should already be passed in; this argument simply caps the
            grid size.

    Returns:
        The assembled PIL Image.
    """
    original = load_image(original_image)
    width, height = original.size

    # Keep only the top regions and assign a stable colour per tag.
    display_regions = regions[:max_regions]

    # --- Top panel: annotated original ------------------------------------
    annotated = original.copy()
    draw = ImageDraw.Draw(annotated)
    for region in display_regions:
        bbox_norm = region.get("bbox_norm")
        tag = str(region.get("tag", "unknown"))
        confidence = float(region.get("confidence", 0.0))
        if not bbox_norm or len(bbox_norm) != 4:
            continue
        color = _CONSTRUCTION_TAG_COLORS.get(tag.lower(), (128, 128, 128))
        px = _norm_to_px(bbox_norm, width, height)
        draw.rectangle(px, outline=color, width=3)
        label = f"{tag} {confidence:.2f}"
        _draw_text_with_background(draw, label, (px[0], max(0, px[1] - 12)), fill=(255, 255, 255), background=color)

    # --- Bottom panel: enlarged ROI grid ----------------------------------
    n = len(display_regions)
    if n == 0:
        grid = Image.new("RGB", (width, height), color=(128, 128, 128))
    else:
        grid = Image.new("RGB", (width, height), color=(240, 240, 240))
        grid_draw = ImageDraw.Draw(grid)

        positions = _compute_grid_layout(n, width, height, gap=2)
        for idx, region in enumerate(display_regions):
            bbox_norm = region.get("bbox_norm")
            tag = str(region.get("tag", "unknown"))
            confidence = float(region.get("confidence", 0.0))
            if not bbox_norm or len(bbox_norm) != 4:
                continue

            x, y, cell_w, cell_h = positions[idx]

            # Enlarge the ROI slightly so context is visible.
            enlarged_norm = compute_enlarged_bbox(bbox_norm, scale=2.0)
            enlarged_px = _norm_to_px(enlarged_norm, width, height)
            crop = original.crop(enlarged_px)

            # Resize crop so it completely fills the cell, then paste.
            filled = _resize_crop_to_fill(crop, (cell_w, cell_h))
            grid.paste(filled, (x, y))

            # Overlay caption inside the cell so the crop can use the full area.
            caption = f"{tag}: {confidence:.2f}"
            font_size = max(12, min(32, cell_h // 25))
            font = _load_scaled_font(font_size)
            _draw_text_with_background(
                grid_draw,
                caption,
                (x + 4, y + 4),
                fill=(255, 255, 255),
                background=(0, 0, 0),
                font=font,
            )

    # --- Assemble top + bottom --------------------------------------------
    gallery = Image.new("RGB", (width, height * 2), color=(255, 255, 255))
    gallery.paste(annotated, (0, 0))
    gallery.paste(grid, (0, height))

    if output_path is not None:
        gallery.save(output_path)

    return gallery
