"""Cache tiers + stale-while-revalidate + last-known-good + offline."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from maxpreps_broadcast.cache.disk import DiskCache
from maxpreps_broadcast.cache.memory import MemoryCache
from maxpreps_broadcast.cache.snapshots import SnapshotStore
from maxpreps_broadcast.cache.swr import CachedFetcher, NotModified
from maxpreps_broadcast.errors import OfflineMissError, RetryableError


def make_fetcher(tmp_path: Path, *, offline: bool = False) -> tuple[CachedFetcher, DiskCache]:
    disk = DiskCache(tmp_path / "c.sqlite3")
    fetcher = CachedFetcher(MemoryCache(64), disk, SnapshotStore(disk), offline=offline)
    return fetcher, disk


def fetch_fn(payloads: list[Any]):
    calls = {"n": 0}

    async def fetch(etag, last_modified):
        calls["n"] += 1
        step = payloads.pop(0)
        if isinstance(step, Exception):
            raise step
        return step, {"source_tier": "json_api"}

    return fetch, calls


class TestSwr:
    async def test_miss_fetches_and_promotes(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path)
        fetch, calls = fetch_fn([{"v": 1}])
        hit = await fetcher.get("k", ttl=60, fetch=fetch)
        assert hit.value == {"v": 1}
        assert hit.state == "fresh"
        assert calls["n"] == 1
        # Fresh from memory on the second read — no new fetch.
        hit2 = await fetcher.get("k", ttl=60, fetch=fetch)
        assert hit2.state in {"fresh", "cached"}
        assert calls["n"] == 1
        disk.close()

    async def test_stale_served_immediately_then_revalidated(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path)
        disk.set("k", {"v": "old"}, ttl=0.01, meta={"source_tier": "json_api"})
        await asyncio.sleep(0.05)
        fetch, calls = fetch_fn([{"v": "new"}])
        hit = await fetcher.get("k", ttl=0.01, fetch=fetch)
        assert hit.value == {"v": "old"}          # stale answer now…
        assert hit.state == "stale"
        await fetcher.wait_for_revalidations()    # …fresh answer next time
        assert calls["n"] == 1
        entry = disk.get("k")
        assert entry.value == {"v": "new"}
        disk.close()

    async def test_not_modified_touches_and_serves_cached(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path)
        disk.set("k", {"v": 1}, ttl=0.01, meta={"source_tier": "json_api"}, etag='W/"abc"')
        await asyncio.sleep(0.05)
        before = disk.get("k").stored_at

        async def fetch(etag, last_modified):
            assert etag == 'W/"abc"'              # validators forwarded
            raise NotModified()

        hit = await fetcher.get("k", ttl=0.01, fetch=fetch)
        # Wait for the background revalidation to process the 304.
        await fetcher.wait_for_revalidations()
        assert disk.get("k").stored_at >= before  # stored_at refreshed
        assert hit.value == {"v": 1}
        disk.close()

    async def test_last_known_good_when_all_else_fails(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path)
        snapshots = SnapshotStore(disk)
        snapshots.save("k", {"v": "golden"}, meta={"source_tier": "json_api"})
        fetch, _calls = fetch_fn([RetryableError("network down")])
        hit = await fetcher.get("k", ttl=60, fetch=fetch)
        assert hit.value == {"v": "golden"}
        assert hit.state == "last_known_good"
        disk.close()

    async def test_error_with_no_lkg_raises(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path)
        fetch, _calls = fetch_fn([RetryableError("network down")])
        with pytest.raises(RetryableError):
            await fetcher.get("k", ttl=60, fetch=fetch)
        disk.close()

    async def test_dedupes_concurrent_revalidations(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path)
        disk.set("k", {"v": "old"}, ttl=0.01, meta={})
        await asyncio.sleep(0.05)
        slow_calls = {"n": 0}

        async def slow_fetch(etag, last_modified):
            slow_calls["n"] += 1
            await asyncio.sleep(0.05)
            return {"v": "new"}, {}

        await asyncio.gather(*(fetcher.get("k", ttl=0.01, fetch=slow_fetch) for _ in range(5)))
        await fetcher.wait_for_revalidations()
        assert slow_calls["n"] == 1               # one revalidation, not five
        disk.close()


class TestOffline:
    async def test_offline_serves_stale_without_fetching(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path, offline=True)
        disk.set("k", {"v": "old"}, ttl=0.01, meta={})
        await asyncio.sleep(0.05)

        async def must_not_run(etag, last_modified):
            raise AssertionError("offline mode fetched the network")

        hit = await fetcher.get("k", ttl=0.01, fetch=must_not_run)
        assert hit.value == {"v": "old"}
        assert hit.state in {"stale", "cached"}
        disk.close()

    async def test_offline_falls_to_lkg(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path, offline=True)
        SnapshotStore(disk).save("k", {"v": "golden"}, meta={})

        async def must_not_run(etag, last_modified):
            raise AssertionError("offline mode fetched the network")

        hit = await fetcher.get("k", ttl=60, fetch=must_not_run)
        assert hit.state == "last_known_good"
        disk.close()

    async def test_offline_total_miss_is_a_clean_error(self, tmp_path):
        fetcher, disk = make_fetcher(tmp_path, offline=True)

        async def must_not_run(etag, last_modified):
            raise AssertionError("offline mode fetched the network")

        with pytest.raises(OfflineMissError):
            await fetcher.get("nothing", ttl=60, fetch=must_not_run)
        disk.close()


class TestDisk:
    def test_round_trip_with_validators(self, tmp_path):
        disk = DiskCache(tmp_path / "c.sqlite3")
        disk.set("k", {"a": [1, 2]}, ttl=60, meta={"m": 1}, etag="E", last_modified="L")
        entry = disk.get("k")
        assert entry.value == {"a": [1, 2]}
        assert entry.etag == "E" and entry.last_modified == "L"
        assert entry.is_fresh(time.time())
        disk.close()

    def test_survives_reopen(self, tmp_path):
        path = tmp_path / "c.sqlite3"
        disk = DiskCache(path)
        disk.set("k", {"v": 1}, ttl=60, meta={})
        disk.close()
        disk2 = DiskCache(path)
        assert disk2.get("k").value == {"v": 1}
        disk2.close()

    def test_snapshot_store_never_expires(self, tmp_path):
        disk = DiskCache(tmp_path / "c.sqlite3")
        snapshots = SnapshotStore(disk)
        snapshots.save("sched:x", {"v": 1}, meta={})
        loaded = snapshots.load("sched:x")
        assert loaded is not None and loaded.value == {"v": 1}
        assert "lkg:sched:x" in disk.keys("lkg:")
        disk.close()
