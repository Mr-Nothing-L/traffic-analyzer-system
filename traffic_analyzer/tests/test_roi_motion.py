"""Unit tests for the ROI motion comparison panel annotations."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.utils.roi_motion import _draw_panel_annotations


def _gold_pixel_centroid(panel: Image.Image) -> tuple[int, float, float]:
    """Return (count, mean_x, mean_y) of #FFD700 crosshair pixels."""
    arr = np.array(panel.convert("RGB"))
    mask = (
        (arr[:, :, 0] == 255)
        & (arr[:, :, 1] == 215)
        & (arr[:, :, 2] == 0)
    )
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return 0, 0.0, 0.0
    return int(len(xs)), float(xs.mean()), float(ys.mean())


def test_draw_panel_annotations_crosshair_uses_crop_relative_coords() -> None:
    """Gold crosshair must be centered on the bbox inside the cropped panel.

    Regression test: the crosshair used absolute frame coordinates, so ROIs
    away from the top-left corner placed it outside the panel entirely.
    """
    frame_width, frame_height = 200, 100
    bbox_norm = [0.5, 0.4, 0.6, 0.6]  # px [100, 40, 120, 60], center (110, 50)
    enlarged_px = [90, 30, 130, 70]  # 40x40 crop away from the origin
    panel = Image.new("RGB", (80, 80), (128, 128, 128))  # scale_x = scale_y = 2.0

    annotated = _draw_panel_annotations(
        panel, bbox_norm, enlarged_px, frame_width, frame_height
    )

    count, mean_x, mean_y = _gold_pixel_centroid(annotated)
    assert count > 0
    # Expected center: ((110 - 90) * 2, (50 - 30) * 2) = (40, 40)
    assert mean_x == pytest.approx(40.0, abs=2.0)
    assert mean_y == pytest.approx(40.0, abs=2.0)
