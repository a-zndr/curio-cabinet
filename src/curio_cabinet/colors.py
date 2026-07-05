"""Small color helpers for the accent theme (stdlib only).

The design tokens can take either a hue (legacy `accent_hue`, rendered via
oklch at a fixed lightness/chroma) or a full `accent` hex color chosen with a
normal color picker. These helpers seed the picker from a hue and pick a
readable text color to sit on the accent.
"""

from __future__ import annotations

import math
import re

__all__ = ["normalize_hex", "oklch_to_hex", "contrast_hex"]

_HEX_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_hex(value: str) -> str | None:
    """Return '#rrggbb' lowercase, or None if not a valid hex color."""
    m = _HEX_RE.fullmatch(value.strip())
    if not m:
        return None
    h = m.group(1).lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def _srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def oklch_to_hex(lightness: float, chroma: float, hue_deg: float) -> str:
    """Convert an OKLCH color to an sRGB hex string (approximate, for seeding
    the color picker so it shows the current accent)."""
    h = math.radians(hue_deg)
    a = chroma * math.cos(h)
    b = chroma * math.sin(h)
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return "#" + "".join(
        f"{round(_srgb(x) * 255):02x}" for x in (r, g, bl)
    )


def contrast_hex(hex_color: str) -> str:
    """Readable text color (#ffffff or a near-black) for text on the accent."""
    h = normalize_hex(hex_color) or "#000000"
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))
    # perceived luminance (sRGB, simple)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1c1917" if lum > 0.6 else "#ffffff"
