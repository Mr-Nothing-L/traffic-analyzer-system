"""Image overlay helpers shared across the analysis pipeline."""

from __future__ import annotations

import io
import os
import platform
from typing import Any, List, Optional

from PIL import Image, ImageDraw, ImageFont


def get_system_font_path() -> Optional[str]:
    """Return a path to a usable bold system font, or None if none found."""
    system = platform.system()
    candidates: List[str] = []
    if system == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates = [
            os.path.join(windir, r"Fonts\arialbd.ttf"),
            os.path.join(windir, r"Fonts\arial.ttf"),
            os.path.join(windir, r"Fonts\msyhbd.ttc"),
            os.path.join(windir, r"Fonts\simhei.ttf"),
        ]
    elif system == "Darwin":
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Arial.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def annotate_frame(image: Any, label: str) -> bytes:
    """Overlay *label* on the top-left corner of *image* and return as JPEG bytes.

    The label is drawn with a dark semi-transparent background so it remains
    readable regardless of image content.
    """
    if isinstance(image, bytes):
        img = Image.open(io.BytesIO(image))
    elif isinstance(image, str):
        img = Image.open(image)
    elif isinstance(image, Image.Image):
        img = image.copy()
    else:
        raise TypeError(f"Unsupported image type: {type(image)}")

    img = img.convert("RGB")

    img.thumbnail((1920, 1080), Image.LANCZOS)

    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    font_path = get_system_font_path()
    if font_path:
        try:
            font = ImageFont.truetype(font_path, 28)
        except Exception:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)

    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 10
    rect = [4, 4, 8 + text_w + pad * 2, 8 + text_h + pad * 2]

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(rect, fill=(0, 0, 0, 180))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    draw.text((8 + pad, 8 + pad), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
