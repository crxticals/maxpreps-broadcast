"""Roster, scoretracker, search, rankings, standings, hydration, timezone tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from maxpreps_broadcast.errors import SchemaDriftError
from maxpreps_broadcast.models import (
    GameStatus,
    ParseMode,
    RosterSort,
    Venue,
    sort_roster,
)
from maxpreps_broadcast.parsers import hydration, json_api
from maxpreps_broadcast.parsers.timezones import tz_for_school
from tests.conftest import load_fixture


class TestRosterWire:
    @pytest.fixture(autouse=True)
    def _parse(self):
        self.roster, self.ctx = json_api.parse_roster(load_fixture("roster_wire_myers_park.json"))

    def test_real_capture_full_parse(self):
        assert len(self.roster.entries) == 63  # 64 rows minus one isDeleted

    def test_deleted_row_filtered_with_warning(self):
        assert any(w.code == "deleted_rows" for w in self.ctx.warnings)

    def test_positions_and_grades_derived(self):
        with_positions = [e for e in self.roster.entries if e.positions]
        with_grades = [e for e in self.roster.entries if e.grade_level]
        assert len(with_positions) > 40
        assert len(with_grades) > 40

    def test_arity_drift_warns_lenient_raises_strict(self):
        shifted = load_fixture("roster_wire_shifted.json")
        _roster, ctx = json_api.parse_roster(shifted, ParseMode.LENIENT)
        assert any(w.code == "arity_drift" for w in ctx.warnings)
        with pytest.raises(SchemaDriftError):
            json_api.parse_roster(shifted, ParseMode.STRICT)


class TestJerseySort:
    """The '00' vs '0' vs None trap: numeric asc, 0 before 00 at same value?
    Convention: sort by int value then string length, blanks last."""

    def _entry(self, jersey, last="Zed"):
        from maxpreps_broadcast.models import RosterEntry

        return RosterEntry(jersey_number=jersey, first_name="A", last_name=last)

    def test_order(self):
        entries = [self._entry(j) for j in ["10", None, "0", "2", "00", "7"]]
        ordered = sort_roster(entries, RosterSort.JERSEY)
        assert [e.jersey_number for e in ordered] == ["0", "00", "2", "7", "10", None]

    def test_padding(self):
        assert self._entry("7").jersey_padded == "07"
        assert self._entry("00").jersey_padded == "00"
        assert self._entry(None).jersey_padded == "--"


class TestScoretrackerWire:
    def test_live_q3_state(self):
        schedule, ctx = json_api.parse_schedule(load_fixture("scoretracker_wire_live.json"))
        live = schedule.live_contest()
        assert live is not None
        state = json_api.build_score_state(
            live, schedule.team, now=datetime(2026, 10, 17, 3, 30, tzinfo=UTC), ctx=ctx
        )
        assert state.status is GameStatus.IN_PROGRESS
        assert state.period == 3
        assert state.period_label == "Q3"
        # Northwood away at Laguna Hills 17-21: home/away frame from is_home.
        assert state.away.name == "Northwood"
        assert state.away_score == 17
        assert state.home_score == 21

    def test_honest_nones_for_unavailable_fields(self):
        schedule, ctx = json_api.parse_schedule(load_fixture("scoretracker_wire_live.json"))
        live = schedule.live_contest()
        state = json_api.build_score_state(
            live, schedule.team, now=datetime(2026, 10, 17, 3, 30, tzinfo=UTC), ctx=ctx
        )
        # The server-rendered surface has no clock/possession/downs — never invent.
        assert state.clock is None
        assert state.possession is None
        assert state.down_and_distance is None


class TestVenueNeutralBothHome:
    def test_both_home_flags_resolve_neutral_with_warning(self):
        payload = load_fixture("schedule_gems_trimmed.json")
        game = payload["data"]["contests"][0]
        game["home_team"]["is_home"] = True
        game["away_team"]["is_home"] = True
        schedule, ctx = json_api.parse_schedule(payload)
        assert schedule.contests[0].venue is Venue.NEUTRAL
        assert any(w.code == "venue_conflict" for w in ctx.warnings)


class TestSearch:
    def test_wire_capture(self):
        schools, _ctx = json_api.parse_search(load_fixture("search_wire.json"))
        assert schools
        assert all(s.name for s in schools)

    def test_empty_results_ok(self):
        schools, _ctx = json_api.parse_search({"pageProps": {"initialSchoolResults": []}})
        assert schools == []


class TestRankings:
    def test_state_page(self):
        rankings, _ctx = json_api.parse_rankings_list(
            load_fixture("rankings_wire_page.json"), scope="state"
        )
        assert rankings.entries[0].rank == 1
        assert "Mater Dei" in rankings.entries[0].school_name
        northwood = rankings.find("Northwood")
        assert northwood is not None
        assert northwood.rank == 367
        assert northwood.rank_delta == -3
        assert northwood.previous_rank == 364

    def test_team_paths_stripped(self):
        rankings, _ = json_api.parse_rankings_list(
            load_fixture("rankings_wire_page.json"), scope="state"
        )
        assert all("/schedule" not in (e.team_path or "") for e in rankings.entries)


class TestStandings:
    def test_members(self):
        members = json_api.parse_standings_members(load_fixture("standings_wire.json"))
        names = [name for name, _ in members]
        assert "Northwood" in names
        assert len(members) == 4

    def test_league_record(self):
        record = json_api.parse_league_record(load_fixture("standings_wire.json"), "Northwood")
        assert record is not None


class TestHydration:
    def test_extracts_next_data(self):
        html = (
            '<html><body><script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"pageProps": {"hello": 1}}, "buildId": "abc123"}'
            "</script></body></html>"
        )
        payload = hydration.page_props_from_html(html)
        assert payload["pageProps"]["hello"] == 1

    def test_build_id_regex(self):
        assert hydration.extract_build_id('..."buildId":"1785513693"...') == "1785513693"

    def test_build_id_with_escaped_newline_in_string(self):
        # Regression: buildId values never contain quotes/backslashes; the
        # regex must stop at the first unescaped quote.
        assert hydration.extract_build_id('"buildId": "x9\\n" junk') == "x9"

    def test_missing_hydration_raises(self):
        with pytest.raises(SchemaDriftError):
            hydration.page_props_from_html("<html><body>static page</body></html>")


class TestTimezones:
    @pytest.mark.parametrize(
        ("state", "zip_code", "zone"),
        [
            ("ca", None, "America/Los_Angeles"),
            ("ny", None, "America/New_York"),
            ("tx", "79901", "America/Denver"),   # El Paso via ZIP prefix
            ("tx", "77002", "America/Chicago"),  # Houston
            ("az", None, "America/Phoenix"),
            ("hi", None, "Pacific/Honolulu"),
        ],
    )
    def test_known_mappings(self, state, zip_code, zone):
        name, _ = tz_for_school(state, zip_code)
        assert name == zone

    def test_split_state_default_carries_warning(self):
        name, warning = tz_for_school("tn", None)
        assert name == "America/Chicago"
        assert warning is not None and warning.code == "tz_split_state_default"

    def test_unknown_state_falls_back_to_utc_with_warning(self):
        name, warning = tz_for_school("zz", None)
        assert name == "UTC"
        assert warning is not None
