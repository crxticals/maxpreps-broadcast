"""robots.txt enforcement.

Cached per host with a TTL.  Disallowed paths fail closed
(``RobotsDisallowedError``) — no override.  If robots.txt itself is
unreachable we proceed (standard practice) but log it, and a 4xx "no file"
means allow-all per the protocol.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from maxpreps_broadcast.errors import RobotsDisallowedError
from maxpreps_broadcast.obs import get_logger

log = get_logger(__name__)


@dataclass
class _Entry:
    parser: urllib.robotparser.RobotFileParser | None  # None = fetch failed → allow, warn
    fetched_at: float


class RobotsGate:
    def __init__(self, *, user_agent: str, ttl_seconds: float = 12 * 3600) -> None:
        self.user_agent = user_agent
        self.ttl = ttl_seconds
        self._cache: dict[str, _Entry] = {}

    async def check(self, url: str, client: httpx.AsyncClient) -> None:
        parts = urlsplit(url)
        host = parts.netloc
        entry = self._cache.get(host)
        now = time.monotonic()
        if entry is None or now - entry.fetched_at > self.ttl:
            entry = await self._fetch(parts.scheme, host, client)
            self._cache[host] = entry
        if entry.parser is None:
            return
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        if not entry.parser.can_fetch(self.user_agent, target or "/"):
            raise RobotsDisallowedError(url)

    async def _fetch(self, scheme: str, host: str, client: httpx.AsyncClient) -> _Entry:
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            resp = await client.get(robots_url, timeout=8.0)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable; proceeding without rules", host=host, error=str(exc))
            return _Entry(parser=None, fetched_at=time.monotonic())
        parser = urllib.robotparser.RobotFileParser()
        if resp.status_code >= 500:
            # Server error: be conservative — treat as disallow-all until it recovers.
            parser.disallow_all = True  # type: ignore[attr-defined]
            log.warning("robots.txt 5xx; failing closed until it recovers",
                        host=host, status=resp.status_code)
        elif resp.status_code >= 400:
            parser.allow_all = True  # type: ignore[attr-defined]  # no robots file → everything allowed
        else:
            parser.parse(resp.text.splitlines())
        return _Entry(parser=parser, fetched_at=time.monotonic())

    def snapshot(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for host, entry in self._cache.items():
            if entry.parser is None:
                out[host] = "unavailable(allow)"
            elif entry.parser.disallow_all:  # type: ignore[attr-defined]
                out[host] = "disallow_all"
            elif entry.parser.allow_all:  # type: ignore[attr-defined]
                out[host] = "allow_all"
            else:
                out[host] = "rules_loaded"
        return out
