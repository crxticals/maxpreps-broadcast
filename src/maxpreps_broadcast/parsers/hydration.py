"""Tier-2 parser: the ``__NEXT_DATA__`` hydration blob embedded in page HTML.

Same payload as the ``_next/data`` route (tier 1), extracted from the page
itself.  Used when the JSON route 404s (stale buildId that failed to
self-heal) or is blocked.  Also exposes buildId extraction, since the blob
carries it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from maxpreps_broadcast.errors import SchemaDriftError

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
# The buildId string on the homepage carries a trailing "\n" *inside* the JSON
# string, so the escape must terminate the capture.
_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"\\]+)')


def extract_next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise SchemaDriftError("no __NEXT_DATA__ blob in page HTML", path="html.__NEXT_DATA__")
    try:
        blob = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise SchemaDriftError(f"__NEXT_DATA__ is not valid JSON: {exc}", path="html.__NEXT_DATA__") from exc
    if not isinstance(blob, dict):
        raise SchemaDriftError("__NEXT_DATA__ is not an object", path="html.__NEXT_DATA__")
    return blob


def page_props_from_html(html: str) -> dict[str, Any]:
    """→ ``{"pageProps": {...}}`` shaped exactly like a ``_next/data`` response."""
    blob = extract_next_data(html)
    props = blob.get("props")
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    if not isinstance(page_props, dict):
        raise SchemaDriftError("__NEXT_DATA__ has no props.pageProps", path="props.pageProps")
    return {"pageProps": page_props}


def extract_build_id(html: str) -> str | None:
    m = _BUILD_ID_RE.search(html)
    if not m:
        return None
    return m.group(1).strip()
