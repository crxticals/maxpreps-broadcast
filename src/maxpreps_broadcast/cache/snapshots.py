"""Last-known-good persistence.

Every successful parse writes a durable snapshot.  When a fetch fails
completely (network down, breaker open, MaxPreps 5xx-ing mid-game), the
snapshot is served with ``cache_state="last_known_good"``, ``stale=True`` and
an accurate ``data_age_seconds`` — the scorebug keeps rendering.
"""

from __future__ import annotations

from typing import Any

from maxpreps_broadcast.cache.disk import DiskCache, DiskEntry

_PREFIX = "lkg:"
_NEVER_EXPIRES = 10 * 365 * 24 * 3600.0


class SnapshotStore:
    def __init__(self, disk: DiskCache) -> None:
        self._disk = disk

    def save(self, key: str, value: Any, *, meta: dict[str, Any] | None = None,
             stored_at: float | None = None) -> None:
        self._disk.set(_PREFIX + key, value, ttl=_NEVER_EXPIRES, meta=meta, stored_at=stored_at)

    def load(self, key: str) -> DiskEntry | None:
        return self._disk.get(_PREFIX + key)

    def keys(self) -> list[str]:
        return [k.removeprefix(_PREFIX) for k in self._disk.keys(_PREFIX)]
