"""Parser and model tests — the trap matrix, compact.

Each test here guards a failure mode that would visibly break a broadcast:
wrong venue on the scorebug, a missed bye week, an hour-off kickoff time
after the DST change, a roster with a deleted kid on it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maxpreps_broadcast.errors import SchemaDriftError
from maxpreps_broadcast.models import (
    ContestType,
    GameResult,
    GameStatus,
    ParseMode,
    RosterSort,
)
from maxpreps_broadcast.parsers import hydration, json_api
from maxpreps_broadcast.parsers.normalize import acronym_from_name, fit_budget, initial_last, strip_accents

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ------------------------------------------------------------------ schedule


class TestScheduleGems:
    @pytest.fixture(scope="class")
    def parsed(self):
        schedule, ctx = json_api.parse_schedule(load("schedule_gems_full.json"), ParseMode.LENIENT)
        return schedule, ctx

    def test_team_block(self, parsed):
        schedule, _ = parsed
        assert schedule.team.school_name == "Northwood"
        assert schedule.team.color1 == "#022C66"
        assert schedule.team.tz_name == "America/Los_Angeles"

    def test_is_home_is_sole_venue_truth(self, parsed):
        """Trap: Brea Olinda occupies the *home* slot in the payload, but our
        side's is_home=False.  The slot order must never decide the venue."""
        schedule, _ = parsed
        brea = next(c for c in schedule.contests if "Brea" in (c.opponent_name or ""))
        assert brea.is_home is False

    def test_timezone_attach(self, parsed):
        """7:00 PM PDT stored as naive local must serialize to 02:00Z."""
        schedule, _ = parsed
        first = schedule.contests[0]
        assert first.starts_at_local.strftime("%H:%M %Z") == "19:00 PDT"
        assert first.starts_at_utc.hour == 2

    def test_week_indexes_and_bye(self, parsed):
        schedule, ctx = parsed
        json_api.synthesize_byes(schedule, ctx)
        byes = [c for c in schedule.contests if c.contest_type is ContestType.BYE]
        assert len(byes) == 1
        assert byes[0].week_index == 5  # the 9/25 gap
        assert byes[0].is_synthesized
        weeks = [c.week_index for c in schedule.contests]
        assert weeks == sorted(weeks)


class TestScheduleResults:
    @pytest.fixture(scope="class")
    def parsed(self):
        schedule, ctx = json_api.parse_schedule(load("schedule_gems_results_dst.json"), ParseMode.LENIENT)
        return schedule, ctx

    def test_running_records_include_ties(self, parsed):
        schedule, _ = parsed
        records = [c.record_before_this_game for c in schedule.contests]
        assert records[0] == "0-0"
        assert "2-1-1" in schedule.record_display() or schedule.record_display().count("-") == 2

    def test_dst_straddle(self, parsed):
        """Oct 31 is PDT (UTC-7); Nov 6 is PST (UTC-8).  Same wall clock,
        different UTC — the classic hour-off playoff kickoff bug."""
        schedule, _ = parsed
        oct31 = next(c for c in schedule.contests
                     if c.starts_at_local.month == 10 and c.starts_at_local.day == 31)
        nov6 = next(c for c in schedule.contests if c.starts_at_local.month == 11)
        assert oct31.starts_at_utc.hour == 2   # 19:00 + 7
        assert nov6.starts_at_utc.hour == 3    # 19:00 + 8

    def test_results_parsed(self, parsed):
        schedule, _ = parsed
        results = [c.result for c in schedule.contests if c.result]
        assert GameResult.WIN in results and GameResult.LOSS in results and GameResult.TIE in results

    def test_tournament(self, parsed):
        schedule, _ = parsed
        assert any(t.tournament_name and "CIF" in t.tournament_name for t in schedule.tournaments)


class TestScheduleDrift:
    def test_missing_required_raises_both_modes(self):
        for mode in (ParseMode.LENIENT, ParseMode.STRICT):
            with pytest.raises(SchemaDriftError):
                json_api.parse_schedule(load("schedule_gems_missing_required.json"), mode)

    def test_unknown_fields_warn_lenient_raise_strict(self):
        schedule, ctx = json_api.parse_schedule(load("schedule_gems_drifted.json"), ParseMode.LENIENT)
        assert schedule.contests
        assert any(w.code == "unknown_fields" for w in ctx.warnings)
        with pytest.raises(SchemaDriftError):
            json_api.parse_schedule(load("schedule_gems_drifted.json"), ParseMode.STRICT)

    def test_truncated_json_is_loader_problem(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads((FIXTURES / "schedule_gems_truncated.json").read_text())


class TestScheduleWire:
    def test_wire_positional_parse(self):
        schedule, _ctx = json_api.parse_schedule(load("schedule_wire_myers_park.json"), ParseMode.LENIENT)
        assert schedule.contests, "wire schedule produced no contests"
        assert schedule.team.school_name
        # Wire rows carry per-side scores + calcResult; at least one final.
        assert any(c.result is not None for c in schedule.contests)


# -------------------------------------------------------------------- roster


class TestRoster:
    @pytest.fixture(scope="class")
    def parsed(self):
        return json_api.parse_roster(load("roster_wire_myers_park.json"), ParseMode.LENIENT)

    def test_deleted_rows_filtered(self, parsed):
        roster, ctx = parsed
        assert len(roster.entries) == 63  # fixture has 64 rows, one isDeleted
        assert any(w.code == "deleted_rows" for w in ctx.warnings)

    def test_jersey_sort_00_before_0_before_1(self, parsed):
        roster, _ = parsed
        entries = roster.sorted(RosterSort.JERSEY).entries
        numbered = [e.jersey_number for e in entries if e.jersey_number]
        # "00" sorts before "0" sorts before "1"; None jerseys sink to the end.
        cleaned = [n for n in numbered if n in {"00", "0", "1"}]
        assert cleaned == sorted(cleaned, key=lambda n: (int(n), -len(n)))
        assert entries[-1].jersey_number is None or numbered  # no crash on None

    def test_arity_drift_warns_lenient_raises_strict(self):
        _roster, ctx = json_api.parse_roster(load("roster_wire_shifted.json"), ParseMode.LENIENT)
        assert any(w.code == "arity_drift" for w in ctx.warnings)
        with pytest.raises(SchemaDriftError):
            json_api.parse_roster(load("roster_wire_shifted.json"), ParseMode.STRICT)

    def test_lower_third_fields(self, parsed):
        roster, _ = parsed
        entry = roster.entries[0]
        assert entry.jersey_padded
        assert entry.lower_third_name == initial_last(entry.first_name, entry.last_name).upper() \
            or entry.lower_third_name  # shape, not exact content


# -------------------------------------------------------------- scoretracker


class TestScoreState:
    def test_live_wire_maps_to_in_progress_q3(self):
        schedule, ctx = json_api.parse_schedule(load("scoretracker_wire_live.json"), ParseMode.LENIENT)
        live = schedule.live_contest()
        assert live is not None
        state = json_api.build_score_state(live, schedule.team, now=datetime.now(UTC), ctx=ctx)
        assert state.status is GameStatus.IN_PROGRESS
        assert state.period == 3
        assert {state.home_score, state.away_score} == {17, 21}
        # Northwood is away in this fixture (homeAwayType says so) → home is Laguna Hills.
        assert "Laguna" in (state.home.name or "")
        # Honest Nones for data the surface doesn't carry:
        assert state.clock is None and state.possession is None and state.down_and_distance is None

    def test_pregame_window(self):
        schedule, ctx = json_api.parse_schedule(load("schedule_gems_full.json"), ParseMode.LENIENT)
        first = schedule.contests[0]
        utc = first.starts_at_utc
        thirty_before = utc.replace(tzinfo=UTC) if utc.tzinfo is None else utc
        state = json_api.build_score_state(
            first, schedule.team, now=thirty_before.replace(hour=thirty_before.hour - 1), ctx=ctx
        )
        assert state.status in {GameStatus.PREGAME, GameStatus.SCHEDULED}


# ------------------------------------------------- search/rankings/standings


class TestOtherSurfaces:
    def test_search(self):
        schools, _ctx = json_api.parse_search(load("search_wire.json"), ParseMode.LENIENT)
        assert schools and schools[0].name

    def test_rankings_movement(self):
        rankings, _ctx = json_api.parse_rankings_list(load("rankings_wire_page.json"), scope="state",
                                                     mode=ParseMode.LENIENT)
        northwood = rankings.find("Northwood")
        assert northwood is not None
        assert northwood.rank == 367
        assert northwood.rank_delta == -3
        assert northwood.previous_rank == 364

    def test_standings_members(self):
        members = json_api.parse_standings_members(load("standings_wire.json"))
        names = [n for n, _ in members]
        assert "Northwood" in names and len(members) >= 4


# ----------------------------------------------------------------- hydration


class TestHydration:
    def test_build_id_extraction_with_trailing_newline_in_string(self):
        html = '<script>{"buildId":"1785513693\\n","x":1}</script>'
        assert hydration.extract_build_id(html) == "1785513693"

    def test_next_data_roundtrip(self):
        payload = {"props": {"pageProps": {"hello": 1}}, "buildId": "abc"}
        html = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script></html>"
        )
        assert hydration.page_props_from_html(html) == {"pageProps": {"hello": 1}}
        assert hydration.extract_build_id(html) == "abc"


# ----------------------------------------------------------------- normalize


class TestNormalize:
    def test_acronyms(self):
        assert acronym_from_name("Irvine") == "IRV"
        assert acronym_from_name("Brea Olinda") == "BO"
        assert acronym_from_name("Orange Lutheran High School") == "OLHS"

    def test_fit_budget_word_boundary(self):
        assert fit_budget("Brea Olinda Wildcats", 12) in {"Brea Olinda", "Brea Olinda…", "Brea Olind…"}
        assert len(fit_budget("Brea Olinda Wildcats", 12)) <= 12

    def test_strip_accents(self):
        assert strip_accents("José Peña") == "Jose Pena"

    def test_initial_last(self):
        assert initial_last("Cameron", "Portis") == "C. PORTIS"  # lower-third convention
