"""One end-to-end view test: fixtures → flat views → every export format.

This is the AE-facing surface.  If this passes, the files After Effects
reads are shaped right; everything else is plumbing."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from maxpreps_broadcast.config import Settings
from maxpreps_broadcast.export import csv_out, json_out, xml_out
from maxpreps_broadcast.export.mgjson import mgjson_for_live, validate_mgjson, write_mgjson
from maxpreps_broadcast.models import ParseMode, Response
from maxpreps_broadcast.parsers import json_api

FIXTURES = Path(__file__).parent / "fixtures"


def _envelope(data, tier="json_api"):
    return Response.wrap(
        data,
        fetched_at=datetime.now(UTC),
        source_tier=tier,
        cache_state="fresh",
        warnings=[],
        request_id="test",
    )


def test_fixture_to_every_format(tmp_path: Path):
    settings = Settings()
    payload = json.loads((FIXTURES / "schedule_gems_full.json").read_text())
    schedule, ctx = json_api.parse_schedule(payload, ParseMode.LENIENT)
    json_api.synthesize_byes(schedule, ctx)
    roster_payload = json.loads((FIXTURES / "roster_wire_myers_park.json").read_text())
    roster, _ = json_api.parse_roster(roster_payload, ParseMode.LENIENT)

    contest = schedule.next_contest(now=datetime(2026, 8, 20, tzinfo=UTC))
    state = json_api.build_score_state(contest, schedule.team, now=datetime(2026, 8, 20, tzinfo=UTC), ctx=ctx)

    live = json_out.live_view(_envelope(state), settings)  # no assets_dir → no network
    sched = json_out.schedule_view(_envelope(schedule), settings)
    roster_v = json_out.roster_view(_envelope(roster), settings)

    # -- flatness: AE expressions only reach one level deep
    for view in (live,):
        assert all(not isinstance(v, dict) for v in view.values())
    assert all(not isinstance(v, (dict, list)) for row in sched["games"] for v in row.values())

    # -- the strings a scorebug actually shows
    assert live["kickoff_display"] == "FRI AUG 21 · 7:00 PM"
    assert "—" in live["score_line"] and live["home_score"] == ""  # pre-game dashes
    assert live["home_primary_hex"] == "#022C66"
    assert live["home_primary_text"] == "#FFFFFF"
    assert 0.0 <= live["home_primary_r01"] <= 1.0

    # -- schedule rows: bye present, venue words, league flags
    bye_rows = [r for r in sched["games"] if r["home_away"] == "BYE"]
    assert len(bye_rows) == 1 and bye_rows[0]["week"] == 5
    assert {r["vs_at"] for r in sched["games"] if r["vs_at"]} <= {"VS", "AT"}

    # -- roster rows
    assert roster_v["player_count"] == 63
    assert all(r["jersey_padded"] for r in roster_v["players"] if r["jersey"])

    # -- every writer produces a parseable file
    json_out.write_views(tmp_path, settings, live=_envelope(state),
                         schedule=_envelope(schedule), roster=_envelope(roster))
    assert json.loads((tmp_path / "live.json").read_text())["kickoff_display"]

    write_mgjson(tmp_path / "live.mgjson", mgjson_for_live(live))
    doc = json.loads((tmp_path / "live.mgjson").read_text())
    assert validate_mgjson(doc) == []

    csv_out.write_csv(tmp_path / "schedule.csv", sched["games"])
    header = (tmp_path / "schedule.csv").read_text().splitlines()[0]
    assert "opponent" in header and "week" in header

    xml_out.write_xml(tmp_path / "live.xml", live)
    root = ET.parse(tmp_path / "live.xml").getroot()
    fields = {el.get("name") for el in root.iter("field")}
    assert "score_line" in fields
