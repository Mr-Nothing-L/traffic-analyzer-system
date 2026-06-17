"""Unit tests for far_non_motor_enhancer utilities."""

from __future__ import annotations

import io
import os
import tempfile

import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.utils.far_non_motor_enhancer import (
    compute_bbox_area_px,
    compute_enlarged_bbox,
    create_composite,
    create_motion_comparison_composite,
    is_bbox_large_enough,
    load_image,
    select_best_candidate,
)


def test_compute_enlarged_bbox_centered():
    """Enlarging a centered bbox by 2x quadruples the area."""
    bbox = [0.4, 0.4, 0.6, 0.6]  # 0.2 x 0.2 centered at (0.5, 0.5)
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    assert enlarged == pytest.approx([0.3, 0.3, 0.7, 0.7], abs=1e-9)


def test_compute_enlarged_bbox_clips_to_zero_one():
    """Enlarged bbox near image borders is clipped to [0, 1]."""
    bbox = [0.0, 0.0, 0.2, 0.2]
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    assert all(0.0 <= c <= 1.0 for c in enlarged)
    assert enlarged[0] == 0.0
    assert enlarged[1] == 0.0


def test_compute_enlarged_bbox_custom_scale():
    """Custom scale is applied to width and height."""
    bbox = [0.45, 0.45, 0.55, 0.55]
    enlarged = compute_enlarged_bbox(bbox, scale=3.0)
    assert enlarged == pytest.approx([0.35, 0.35, 0.65, 0.65], abs=1e-9)


def test_tiny_bbox_enlarged_is_valid():
    """A very small bbox is enlarged and still stays within [0, 1]."""
    bbox = [0.501, 0.501, 0.502, 0.502]
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    assert len(enlarged) == 4
    assert enlarged[0] <= enlarged[2]
    assert enlarged[1] <= enlarged[3]
    assert all(0.0 <= c <= 1.0 for c in enlarged)


def test_load_image_from_ndarray_bgr():
    """BGR ndarray is converted to RGB PIL image."""
    bgr = np.zeros((10, 10, 3), dtype=np.uint8)
    bgr[:, :, 0] = 255  # blue in BGR
    img = load_image(bgr)
    assert isinstance(img, Image.Image)
    assert img.mode == "RGB"
    assert img.size == (10, 10)
    # Top-left pixel should be blue in RGB -> (0, 0, 255)
    assert img.getpixel((0, 0)) == (0, 0, 255)


def test_load_image_from_bytes():
    """Bytes are decoded into an RGB PIL image."""
    img = Image.new("RGB", (20, 20), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    loaded = load_image(buf.getvalue())
    assert isinstance(loaded, Image.Image)
    assert loaded.mode == "RGB"
    assert loaded.size == (20, 20)


def test_load_image_from_pil():
    """PIL image is returned as RGB."""
    img = Image.new("RGBA", (15, 15), color=(255, 0, 0, 128))
    loaded = load_image(img)
    assert loaded.mode == "RGB"
    assert loaded.size == (15, 15)


def test_create_composite_dimensions():
    """Composite dimensions equal original width + separator + zoom width, same height."""
    width, height = 400, 300
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    bbox = [0.45, 0.45, 0.55, 0.55]
    composite = create_composite(frame, bbox_norm=bbox)

    assert isinstance(composite, Image.Image)
    assert composite.mode == "RGB"
    assert composite.height == height

    # Zoom width is capped at half original width.
    expected_zoom_width = width // 2
    expected_total_width = width + 3 + expected_zoom_width
    assert composite.width == expected_total_width


def test_create_composite_aspect_ratio_preserved():
    """Zoomed crop preserves the aspect ratio of the enlarged ROI."""
    width, height = 640, 480
    # White background with a gray ROI so the zoomed panel can be measured.
    frame = np.full((height, width, 3), 255, dtype=np.uint8)
    bbox = [0.3, 0.45, 0.5, 0.55]
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    x1 = int(round(enlarged[0] * width))
    y1 = int(round(enlarged[1] * height))
    x2 = int(round(enlarged[2] * width))
    y2 = int(round(enlarged[3] * height))
    frame[y1:y2, x1:x2] = (100, 100, 100)

    composite = create_composite(frame, bbox_norm=bbox)

    # Expected zoom panel size: height capped to frame height, width capped to half frame.
    crop_w = x2 - x1
    crop_h = y2 - y1
    expected_zoom_w = width // 2
    expected_zoom_h = int(round(crop_h * (expected_zoom_w / crop_w)))
    assert composite.width == width + 3 + expected_zoom_w

    # Inspect the right panel to verify the zoomed ROI size.
    right_panel = composite.crop((width + 3, 0, composite.width, composite.height))
    right_arr = np.array(right_panel)
    # Find rows and columns that differ from the white background.
    white = np.array([255, 255, 255], dtype=np.uint8)
    diff = np.any(right_arr != white, axis=2)
    rows = np.any(diff, axis=1)
    cols = np.any(diff, axis=0)
    assert rows.any() and cols.any()
    y_top = int(np.argmax(rows))
    y_bottom = int(len(rows) - np.argmax(rows[::-1]))
    x_left = int(np.argmax(cols))
    x_right = int(len(cols) - np.argmax(cols[::-1]))
    content_w = x_right - x_left
    content_h = y_bottom - y_top
    assert content_w == expected_zoom_w
    assert content_h == expected_zoom_h
    assert content_w / content_h == pytest.approx(crop_w / crop_h, abs=1.0)


def test_create_composite_saves_to_file():
    """Composite is saved to disk when output_path is provided."""
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    bbox = [0.4, 0.4, 0.6, 0.6]
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "composite.png")
        composite = create_composite(frame, bbox_norm=bbox, output_path=out_path)
        assert os.path.exists(out_path)
        reloaded = Image.open(out_path)
        assert reloaded.size == composite.size


def test_select_best_candidate_returns_highest_confidence():
    """Highest-confidence candidate is selected."""
    candidates = [
        {"confidence": "low", "bbox_norm": [0.1, 0.1, 0.2, 0.2]},
        {"confidence": "high", "bbox_norm": [0.3, 0.3, 0.4, 0.4]},
        {"confidence": "medium", "bbox_norm": [0.5, 0.5, 0.6, 0.6]},
    ]
    best = select_best_candidate(candidates)
    assert best is not None
    assert best["confidence"] == "high"
    assert best["bbox_norm"] == [0.3, 0.3, 0.4, 0.4]


def test_select_best_candidate_empty_list():
    """Empty candidate list returns None."""
    assert select_best_candidate([]) is None


def test_select_best_candidate_unknown_confidence():
    """Unknown confidence is treated as lower than 'low'."""
    candidates = [
        {"confidence": "low", "bbox_norm": [0.1, 0.1, 0.2, 0.2]},
        {"confidence": "unknown", "bbox_norm": [0.3, 0.3, 0.4, 0.4]},
    ]
    best = select_best_candidate(candidates)
    assert best["confidence"] == "low"


def test_compute_bbox_area_px():
    """Pixel area is computed correctly from normalized bbox."""
    bbox = [0.1, 0.1, 0.3, 0.4]  # 0.2 x 0.3 normalized
    area = compute_bbox_area_px(bbox, width=1000, height=1000)
    # Expected: 200 x 300 = 60000
    assert area == 60000


def test_is_bbox_large_enough_passes():
    """BBox with area >= threshold passes."""
    bbox = [0.0, 0.0, 0.1, 0.1]  # 100 x 100 = 10000 px on 1000x1000
    assert is_bbox_large_enough(bbox, width=1000, height=1000, min_area_px=80) is True


def test_is_bbox_large_enough_fails():
    """Tiny bbox below threshold fails."""
    bbox = [0.0, 0.0, 0.001, 0.001]  # ~1 px on 1000x1000
    assert is_bbox_large_enough(bbox, width=1000, height=1000, min_area_px=80) is False


def test_is_bbox_large_enough_default_threshold():
    """Default threshold is 80 pixels."""
    bbox = [0.0, 0.0, 0.009, 0.009]  # ~81 px on 1000x1000
    assert is_bbox_large_enough(bbox, width=1000, height=1000) is True
    bbox_small = [0.0, 0.0, 0.008, 0.008]  # ~64 px on 1000x1000
    assert is_bbox_large_enough(bbox_small, width=1000, height=1000) is False


def test_create_motion_comparison_composite_dimensions():
    """Dimensions equal two panels plus a separator, with equal panel sizes."""
    width, height = 200, 150
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    adjacent_frame = np.zeros((height, width, 3), dtype=np.uint8)
    bbox = [0.4, 0.4, 0.6, 0.6]

    composite = create_motion_comparison_composite(
        frame,
        adjacent_frame,
        bbox_norm=bbox,
        scale=2.0,
    )

    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    ex1 = int(round(enlarged[0] * width))
    ey1 = int(round(enlarged[1] * height))
    ex2 = int(round(enlarged[2] * width))
    ey2 = int(round(enlarged[3] * height))
    panel_width = ex2 - ex1
    panel_height = ey2 - ey1

    assert isinstance(composite, Image.Image)
    assert composite.mode == "RGB"
    assert composite.width == 2 * panel_width + 3
    assert composite.height == panel_height


def test_create_motion_comparison_composite_saves_to_file():
    """Composite is saved to disk when output_path is provided."""
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    adjacent_frame = np.zeros((100, 200, 3), dtype=np.uint8)
    bbox = [0.4, 0.4, 0.6, 0.6]

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "motion_comparison.jpg")
        composite = create_motion_comparison_composite(
            frame,
            adjacent_frame,
            bbox_norm=bbox,
            output_path=out_path,
        )
        assert os.path.exists(out_path)
        reloaded = Image.open(out_path)
        assert reloaded.size == composite.size


def test_create_motion_comparison_composite_draws_red_box_on_both_panels():
    """Both left and right panels contain the red ROI outline."""
    width, height = 200, 150
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    adjacent_frame = np.full((height, width, 3), 10, dtype=np.uint8)
    bbox = [0.45, 0.45, 0.55, 0.55]

    composite = create_motion_comparison_composite(
        frame,
        adjacent_frame,
        bbox_norm=bbox,
        scale=2.0,
    )

    panel_width = (composite.width - 3) // 2
    left_panel = composite.crop((0, 0, panel_width, composite.height))
    right_panel = composite.crop((panel_width + 3, 0, composite.width, composite.height))

    red = np.array([255, 0, 0], dtype=np.uint8)
    left_arr = np.array(left_panel)
    right_arr = np.array(right_panel)

    assert np.any(np.all(left_arr == red, axis=2))
    assert np.any(np.all(right_arr == red, axis=2))
