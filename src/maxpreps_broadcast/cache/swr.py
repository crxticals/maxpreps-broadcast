"""Stale-while-revalidate orchestration across both cache tiers.

Resolution order for ``get``:

1. memory hit, fresh          → ``cached`` (no network)
2. disk hit, fresh            → promote to memory → ``cached``
3. any hit, stale             → return it *immediately* as ``stale`` and
                                revalidate in the background (deduped per key)
4. miss                       → fetch now; on success write memory + disk +
                                last-known-good snapshot → ``fresh``
5. fetch failed / breaker open → serve the LKG snapshot as
                                ``last_known_good`` (accurate age); only if
                                even that is absent does the error propagate

``offline=True`` skips fetching entirely and serves whatever exists.
Never block the render; never return nothing when we have *something*.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from maxpreps_broadcast.cache.disk import DiskCache
from maxpreps_broadcast.cache.memory import MemoryCache
from maxpreps_broadcast.cache.snapshots import SnapshotStore
from maxpreps_broadcast.errors import CacheMissError, MaxPrepsError, OfflineMissError
from maxpreps_broadcast.models.envelope import CacheState
from maxpreps_broadcast.obs import METRICS, get_logger

log = get_logger(__name__)

# A fetcher returns (payload, meta).  meta typically carries source_tier,
# etag, last_modified.  It may receive prior validators for conditional GETs.
FetchFn = Callable[[str | None, str | None], Awaitable[tuple[Any, dict[str, Any]]]]


@dataclass
class CacheHit:
    value: Any
    state: CacheState
    stored_at: float
    meta: dict[str, Any]

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.stored_at)


class NotModified(Exception):
    """Raised by a fetcher when the origin returned 304 for our validators."""


class CachedFetcher:
    def __init__(
        self,
        memory: MemoryCache,
        disk: DiskCache,
        snapshots: SnapshotStore,
        *,
        offline: bool = False,
    ) -> None:
        self.memory = memory
        self.disk = disk
        self.snapshots = snapshots
        self.offline = offline
        self._inflight: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ get

    async def get(self, key: str, *, ttl: float, fetch: FetchFn) -> CacheHit:
        now = time.time()

        mem = self.memory.get(key)
        if mem is not None and mem.is_fresh(now):
            METRICS.inc("cache_hits_total", tier="memory")
            return CacheHit(mem.value, "cached", mem.stored_at, mem.meta)

        entry = self.disk.get(key)
        if entry is not None and entry.is_fresh(now):
            METRICS.inc("cache_hits_total", tier="disk")
            self.memory.set(key, entry.value, ttl=ttl, stored_at=entry.stored_at, meta=entry.meta)
            return CacheHit(entry.value, "cached", entry.stored_at, entry.meta)

        stale_value: CacheHit | None = None
        if entry is not None:
            stale_value = CacheHit(entry.value, "stale", entry.stored_at, entry.meta)
        elif mem is not None:
            stale_value = CacheHit(mem.value, "stale", mem.stored_at, mem.meta)

        if self.offline:
            if stale_value is not None:
                METRICS.inc("cache_hits_total", tier="offline_stale")
                return stale_value
            snap = self.snapshots.load(key)
            if snap is not None:
                METRICS.inc("cache_hits_total", tier="offline_lkg")
                return CacheHit(snap.value, "last_known_good", snap.stored_at, snap.meta)
            raise OfflineMissError(f"offline mode and nothing cached for {key}")

        if stale_value is not None:
            # Stale-while-revalidate: hand back the stale value now, refresh behind.
            METRICS.inc("cache_stale_served_total")
            self._spawn_revalidate(key, ttl=ttl, fetch=fetch)
            return stale_value

        METRICS.inc("cache_misses_total")
        try:
            return await self._fetch_and_store(key, ttl=ttl, fetch=fetch)
        except MaxPrepsError as exc:
            snap = self.snapshots.load(key)
            if snap is not None:
                METRICS.inc("cache_hits_total", tier="lkg")
                log.warning("fetch failed; serving last-known-good", key=key, error=str(exc))
                return CacheHit(snap.value, "last_known_good", snap.stored_at, snap.meta)
            raise

    # --------------------------------------------------------------- helpers

    async def _fetch_and_store(self, key: str, *, ttl: float, fetch: FetchFn) -> CacheHit:
        prior = self.disk.get(key)
        etag = prior.etag if prior else None
        last_modified = prior.last_modified if prior else None
        try:
            value, meta = await fetch(etag, last_modified)
        except NotModified:
            now = time.time()
            self.disk.touch(key, stored_at=now)
            assert prior is not None
            self.memory.set(key, prior.value, ttl=ttl, stored_at=now, meta=prior.meta)
            METRICS.inc("cache_revalidated_304_total")
            return CacheHit(prior.value, "fresh", now, prior.meta)
        now = time.time()
        self.disk.set(
            key, value, ttl=ttl,
            etag=meta.get("etag"), last_modified=meta.get("last_modified"),
            stored_at=now, meta=meta,
        )
        self.memory.set(key, value, ttl=ttl, stored_at=now, meta=meta)
        self.snapshots.save(key, value, meta=meta, stored_at=now)
        return CacheHit(value, "fresh", now, meta)

    def _spawn_revalidate(self, key: str, *, ttl: float, fetch: FetchFn) -> None:
        if key in self._inflight and not self._inflight[key].done():
            return

        async def _revalidate() -> None:
            try:
                await self._fetch_and_store(key, ttl=ttl, fetch=fetch)
                log.debug("background revalidation complete", key=key)
            except Exception as exc:
                log.warning("background revalidation failed", key=key, error=str(exc))
            finally:
                self._inflight.pop(key, None)

        with contextlib.suppress(RuntimeError):
            # No running loop (sync facade edge) — skip background refresh.
            self._inflight[key] = asyncio.get_running_loop().create_task(_revalidate())

    async def wait_for_revalidations(self) -> None:
        """Test/shutdown helper: drain in-flight background refreshes."""
        tasks = [t for t in self._inflight.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def peek_stored_at(self, key: str) -> float | None:
        entry = self.disk.get(key)
        if entry:
            return entry.stored_at
        snap = self.snapshots.load(key)
        return snap.stored_at if snap else None

    def require_any(self, key: str) -> CacheHit:
        """Any tier, freshness ignored — used by /healthz introspection."""
        mem = self.memory.get(key)
        if mem:
            return CacheHit(mem.value, "cached", mem.stored_at, mem.meta)
        entry = self.disk.get(key)
        if entry:
            return CacheHit(entry.value, "cached", entry.stored_at, entry.meta)
        snap = self.snapshots.load(key)
        if snap:
            return CacheHit(snap.value, "last_known_good", snap.stored_at, snap.meta)
        raise CacheMissError(key)
