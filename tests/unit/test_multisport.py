"""Multi-sport fan-out: failure isolation, per-sport paths, and the API."""

from __future__ import annotations

import httpx
import pytest

from maxpreps_broadcast import sports
from maxpreps_broadcast.client import MaxPrepsClient
from maxpreps_broadcast.config import SportsConfig
from maxpreps_broadcast.errors import MaxPrepsError
from maxpreps_broadcast.service.app import create_app
from tests.conftest import load_fixture

FIXTURE = "schedule_gems_full.json"


def seed(client: MaxPrepsClient, *sport_keys: str) -> None:
    """Pre-seed the offline cache for each sport at its real URL path."""
    for key in sport_keys:
        entry = sports.get(key)
        path = "/".join(
            ["ca", "irvine", "northwood-timberwolves", *entry.path_segments(), "schedule"]
        )
        client.disk.set(
            f"sched:{path}",
            {"tier": "json_api", "payload": load_fixture(FIXTURE)},
            ttl=3600,
            meta={"source_tier": "json_api"},
        )


@pytest.fixture()
async def multi_client(settings):
    settings.offline = True
    settings.sports = SportsConfig(active=["football", "girls-volleyball", "boys-water-polo"])
    client = MaxPrepsClient(settings)
    yield client
    await client.aclose()


class TestFanOut:
    async def test_covers_every_active_sport(self, multi_client):
        seed(multi_client, "football", "girls-volleyball", "boys-water-polo")
        results = await multi_client.get_schedules()
        assert list(results) == ["football", "girls-volleyball", "boys-water-polo"]
        assert all(not isinstance(r, MaxPrepsError) for r in results.values())

    async def test_one_dark_sport_does_not_lose_the_others(self, multi_client):
        """The whole point of one call: five good sports still arrive when the
        sixth has nothing published."""
        seed(multi_client, "football", "girls-volleyball")  # water polo left unseeded
        results = await multi_client.get_schedules()
        assert not isinstance(results["football"], MaxPrepsError)
        assert not isinstance(results["girls-volleyball"], MaxPrepsError)
        assert isinstance(results["boys-water-polo"], MaxPrepsError)

    async def test_each_sport_is_fetched_at_its_own_path(self, multi_client):
        """A shared cache key across sports would serve football's schedule as
        volleyball's — the failure mode that puts the wrong data on air."""
        seed(multi_client, "football")
        results = await multi_client.get_schedules()
        assert not isinstance(results["football"], MaxPrepsError)
        assert isinstance(results["girls-volleyball"], MaxPrepsError)

    async def test_explicit_selection_overrides_the_active_list(self, multi_client):
        seed(multi_client, "football")
        results = await multi_client.get_schedules(["football"])
        assert list(results) == ["football"]

    async def test_result_order_follows_the_rotation_order(self, multi_client):
        seed(multi_client, "football", "girls-volleyball", "boys-water-polo")
        multi_client.settings.sports = SportsConfig(
            active=["boys-water-polo", "football", "girls-volleyball"]
        )
        results = await multi_client.get_schedules()
        assert list(results) == ["boys-water-polo", "football", "girls-volleyball"]


class TestSportsApi:
    @pytest.fixture()
    async def api(self, multi_client):
        app = create_app(multi_client.settings, client=multi_client, persist_selection=False)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                yield http

    async def test_catalogue_is_enough_to_render_a_picker(self, api):
        body = (await api.get("/sports")).json()
        assert body["max_active"] == sports.MAX_ACTIVE_SPORTS
        assert set(body["seasons"]) == {"fall", "winter", "spring"}
        for season in body["seasons"].values():
            assert season["preset"] and season["sports"]
        assert [s["key"] for s in body["active"]] == [
            "football", "girls-volleyball", "boys-water-polo"
        ]

    async def test_put_replaces_the_selection(self, api):
        resp = await api.put("/sports/active", json={"sports": ["Girls Volleyball", "football"]})
        assert resp.status_code == 200
        assert [s["key"] for s in resp.json()["active"]] == ["girls-volleyball", "football"]

    async def test_unknown_sport_is_422_and_changes_nothing(self, api):
        before = (await api.get("/sports")).json()["active"]
        resp = await api.put("/sports/active", json={"sports": ["quidditch"]})
        assert resp.status_code == 422
        assert (await api.get("/sports")).json()["active"] == before

    async def test_over_the_cap_is_422_and_changes_nothing(self, api):
        before = (await api.get("/sports")).json()["active"]
        resp = await api.put(
            "/sports/active",
            json={"sports": ["football", "baseball", "softball", "wrestling",
                             "boys-golf", "boys-tennis", "swimming"]},
        )
        assert resp.status_code == 422
        assert (await api.get("/sports")).json()["active"] == before

    async def test_schedules_route_reports_failures_inline(self, api, multi_client):
        seed(multi_client, "football")
        body = (await api.get("/schedules")).json()
        assert "football" in body["ok"]
        assert "girls-volleyball" in body["failed"]
        assert "error" in body["sports"]["girls-volleyball"]


class TestByeSynthesisIsSportAware:
    async def test_weekly_sport_gets_byes(self, multi_client):
        seed(multi_client, "football")
        resp = await multi_client.get_team_schedule(sport="football", include_byes=True)
        assert any(c.is_synthesized for c in resp.data.contests)
        assert all(c.week_index is not None for c in resp.data.contests)

    async def test_non_weekly_sport_gets_no_phantom_byes(self, multi_client):
        """A volleyball team playing three times in a week has no bye; inventing
        one would put a phantom row on air."""
        seed(multi_client, "girls-volleyball")
        resp = await multi_client.get_team_schedule(sport="girls-volleyball", include_byes=True)
        assert not any(c.is_synthesized for c in resp.data.contests)
