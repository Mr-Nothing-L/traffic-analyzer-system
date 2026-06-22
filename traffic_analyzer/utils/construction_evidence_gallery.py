"""Multi-ROI construction evidence gallery."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from PIL import Image, ImageDraw

from .bbox_geometry import compute_enlarged_bbox, _norm_to_px
from .image_drawing import _draw_text_with_background, _load_scaled_font, load_image


# Colour palette for construction evidence tags.
_CONSTRUCTION_TAG_COLORS = {
    "cone": (255, 0, 0),      # red
    "worker": (0, 255, 0),    # green
    "vehicle": (0, 0, 255),   # blue
    "barrier": (255, 165, 0), # orange
    "sign": (255, 0, 255),    # magenta
}


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
