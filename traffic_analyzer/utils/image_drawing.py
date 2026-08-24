"""Image loading helpers and low-level drawing primitives.

[文件说明]
作用:图像加载与底层绘制原语:``load_image`` 将 numpy/bytes/PIL 统一为 RGB
    PIL 图像;``_draw_crosshair`` 画中心十字;``_draw_text_with_background``
    画带底色文字;``_load_scaled_font`` 加载可缩放系统字体(含回退)。
上游:utils 内 roi_composite/roi_motion/construction_evidence_gallery/
    emergency_lane_occupancy,并经 far_non_motor_enhancer 间接服务于
    core/expert_agent_far_enhancement.py。
下游:PIL/OpenCV;系统字体文件(探测常见 Linux/macOS/Windows 路径)。
"""

from __future__ import annotations

import io
import os
from typing import Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw


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
        # CJK-capable fonts first: Chinese labels render as tofu with DejaVu/Liberation.
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.otf",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
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
