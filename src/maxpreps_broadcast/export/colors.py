"""Team colors and mascot assets for After Effects.

Colors ship in every form AE consumers actually use — ``#RRGGBB``, 0–255
triplets and 0.0–1.0 floats — plus a WCAG-contrast-picked text color per
background so lower thirds stay legible on any school's palette.

Mascots are downloaded once, cached, and converted GIF→PNG (AE's GIF import
is famously miserable).
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from maxpreps_broadcast.export.atomic import atomic_write_bytes
from maxpreps_broadcast.obs import get_logger

log = get_logger(__name__)

_WHITE = "#FFFFFF"
_BLACK = "#000000"


def hex_to_rgb255(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _srgb_channel_to_linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    r, g, b = (_srgb_channel_to_linear(c) for c in hex_to_rgb255(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_text_for(background: str) -> str:
    """White or black, whichever meets WCAG better on this background."""
    return _WHITE if contrast_ratio(background, _WHITE) >= contrast_ratio(background, _BLACK) else _BLACK


class ColorEntry(BaseModel):
    hex: str
    rgb_255: tuple[int, int, int]
    rgb_01: tuple[float, float, float]
    contrast_text: str
    contrast_ratio: float

    @classmethod
    def from_hex(cls, color: str) -> ColorEntry:
        rgb = hex_to_rgb255(color)
        text = contrast_text_for(color)
        return cls(
            hex=color.upper(),
            rgb_255=rgb,
            rgb_01=tuple(round(c / 255.0, 4) for c in rgb),
            contrast_text=text,
            contrast_ratio=round(contrast_ratio(color, text), 2),
        )


class TeamColorBlock(BaseModel):
    primary: ColorEntry | None = None
    secondary: ColorEntry | None = None
    tertiary: ColorEntry | None = None

    @classmethod
    def from_hexes(
        cls, color1: str | None, color2: str | None = None, color3: str | None = None
    ) -> TeamColorBlock:
        return cls(
            primary=ColorEntry.from_hex(color1) if color1 else None,
            secondary=ColorEntry.from_hex(color2) if color2 else None,
            tertiary=ColorEntry.from_hex(color3) if color3 else None,
        )

    def flat(self, prefix: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("primary", "secondary", "tertiary"):
            entry: ColorEntry | None = getattr(self, name)
            if entry is None:
                continue
            key = f"{prefix}_{name}"
            out[f"{key}_hex"] = entry.hex
            out[f"{key}_r"], out[f"{key}_g"], out[f"{key}_b"] = entry.rgb_255
            out[f"{key}_r01"], out[f"{key}_g01"], out[f"{key}_b01"] = entry.rgb_01
            out[f"{key}_text"] = entry.contrast_text
        return out


def cache_mascot(
    url: str | None,
    cache_dir: Path,
    *,
    fetch_bytes: Any = None,
    prefer_png: bool = True,
) -> Path | None:
    """Download (or reuse) a mascot image; convert GIF→PNG for AE.

    ``fetch_bytes(url) -> bytes`` is injectable; by default a plain httpx GET.
    Failures return None — a missing logo must never block a render.
    """
    if not url:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    is_gif = ".gif" in url.lower()
    final_suffix = ".png" if (prefer_png and is_gif) else Path(url.split("?")[0]).suffix or ".png"
    target = cache_dir / f"mascot_{digest}{final_suffix}"
    if target.exists():
        return target
    try:
        if fetch_bytes is None:
            import httpx

            resp = httpx.get(url, timeout=10.0, follow_redirects=True)
            resp.raise_for_status()
            raw = resp.content
        else:
            raw = fetch_bytes(url)
    except Exception as exc:
        log.warning("mascot download failed", url=url, error=str(exc))
        return None
    if prefer_png and is_gif:
        try:
            from PIL import Image

            with Image.open(BytesIO(raw)) as img:
                img.seek(0)
                buffer = BytesIO()
                img.convert("RGBA").save(buffer, format="PNG")
                raw = buffer.getvalue()
        except Exception as exc:
            log.warning("gif→png conversion failed; keeping original bytes", url=url, error=str(exc))
            target = target.with_suffix(".gif")
    atomic_write_bytes(target, raw)
    return target
