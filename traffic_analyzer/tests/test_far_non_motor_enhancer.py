"""Unit tests for far_non_motor_enhancer utilities.

[文件说明]
作用:测试远距非机动车增强工具函数,覆盖 bbox 面积/宽高比/放大计算、运动评分与多 ROI 合成图生成。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/utils/far_non_motor_enhancer.py(被测模块)。
"""

from __future__ import annotations

import io
import os
import tempfile

import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.models.schemas import FarObjectEnhancementConfig
from traffic_analyzer.utils.far_non_motor_enhancer import (
    compute_bbox_area_px,
    compute_bbox_aspect_ratio,
    compute_enlarged_bbox,
    compute_roi_motion_score,
    create_composite,
    create_motion_comparison_composite,
    create_multi_roi_gallery,
    is_bbox_aspect_valid,
    is_bbox_large_enough,
    load_image,
)


@pytest.mark.parametrize(
    "bbox,scale,expected",
    [
        ([0.4, 0.4, 0.6, 0.6], 2.0, [0.3, 0.3, 0.7, 0.7]),
        ([0.45, 0.45, 0.55, 0.55], 3.0, [0.35, 0.35, 0.65, 0.65]),
    ],
)
def test_compute_enlarged_bbox(bbox, scale, expected):
    """Enlarging a centered bbox scales around its center."""
    enlarged = compute_enlarged_bbox(bbox, scale=scale)
    assert enlarged == pytest.approx(expected, abs=1e-9)


def test_compute_enlarged_bbox_clips_to_zero_one():
    """Enlarged bbox near image borders is clipped to [0, 1]."""
    bbox = [0.0, 0.0, 0.2, 0.2]
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    assert all(0.0 <= c <= 1.0 for c in enlarged)
    assert enlarged[0] == 0.0
    assert enlarged[1] == 0.0


def test_tiny_bbox_enlarged_is_valid():
    """A very small bbox is enlarged and still stays within [0, 1]."""
    bbox = [0.501, 0.501, 0.502, 0.502]
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    assert len(enlarged) == 4
    assert enlarged[0] <= enlarged[2]
    assert enlarged[1] <= enlarged[3]
    assert all(0.0 <= c <= 1.0 for c in enlarged)


@pytest.mark.parametrize(
    "factory,expected_pixel",
    [
        (lambda: np.zeros((10, 10, 3), dtype=np.uint8), (0, 0, 255)),
        (lambda: Image.new("RGBA", (15, 15), color=(255, 0, 0, 128)), (255, 0, 0)),
    ],
)
def test_load_image(factory, expected_pixel):
    """ndarray BGR and RGBA PIL inputs are converted to RGB PIL images."""
    img_in = factory()
    if isinstance(img_in, np.ndarray):
        img_in[:, :, 0] = 255  # blue in BGR
    loaded = load_image(img_in)
    assert isinstance(loaded, Image.Image)
    assert loaded.mode == "RGB"
    assert loaded.getpixel((0, 0)) == expected_pixel


def test_load_image_from_bytes():
    """Bytes are decoded into an RGB PIL image."""
    img = Image.new("RGB", (20, 20), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    loaded = load_image(buf.getvalue())
    assert isinstance(loaded, Image.Image)
    assert loaded.mode == "RGB"
    assert loaded.size == (20, 20)


def test_create_composite_dimensions():
    """Composite dimensions equal original width + separator + zoom width, same height."""
    width, height = 400, 300
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    bbox = [0.45, 0.45, 0.55, 0.55]
    composite = create_composite(frame, bbox_norm=bbox)

    assert isinstance(composite, Image.Image)
    assert composite.mode == "RGB"
    assert composite.height == height

    expected_zoom_width = width // 2
    expected_total_width = width + 3 + expected_zoom_width
    assert composite.width == expected_total_width


def test_create_composite_aspect_ratio_preserved():
    """Zoomed crop preserves the aspect ratio of the enlarged ROI."""
    width, height = 640, 480
    frame = np.full((height, width, 3), 255, dtype=np.uint8)
    bbox = [0.3, 0.45, 0.5, 0.55]
    enlarged = compute_enlarged_bbox(bbox, scale=2.0)
    x1 = int(round(enlarged[0] * width))
    y1 = int(round(enlarged[1] * height))
    x2 = int(round(enlarged[2] * width))
    y2 = int(round(enlarged[3] * height))
    frame[y1:y2, x1:x2] = (100, 100, 100)

    composite = create_composite(frame, bbox_norm=bbox)

    crop_w = x2 - x1
    crop_h = y2 - y1
    expected_zoom_w = width // 2
    expected_zoom_h = int(round(crop_h * (expected_zoom_w / crop_w)))
    assert composite.width == width + 3 + expected_zoom_w

    right_panel = composite.crop((width + 3, 0, composite.width, composite.height))
    right_arr = np.array(right_panel)
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


@pytest.mark.parametrize(
    "bbox,width,height,expected",
    [
        ([0.1, 0.1, 0.3, 0.4], 1000, 1000, 60000),
        ([0.0, 0.0, 0.1, 0.1], 1000, 1000, 10000),
    ],
)
def test_compute_bbox_area_px(bbox, width, height, expected):
    """Pixel area is computed correctly from normalized bbox."""
    assert compute_bbox_area_px(bbox, width=width, height=height) == expected


@pytest.mark.parametrize(
    "bbox,min_area,expected",
    [
        ([0.0, 0.0, 0.1, 0.1], 80, True),  # 10000 px
        ([0.0, 0.0, 0.001, 0.001], 80, False),  # ~1 px
        ([0.0, 0.0, 0.009, 0.009], 80, True),  # ~81 px
        ([0.0, 0.0, 0.008, 0.008], 80, False),  # ~64 px
    ],
)
def test_is_bbox_large_enough(bbox, min_area, expected):
    """Area threshold check works on a 1000x1000 canvas."""
    assert is_bbox_large_enough(bbox, width=1000, height=1000, min_area_px=min_area) is expected


@pytest.mark.parametrize(
    "bbox,expected",
    [
        ([0.1, 0.1, 0.3, 0.4], 0.2 / 0.3),
        ([0.1, 0.2, 0.3, 0.2], float("inf")),
        ([0.1, 0.1, 0.4, 0.2], 0.3 / 0.1),
    ],
)
def test_compute_bbox_aspect_ratio(bbox, expected):
    """Aspect ratio is width / height, with inf for zero-height bboxes."""
    assert compute_bbox_aspect_ratio(bbox) == pytest.approx(expected)


@pytest.mark.parametrize(
    "bbox,max_ratio,expected",
    [
        ([0.4, 0.3, 0.5, 0.6], 1.0, True),  # ratio 1/3
        ([0.3, 0.4, 0.6, 0.5], 1.0, False),  # ratio 3
        ([0.3, 0.4, 0.6, 0.5], 4.0, True),
        ([0.3, 0.4, 0.6, 0.5], 2.0, False),
    ],
)
def test_is_bbox_aspect_valid(bbox, max_ratio, expected):
    """Tall bboxes pass, flat bboxes fail, custom thresholds tune behavior."""
    assert is_bbox_aspect_valid(bbox, max_ratio=max_ratio) is expected


def test_create_motion_comparison_composite_dimensions():
    """Dimensions equal two upscaled panels plus a separator, with equal panel sizes."""
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
    crop_w = ex2 - ex1
    crop_h = ey2 - ey1

    target_height = min(height, max(height // 2, crop_h * 4))
    target_width = int(round(crop_w * target_height / crop_h))

    assert isinstance(composite, Image.Image)
    assert composite.mode == "RGB"
    assert composite.width == 2 * target_width + 3
    assert composite.height == target_height


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


@pytest.mark.parametrize(
    "desc,frame_factory,expected_motion",
    [
        (
            "identical frames",
            lambda h, w: np.zeros((h, w, 3), dtype=np.uint8),
            0.0,
        ),
        (
            "degenerate bbox",
            lambda h, w: np.zeros((h, w, 3), dtype=np.uint8),
            0.0,
        ),
    ],
)
def test_compute_roi_motion_score_zero_cases(desc, frame_factory, expected_motion):
    """Identical frames or degenerate bboxes return zero motion metrics."""
    if "degenerate" in desc:
        bbox = [0.5, 0.5, 0.5, 0.5]
        frame = frame_factory(50, 50)
    else:
        bbox = [0.45, 0.45, 0.55, 0.55]
        frame = frame_factory(100, 100)

    score = compute_roi_motion_score(frame, frame, bbox)
    assert score["mean_diff"] == pytest.approx(0.0, abs=1e-6)
    assert score["fraction_above_threshold"] == pytest.approx(0.0, abs=1e-6)
    assert score["motion_score"] == pytest.approx(expected_motion, abs=1e-6)


def test_compute_roi_motion_score_detects_moving_object():
    """A shifted bright spot inside the ROI yields a positive motion score."""
    height, width = 100, 100
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    adjacent = np.zeros((height, width, 3), dtype=np.uint8)

    frame[48:53, 48:53] = 200
    adjacent[48:53, 55:60] = 200

    bbox = [0.45, 0.45, 0.55, 0.55]
    score = compute_roi_motion_score(frame, adjacent, bbox)

    assert score["mean_diff"] > 0.0
    assert score["fraction_above_threshold"] > 0.0
    assert score["motion_score"] > FarObjectEnhancementConfig().motion_score_threshold


def test_create_multi_roi_gallery_fills_bottom_panel():
    """Bottom grid cells cover the full panel width and use the full cell height."""
    width, height = 400, 300
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    # Fill a few ROIs with distinct colours so the crops are non-empty.
    frame[80:120, 60:100] = [255, 0, 0]
    frame[80:120, 180:220] = [0, 255, 0]
    frame[180:220, 60:100] = [0, 0, 255]
    frame[180:220, 180:220] = [255, 255, 0]

    regions = [
        {"bbox_norm": [0.15, 0.27, 0.25, 0.40], "tag": "cone", "confidence": 0.9},
        {"bbox_norm": [0.45, 0.27, 0.55, 0.40], "tag": "worker", "confidence": 0.8},
        {"bbox_norm": [0.15, 0.60, 0.25, 0.73], "tag": "barrier", "confidence": 0.7},
        {"bbox_norm": [0.45, 0.60, 0.55, 0.73], "tag": "sign", "confidence": 0.6},
    ]

    gallery = create_multi_roi_gallery(frame, regions)
    assert gallery.size == (width, height * 2)

    # With 4 ROIs the bottom panel is split into a 2x2 grid.
    bottom = gallery.crop((0, height, width, height * 2))
    # Very little grey background should remain: each cell is filled by cover-resize.
    grey = np.array([240, 240, 240], dtype=np.uint8)
    arr = np.array(bottom)
    grey_mask = np.all(arr == grey, axis=2)
    # Allow only a narrow separator and rounding margins (~2% of pixels).
    assert grey_mask.sum() / grey_mask.size < 0.02


def test_create_multi_roi_gallery_single_roi_uses_full_bottom():
    """A single ROI occupies the entire bottom panel."""
    width, height = 200, 150
    frame = np.full((height, width, 3), 128, dtype=np.uint8)
    frame[60:90, 80:120] = [255, 0, 0]

    gallery = create_multi_roi_gallery(
        frame,
        [{"bbox_norm": [0.40, 0.40, 0.60, 0.60], "tag": "cone", "confidence": 0.95}],
    )
    assert gallery.size == (width, height * 2)
