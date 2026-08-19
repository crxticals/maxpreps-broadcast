"""Service layer: routes through ASGI, envelopes, error codes, SSE, watcher."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from maxpreps_broadcast.client import MaxPrepsClient
from maxpreps_broadcast.config import Settings
from maxpreps_broadcast.service.app import Watcher, create_app
from maxpreps_broadcast.service.sse import LiveBroker
from tests.conftest import load_fixture


@pytest.fixture()
async def seeded_client(settings):
    """Offline client with schedule + roster fixtures pre-seeded on disk."""
    settings.offline = True
    client = MaxPrepsClient(settings)
    sched_key = "sched:ca/irvine/northwood-timberwolves/football/fall/schedule"
    roster_key = "roster:ca/irvine/northwood-timberwolves/football/fall/roster"
    client.disk.set(sched_key, {"tier": "json_api", "payload": load_fixture("schedule_gems_full.json")},
                    ttl=3600, meta={"source_tier": "json_api"})
    client.disk.set(roster_key, {"tier": "json_api", "payload": load_fixture("roster_wire_myers_park.json")},
                    ttl=3600, meta={"source_tier": "json_api"})
    yield client
    await client.aclose()


@pytest.fixture()
async def api(seeded_client):
    app = create_app(seeded_client.settings, client=seeded_client)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


class TestRoutes:
    async def test_healthz_shape(self, api):
        body = (await api.get("/healthz")).json()
        assert body["status"] in {"ok", "degraded"}
        assert body["offline"] is True
        assert "breakers" in body and "surfaces" in body
        assert body["watch"] == {"enabled": False}

    async def test_schedule_envelope(self, api):
        body = (await api.get("/schedule")).json()
        assert body["source_tier"] == "json_api"
        assert body["cache_state"] in {"cached", "fresh", "stale"}
        assert body["data"]["team"]["school_name"] == "Northwood"
        byes = [c for c in body["data"]["contests"] if c["contest_type"] == "bye"]
        assert len(byes) == 1                      # include_byes defaults on

    async def test_roster_route(self, api):
        body = (await api.get("/roster")).json()
        assert len(body["data"]["entries"]) == 63

    async def test_live_route(self, api):
        body = (await api.get("/live")).json()
        assert body["data"]["status"] in {"scheduled", "pregame", "in_progress", "final"}

    async def test_search_validation(self, api):
        assert (await api.get("/search", params={"q": "x"})).status_code == 422

    async def test_metrics_prometheus_text(self, api):
        resp = await api.get("/metrics")
        assert resp.headers["content-type"].startswith("text/plain")

    async def test_unconfigured_primary_is_409(self, settings, tmp_path):
        bare = Settings(cache=settings.cache, export=settings.export)
        bare.offline = True
        client = MaxPrepsClient(bare)
        app = create_app(bare, client=client)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                assert (await http.get("/live")).status_code == 409
        await client.aclose()


class TestSse:
    async def test_new_subscriber_gets_current_state_immediately(self):
        broker = LiveBroker(heartbeat_seconds=0.2)
        await broker.publish("changes", {"home_score": 7})
        stream = broker.subscribe()
        first = await asyncio.wait_for(anext(stream), timeout=1)
        assert "event: changes" in first
        assert json.loads(first.split("data: ")[1].strip()) == {"home_score": 7}
        await stream.aclose()

    async def test_heartbeat_when_idle(self):
        broker = LiveBroker(heartbeat_seconds=0.05)
        stream = broker.subscribe()
        beat = await asyncio.wait_for(anext(stream), timeout=1)
        assert beat.startswith(": heartbeat")
        await stream.aclose()

    async def test_publish_wakes_subscriber(self):
        broker = LiveBroker(heartbeat_seconds=5.0)
        stream = broker.subscribe()

        async def consume():
            return await anext(stream)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        await broker.publish("changes", {"n": 1})
        message = await asyncio.wait_for(task, timeout=1)
        assert "event: changes" in message
        await stream.aclose()

    async def test_laggard_skips_to_newest(self):
        broker = LiveBroker(heartbeat_seconds=5.0)
        await broker.publish("changes", {"n": 1})
        stream = broker.subscribe()
        await anext(stream)                        # consume current
        for n in range(2, 6):
            await broker.publish("changes", {"n": n})
        newest = await asyncio.wait_for(anext(stream), timeout=1)
        assert json.loads(newest.split("data: ")[1].strip()) == {"n": 5}
        await stream.aclose()


class TestWatcher:
    async def test_tick_writes_atomic_views_and_publishes_changes(self, seeded_client, tmp_path):
        broker = LiveBroker()
        out = tmp_path / "broadcast"
        watcher = Watcher(seeded_client, broker, out_dir=out, interval_seconds=0.1)
        await watcher.tick()
        assert (out / "live.json").exists()
        assert (out / "live.mgjson").exists()
        first = json.loads((out / "live.json").read_text())
        assert first["home_name"] == "Northwood"

        # First tick publishes the initial state as a change set.
        stream = broker.subscribe()
        event = await asyncio.wait_for(anext(stream), timeout=1)
        assert "event: changes" in event
        await stream.aclose()

        # Second tick with identical data: files rewritten, no new publish.
        sequence_before = broker._sequence
        await watcher.tick()
        assert broker._sequence == sequence_before
        assert watcher.errors == 0

    async def test_tick_errors_never_kill_the_loop(self, seeded_client, tmp_path):
        broker = LiveBroker()
        watcher = Watcher(seeded_client, broker, out_dir=tmp_path, interval_seconds=0.01)

        original = seeded_client.get_scoretracker
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return await original(*a, **k)

        seeded_client.get_scoretracker = flaky  # type: ignore[method-assign]
        watcher.start()
        await asyncio.sleep(0.2)
        await watcher.stop()
        assert watcher.errors >= 1
        assert watcher.ticks > watcher.errors      # it kept going
        assert (tmp_path / "live.json").exists()   # later ticks still wrote
