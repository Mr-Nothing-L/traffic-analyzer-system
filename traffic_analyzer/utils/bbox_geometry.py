"""Normalized bounding-box geometry utilities."""

from __future__ import annotations

from typing import List


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
