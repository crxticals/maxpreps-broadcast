"""Client integration against a fully mocked network (respx):

buildId self-heal, tier fallback, conditional GET → 304 → fresh, breaker →
last-known-good with honest staleness flags, search ranking, ambiguity.
"""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from maxpreps_broadcast.client import BASE, MaxPrepsClient
from maxpreps_broadcast.errors import AmbiguousTeamError
from tests.conftest import load_fixture

HOME_HTML = ('<script id="__NEXT_DATA__" type="application/json">'
             '{"props":{"pageProps":{}},"buildId":"BUILD1"}</script>')
HOME_HTML2 = HOME_HTML.replace("BUILD1", "BUILD2")
SCHED_PATH = "ca/irvine/northwood-timberwolves/football/fall/schedule"


def gems() -> dict:
    return load_fixture("schedule_gems_trimmed.json")


@pytest.fixture()
async def client(settings):
    settings.http.respect_robots = False        # robots gate has its own tests
    settings.http.requests_per_second = 1000.0  # no pacing in tests
    async with MaxPrepsClient(settings) as c:
        yield c


@respx.mock
async def test_tier1_json_api_happy_path(client):
    respx.get(f"{BASE}/").mock(return_value=Response(200, text=HOME_HTML))
    respx.get(f"{BASE}/_next/data/BUILD1/{SCHED_PATH}.json").mock(
        return_value=Response(200, json=gems())
    )
    resp = await client.get_team_schedule()
    assert resp.source_tier == "json_api"
    assert resp.data.team.school_name == "Northwood"


@respx.mock
async def test_build_id_self_heals_on_404(client):
    respx.get(f"{BASE}/").mock(side_effect=[Response(200, text=HOME_HTML),
                                            Response(200, text=HOME_HTML2)])
    respx.get(f"{BASE}/_next/data/BUILD1/{SCHED_PATH}.json").mock(return_value=Response(404))
    route2 = respx.get(f"{BASE}/_next/data/BUILD2/{SCHED_PATH}.json").mock(
        return_value=Response(200, json=gems())
    )
    resp = await client.get_team_schedule()
    assert route2.called
    assert resp.source_tier == "json_api"


@respx.mock
async def test_falls_back_to_hydration_tier(client):
    respx.get(f"{BASE}/").mock(return_value=Response(200, text=HOME_HTML))
    # Data route hard-fails on both buildIds → page HTML with hydration.
    respx.get(url__regex=rf"{BASE}/_next/data/.*").mock(return_value=Response(404))
    wire = load_fixture("schedule_wire_myers_park.json")
    page = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": wire["pageProps"]}, "buildId": "BUILD1"})
            + "</script>")
    respx.get(f"{BASE}/{SCHED_PATH}/").mock(return_value=Response(200, text=page))
    resp = await client.get_team_schedule()
    assert resp.source_tier == "hydration"
    assert resp.data.team.school_name  # wire capture parses through tier 2


@respx.mock
async def test_conditional_get_304_touches_and_serves_fresh(client):
    respx.mock.get(f"{BASE}/").mock(return_value=Response(200, text=HOME_HTML))
    route = respx.mock.get(f"{BASE}/_next/data/BUILD1/{SCHED_PATH}.json")
    route.mock(side_effect=[
        Response(200, json=gems(), headers={"ETag": 'W/"v1"'}),
        Response(304),
    ])
    first = await client.get_team_schedule()
    assert first.cache_state == "fresh"

    # Expire the entry without wall-clock time passing, then read again:
    # the revalidation sends If-None-Match, gets a 304, and the entry is
    # fresh again without a byte of body re-downloaded.
    key = f"sched:{SCHED_PATH}"
    client.disk.touch(key, stored_at=1.0)
    client.memory.delete(key)
    second = await client.get_team_schedule()
    assert second.cache_state == "stale"          # served instantly
    await client.fetcher.wait_for_revalidations() # 304 lands behind the scenes
    third = await client.get_team_schedule()
    assert third.cache_state == "cached"
    assert route.call_count == 2
    assert third.data.team.school_name == "Northwood"


@respx.mock
async def test_breaker_opens_then_lkg_serves_the_show(client):
    respx.mock.get(f"{BASE}/").mock(return_value=Response(200, text=HOME_HTML))
    data_route = respx.mock.get(f"{BASE}/_next/data/BUILD1/{SCHED_PATH}.json")
    data_route.mock(return_value=Response(200, json=gems()))
    first = await client.get_team_schedule()
    assert first.cache_state == "fresh"

    # The site melts down.  Stale reads keep serving while background
    # revalidations fail; those failures trip the breaker.
    data_route.mock(return_value=Response(500))
    respx.mock.get(f"{BASE}/{SCHED_PATH}/").mock(return_value=Response(500))
    object.__setattr__(client.transport.retry_policy, "max_retries", 0)
    key = f"sched:{SCHED_PATH}"
    for _ in range(6):
        client.disk.touch(key, stored_at=1.0)
        client.memory.delete(key)
        resp = await client.get_team_schedule()
        assert resp.cache_state == "stale"        # audience never sees an error
        assert resp.stale is True
        await client.fetcher.wait_for_revalidations()
    assert "open" in set(client.transport.breakers.snapshot().values())

    # Cache wiped (new machine, cleared dir) but the LKG snapshot survives:
    # the breaker fails fast and the snapshot carries the broadcast.
    client.disk.delete(key)
    client.memory.delete(key)
    resp = await client.get_team_schedule()
    assert resp.cache_state == "last_known_good"
    assert resp.stale is True
    assert resp.data.team.school_name == "Northwood"


@respx.mock
async def test_search_ranking_prefers_exact_then_prefix(client):
    respx.get(f"{BASE}/").mock(return_value=Response(200, text=HOME_HTML))
    results = {
        "pageProps": {
            "initialSchoolResults": [
                {"name": "Northwood High School", "city": "X", "state": "SC",
                 "canonicalUrl": "https://www.maxpreps.com/sc/x/northwood-hs/"},
                {"name": "Northwood", "city": "Irvine", "state": "CA",
                 "canonicalUrl": "https://www.maxpreps.com/ca/irvine/northwood-timberwolves/"},
                {"name": "North Woods", "city": "Y", "state": "MN",
                 "canonicalUrl": "https://www.maxpreps.com/mn/y/north-woods/"},
            ]
        }
    }
    respx.get(url__regex=rf"{BASE}/_next/data/BUILD1/search\.json.*").mock(
        return_value=Response(200, json=results)
    )
    resp = await client.search_schools("northwood")
    assert resp.data[0].name == "Northwood"          # exact match first
    assert resp.data[0].city == "Irvine"

    ref = await client.resolve_school("northwood")
    assert ref.city == "irvine"

    with pytest.raises(AmbiguousTeamError) as excinfo:
        await client.resolve_school("north")
    assert len(excinfo.value.candidates) >= 2
