"""一次性换算脚本:把 design.md §1 的 canonical hex 精确转为 OKLCH。

纯标准库实现(sRGB → linear → XYZ D65 → OKLab → OKLCH,Björn Ottosson
矩阵),无第三方依赖。生成 frontend/src/styles/tokens.css;hex 源改动后重跑:

    python3 frontend/scripts/hex_to_oklch.py

每条颜色 token 输出两层::root 内先写 hex 兜底,再在
@supports (color: oklch(0 0 0)) 块里写 oklch 覆盖。注意不能用"hex 行 + oklch 行
同块并列"的写法——自定义属性解析期不校验,oklch 声明会覆盖 hex 声明,不认
oklch 的老内核在使用处才 IACVT 回退 unset(等同透明),兜底会失效。
"""

from __future__ import annotations

import math
from pathlib import Path

# design.md §1 调色板(token → canonical hex,唯一源)。
PALETTE: list[tuple[str, str]] = [
    ("--color-paper", "#F7F4EE"),
    ("--color-card", "#FFFFFF"),
    ("--color-border", "#E8E2D5"),
    ("--color-line-strong", "#C9C0AF"),
    ("--color-text", "#2A2620"),
    ("--color-text2", "#6B6257"),
    ("--color-accent", "#D97757"),
    ("--color-accent-hover", "#C4664A"),
    ("--color-accent-soft", "#F6E3DA"),
    ("--color-on-accent", "#FFFFFF"),
    ("--color-sage", "#7A9B76"),
    ("--color-sage-soft", "#E6EEE3"),
    ("--color-red", "#B26B5B"),
    ("--color-red-soft", "#F3E2DD"),
    ("--color-blue", "#3E7CB1"),
    ("--color-blue-soft", "#E2ECF4"),
    ("--color-gold", "#C9A227"),
    ("--color-surface-2", "#FBF9F4"),
    ("--color-surface-3", "#EFEAE0"),
    ("--color-surface-4", "#F1EDE4"),
    ("--color-surface-5", "#FCFAF6"),
    ("--color-hover-bg", "#F4EFE5"),
    ("--color-stage-bg", "#1C1A17"),
    # 阶段 5 补齐(SFT 编辑器 / 专家泳道,取自 legacy sft.css / expert.css;
    # design.md 属仓库根未同步,见 tokens.css 顶部注释的生成纪律):
    ("--color-accent-deep", "#F0C4AB"),  # legacy .sft-tok-link(chip hover 联动加深底)
    ("--color-dot-muted", "#D8D1C2"),  # legacy .lane-dot(泳道 queued 状态点)
]

OUT = Path(__file__).resolve().parents[1] / "src" / "styles" / "tokens.css"

HEADER = """/* Hallmark · macrostructure: Workbench · tone: technical/utilitarian · anchor hue: warm-orange · source: design.md (locked) */
/* ==========================================================================
   高速交通事件分析台 v2 — 设计 token(design.md §1 的 OKLCH 承载)
   由 frontend/scripts/hex_to_oklch.py 生成,请勿手改色值;改色先改 design.md。
   每条颜色两层::root 写 hex 兜底,@supports (color: oklch(...)) 内写 oklch
   覆盖(老内核整个 @supports 块跳过,真正回退 hex;同块双行写法会因自定义
   属性解析期不校验而失效)。
   组件内只准引用 var(--token),禁止 inline hex/OKLCH(design.md §8)。
   ========================================================================== */
"""

FONTS = """  --font-pixel: "Fusion Pixel", "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
"""

SCALES = """  --radius: 12px;
  --radius-sm: 8px;
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;
  --space-2xl: 48px;
  --text-xs: 11px;
  --text-sm: 12px;
  --text-md: 13px;
  --text-lg: 15px;
  --text-xl: 18px;
  --text-2xl: 22px;
  --text-display: 28px;
  --ease-out: cubic-bezier(.22, 1, .36, 1);
  --ease-in: cubic-bezier(.64, 0, .78, 0);
  --ease-in-out: cubic-bezier(.83, 0, .17, 1);
  --dur-fast: 120ms;
  --dur-med: 200ms;
"""

# 阴影取自 legacy tokens.css(rgba 黑 5–10%,非调色板 hex,保持原值)。
SHADOWS = """  --shadow: 0 1px 2px rgba(42, 38, 32, 0.05), 0 4px 14px rgba(42, 38, 32, 0.06);
  --shadow-hover: 0 2px 4px rgba(42, 38, 32, 0.07), 0 8px 22px rgba(42, 38, 32, 0.10);
"""

FONT_FACE = """
/* 缝合像素字体(SIL OFL 1.1,保留字体名「缝合像素 / Fusion Pixel」):
   仅用于 UI 骨架。public/fonts/ 下的文件被 Vite 原样拷到 dist/fonts/,
   此处用相对路径(css 在 dist/assets/,../fonts 即 dist/fonts),兼容任意 base。 */
@font-face {
  font-family: "Fusion Pixel";
  src: url("../fonts/fusion-pixel-12px.woff2") format("woff2");
  font-display: swap;
}
"""


def hex_to_oklch(hex_value: str) -> tuple[float, float, float]:
    """#RRGGBB → (L, C, H) OKLCH;H 为角度。"""
    r8, g8, b8 = (int(hex_value[i : i + 2], 16) for i in (1, 3, 5))

    def linearize(c: float) -> float:
        c /= 255.0
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = linearize(r8), linearize(g8), linearize(b8)
    # linear sRGB → CIE XYZ (D65)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    # XYZ → OKLab(经 LMS 立方根)
    l_ = 0.8189330101 * x + 0.3618667424 * y - 0.1288597137 * z
    m_ = 0.0329845436 * x + 0.9293118715 * y + 0.0361456387 * z
    s_ = 0.0482003018 * x + 0.2643662691 * y + 0.6338517070 * z
    l_, m_, s_ = math.copysign(abs(l_) ** (1 / 3), l_), m_ ** (1 / 3), s_ ** (1 / 3)
    lab_l = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    lab_a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    lab_b = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    chroma = math.hypot(lab_a, lab_b)
    hue = math.degrees(math.atan2(lab_b, lab_a)) % 360.0
    return lab_l, chroma, hue


def fmt(l: float, c: float, h: float) -> str:
    """oklch() 字符串;近无色度(白/黑/灰)hue 无意义,固定写 0.00。

    不能省略 hue 写成 oklch(L C) 两参式:主流内核(Safari/Firefox/Chromium)
    将其判为非法色,自定义属性在使用处触发 IACVT 回退 unset(等同透明),
    曾导致 DirPickerModal 的 var(--color-card) 背景全透。"""
    if c < 0.0005:
        return f"oklch({l:.4f} {c:.4f} 0.00)"
    return f"oklch({l:.4f} {c:.4f} {h:.2f})"


def main() -> None:
    lines = [HEADER, ":root {\n"]
    oklch_lines = []
    for token, hex_value in PALETTE:
        l, c, h = hex_to_oklch(hex_value)
        lines.append(f"  {token}: {hex_value};\n")  # 兜底:老内核(无 oklch 支持)只用这层
        oklch_lines.append(f"  {token}: {fmt(l, c, h)}; /* 源 {hex_value} */\n")
        print(f"{token:24s} {hex_value} -> {fmt(l, c, h)}")
    lines.append("\n")
    lines.append(SHADOWS)
    lines.append(FONTS)
    lines.append(SCALES)
    lines.append("}\n")
    # 支持 oklch 的现代浏览器用精确 OKLCH 值覆盖;老内核跳过整个块,hex 兜底生效
    lines.append("\n@supports (color: oklch(0 0 0)) {\n:root {\n")
    lines.extend(oklch_lines)
    lines.append("}\n}\n")
    lines.append(FONT_FACE)
    OUT.write_text("".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
