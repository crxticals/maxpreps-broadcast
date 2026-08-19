"""Shared normalization helpers used by every parser and the export layer."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_HEIGHT_RE = re.compile(r"^\s*(\d)\s*[-'’ft.\s]+\s*(\d{1,2})\s*(?:\"|in|inches)?\s*$", re.IGNORECASE)


def norm_hex_color(value: object) -> str | None:
    """``022C66`` → ``#022C66``; 3-digit shorthand expanded; junk → None."""
    if not isinstance(value, str):
        return None
    m = _HEX_RE.match(value.strip())
    if not m:
        return None
    hex_part = m.group(1)
    if len(hex_part) == 3:
        hex_part = "".join(ch * 2 for ch in hex_part)
    return "#" + hex_part.upper()


def parse_height_to_inches(value: object, *, feet: object = None, inches: object = None) -> int | None:
    """Total inches from ``(feet, inches)`` ints or strings like ``6'2"`` / ``6-2``."""
    f = safe_int(feet)
    i = safe_int(inches)
    if f is not None and i is not None:
        total = f * 12 + i
        return total if 36 <= total <= 96 else None
    if isinstance(value, int | float):
        v = int(value)
        return v if 36 <= v <= 96 else None
    if isinstance(value, str):
        m = _HEIGHT_RE.match(value)
        if m:
            return parse_height_to_inches(None, feet=m.group(1), inches=m.group(2))
    return None


def norm_jersey(value: object) -> str | None:
    """Preserve the string exactly — ``00`` and leading zeros are real."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
    return None


def safe_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def safe_str(value: object) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return None


def strip_accents(value: str) -> str:
    """Fold accents for fuzzy matching: 'José' → 'Jose'."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def upper_display(value: str) -> str:
    """Unicode-aware upper-casing for broadcast display (handles diacritics)."""
    return unicodedata.normalize("NFC", value).upper()


def initial_last(first: str | None, last: str | None) -> str:
    """``("Jayden", "Nguyen")`` → ``J. NGUYEN``.  Degrades gracefully."""
    last_u = upper_display(last.strip()) if last and last.strip() else ""
    first_u = first.strip() if first else ""
    if first_u and last_u:
        return f"{first_u[0].upper()}. {last_u}"
    if last_u:
        return last_u
    if first_u:
        return upper_display(first_u)
    return ""


def fit_budget(value: str, budget: int) -> str:
    """Graceful truncation to a character budget.

    Prefers a word boundary; falls back to a hard cut.  No ellipsis character —
    CG fonts frequently lack it and a phantom box on air is worse than a cut.
    """
    if budget <= 0:
        return ""
    if len(value) <= budget:
        return value
    cut = value[:budget]
    if " " in cut.strip() and (space := cut.rstrip().rfind(" ")) > budget // 2:
        return cut[:space].rstrip()
    return cut.rstrip()


_ORDINAL_SUFFIX = {1: "ST", 2: "ND", 3: "RD"}


def ordinal_upper(n: int) -> str:
    """``3`` → ``3RD`` (broadcast style, upper case)."""
    if 10 <= n % 100 <= 20:
        return f"{n}TH"
    return f"{n}{_ORDINAL_SUFFIX.get(n % 10, 'TH')}"


def slugify(value: str) -> str:
    ascii_ish = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_ish.lower()).strip("-")


def acronym_from_name(name: str, *, max_len: int = 4) -> str:
    """Fallback abbreviation when neither override nor API acronym exists.
    Multi-word names take initials; single words take a 3-letter clip
    (Irvine → IRV), matching scorebug convention."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    if not words:
        return ""
    if len(words) == 1:
        return upper_display(words[0][: min(3, max_len)])
    return upper_display("".join(w[0] for w in words)[:max_len])


def pop_known(raw: dict[str, Any], known: set[str]) -> dict[str, Any]:
    """Return the unknown remainder of ``raw`` (for ``raw_extra`` preservation)."""
    return {k: v for k, v in raw.items() if k not in known}
