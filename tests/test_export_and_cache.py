"""Export and resilience tests — compact.

The export half guards what AE actually consumes (atomic files, valid mgJSON,
budgets, WCAG picks).  The resilience half guards broadcast night: stale
cache served instantly, last-known-good on network death, offline mode.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maxpreps_broadcast.cache.disk import DiskCache
from maxpreps_broadcast.cache.memory import MemoryCache
from maxpreps_broadcast.cache.snapshots import SnapshotStore
from maxpreps_broadcast.cache.swr import CachedFetcher, OfflineMissError
from maxpreps_broadcast.errors import FetchError
from maxpreps_broadcast.export.atomic import atomic_write_json, atomic_write_text
from maxpreps_broadcast.export.colors import ColorEntry, contrast_text_for
from maxpreps_broadcast.export.mapping import TemplateMapping, render_mapping, resolve_path
from maxpreps_broadcast.export.mgjson import (
    DynamicSeries,
    MgjsonBuilder,
    format_utc,
    validate_mgjson,
)
from maxpreps_broadcast.export.strings import down_distance_display, kickoff_display, score_line
from maxpreps_broadcast.parsers.normalize import fit_budget

# ------------------------------------------------------------------- atomic


class TestAtomic:
    def test_replace_not_append(self, tmp_path: Path):
        target = tmp_path / "live.json"
        atomic_write_json(target, {"v": 1})
        atomic_write_json(target, {"v": 2})
        assert json.loads(target.read_text()) == {"v": 2}
        # no temp litter left behind
        assert list(tmp_path.iterdir()) == [target]

    def test_crash_leaves_old_file_intact(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "live.json"
        atomic_write_text(target, "OLD COMPLETE FILE")
        import os as _os
        real_replace = _os.replace

        def exploding_replace(src, dst):
            raise OSError("simulated crash at swap")

        monkeypatch.setattr("os.replace", exploding_replace)
        with pytest.raises(OSError):
            atomic_write_text(target, "NEW PARTIAL")
        monkeypatch.setattr("os.replace", real_replace)
        assert target.read_text() == "OLD COMPLETE FILE"
        assert [p for p in tmp_path.iterdir() if p != target] == []  # tmp cleaned up


# ------------------------------------------------------------------- mgjson


class TestMgjson:
    def _builder(self) -> MgjsonBuilder:
        b = MgjsonBuilder()
        b.add_static("home_abbr", "Home Abbr", "NW")
        b.add_static("home_score", "Home Score", 21)
        b.add_series(
            DynamicSeries(
                "home_score_t",
                "Home Score",
                [
                    (datetime(2026, 10, 16, 19, 0, tzinfo=UTC), 0.0),
                    (datetime(2026, 10, 16, 19, 30, tzinfo=UTC), 7.0),
                ],
            )
        )
        return b

    def test_valid_document(self):
        doc = self._builder().build()
        assert validate_mgjson(doc) == []
        dynamic = next(e for e in doc["dataOutline"] if e["objectType"] == "dataDynamic")
        assert dynamic["interpolation"] == "hold"  # scores must not lerp
        assert "hasExpectedFrequecyB" in dynamic  # the schema's own typo
        sample = doc["dataDynamicSamples"][0]["samples"][0]
        assert sample == {"time": "2026-10-16T19:00:00.000Z", "value": "0"}

    def test_validator_catches_corruption(self):
        doc = self._builder().build()
        doc["dataDynamicSamples"][0]["samples"].pop()  # sampleCount now lies
        assert any("sampleCount" in p for p in validate_mgjson(doc))
        doc2 = self._builder().build()
        doc2["dataDynamicSamples"][0]["samples"][0]["value"] = 0  # must be a string
        assert any("string" in p for p in validate_mgjson(doc2))

    def test_time_format(self):
        assert format_utc(datetime(2026, 8, 21, 2, 0, 0, 123000, tzinfo=UTC)) == "2026-08-21T02:00:00.123Z"


# ------------------------------------------------------------ strings/colors


class TestBroadcastStrings:
    def test_score_line(self):
        assert score_line("NW", 21, "IRV", 14) == "NW 21 — IRV 14"
        assert score_line("NW", None, "IRV", None) == "NW – — IRV –"

    def test_kickoff(self):
        local = datetime(2026, 8, 21, 19, 0)
        assert kickoff_display(local) == "FRI AUG 21 · 7:00 PM"
        assert kickoff_display(local, time_tba=True) == "FRI AUG 21 · TIME TBA"
        assert kickoff_display(None) == "DATE TBA"

    def test_down_distance(self):
        assert down_distance_display(1, 10) == "1ST & 10"
        assert down_distance_display(3, "Goal") == "3RD & GOAL"
        assert down_distance_display(None, None) == ""

    def test_budgets_never_exceeded(self):
        for budget in (4, 8, 12, 18):
            assert len(fit_budget("Orange Lutheran High School Lancers", budget)) <= budget

    def test_wcag_picks(self):
        assert contrast_text_for("#022C66") == "#FFFFFF"  # navy → white
        assert contrast_text_for("#FFD700") == "#000000"  # gold → black
        entry = ColorEntry.from_hex("#022C66")
        assert entry.rgb_255 == (2, 44, 102)
        assert entry.contrast_ratio >= 4.5  # AA at minimum for team colors


# ------------------------------------------------------------------ mapping


class TestMapping:
    def test_resolve_and_render(self):
        mapping = TemplateMapping(
            template="t", source="live",
            layers={"SCORE": "home_score", "OPP": "games.1.opponent", "MISSING": "nope"},
            defaults={"MISSING": "—"},
        )
        view = {"home_score": "21", "games": [{"opponent": "A"}, {"opponent": "B"}]}
        rendered = render_mapping(mapping, view)
        assert rendered["layers"] == {"SCORE": "21", "OPP": "B", "MISSING": "—"}
        assert rendered["missing_fields"] == ["nope"]
        assert resolve_path(view, "games.7.opponent") is None


# ------------------------------------------------------------- cache tiers


class TestCacheResilience:
    @pytest.fixture()
    def fetcher(self, tmp_path: Path):
        disk = DiskCache(tmp_path / "c.sqlite3")
        f = CachedFetcher(MemoryCache(64), disk, SnapshotStore(disk))
        yield f
        disk.close()

    async def test_fresh_then_stale_serves_immediately_and_revalidates(self, fetcher):
        calls = 0

        async def fetch(etag, last_modified):
            nonlocal calls
            calls += 1
            return {"n": calls}, {"source_tier": "json_api"}

        hit1 = await fetcher.get("k", ttl=0.01, fetch=fetch)
        assert hit1.state == "fresh" and hit1.value == {"n": 1}
        await asyncio.sleep(0.05)  # let it go stale
        hit2 = await fetcher.get("k", ttl=0.01, fetch=fetch)
        assert hit2.value == {"n": 1}  # stale value served instantly
        assert hit2.state == "stale"
        await fetcher.wait_for_revalidations()
        assert calls == 2  # background revalidation happened

    async def test_network_death_serves_last_known_good(self, fetcher):
        async def good(etag, last_modified):
            return {"score": "17-21"}, {"source_tier": "json_api"}

        async def dead(etag, last_modified):
            raise FetchError("upstream on fire")

        await fetcher.get("game", ttl=0.01, fetch=good)
        await asyncio.sleep(0.05)
        # Expire disk entry entirely by using a fresh key that only LKG has:
        hit = await fetcher.get("game", ttl=0.01, fetch=dead)
        # stale path still works; now nuke freshness and force the LKG path
        fetcher.memory.clear()
        fetcher.disk.delete("game")
        hit = await fetcher.get("game", ttl=0.01, fetch=dead)
        assert hit.state == "last_known_good"
        assert hit.value == {"score": "17-21"}  # yesterday's score beats no score

    async def test_offline_miss_is_explicit(self, tmp_path: Path):
        disk = DiskCache(tmp_path / "o.sqlite3")
        offline = CachedFetcher(MemoryCache(8), disk, SnapshotStore(disk), offline=True)

        async def never(etag, last_modified):  # pragma: no cover
            raise AssertionError("offline mode must not fetch")

        with pytest.raises(OfflineMissError):
            await offline.get("nothing", ttl=1, fetch=never)
        disk.close()
