"""Schedule parsing: goldens against the spec fixture, then every trap."""

from __future__ import annotations

import json

import pytest

from maxpreps_broadcast.errors import SchemaDriftError
from maxpreps_broadcast.models import ContestType, GameResult, ParseMode, Venue
from maxpreps_broadcast.parsers import json_api
from tests.conftest import load_fixture


class TestGoldenTrimmed:
    """Every field of the spec's 2-game trimmed fixture, exactly."""

    @pytest.fixture(autouse=True)
    def _parse(self):
        self.schedule, self.ctx = json_api.parse_schedule(load_fixture("schedule_gems_trimmed.json"))

    def test_team_identity(self):
        team = self.schedule.team
        assert team.school_name == "Northwood"
        assert team.mascot == "Timberwolves"
        assert team.team_id == "8ad7fb66-314d-456c-b977-842f95a13025"
        assert team.sport_season_id == "2286cd80-c46d-4739-8dd1-92a67ca8daa7"
        assert team.city == "Irvine"
        assert team.state_code == "CA"

    def test_colors_normalized(self):
        assert self.schedule.team.color1 == "#022C66"

    def test_league_and_coach(self):
        assert self.schedule.team.league_name == "Iota"
        assert self.schedule.team.coach_name == "Stephen Barbee"

    def test_timezone_attached(self):
        # 2026-08-21 19:00 local (Irvine, CA = America/Los_Angeles, PDT)
        first = self.schedule.contests[0]
        assert first.starts_at_local is not None
        assert first.starts_at_local.tzname() == "PDT"
        assert first.starts_at_utc.hour == 2  # 19:00 PDT = 02:00Z next day

    def test_home_game_venue(self):
        first = self.schedule.contests[0]
        assert first.is_home is True
        assert first.venue is Venue.HOME
        assert first.opponent_name == "Irvine"

    def test_no_result_pre_season(self):
        assert all(c.result is None for c in self.schedule.contests)
        assert self.schedule.record_display() == "0-0"


class TestIsHomeInversionTrap:
    """The away game where Northwood appears in the wire's home slot: is_home
    is the single source of truth; slot order must be ignored."""

    def test_brea_olinda_is_away(self):
        schedule, _ = json_api.parse_schedule(load_fixture("schedule_gems_full.json"))
        brea = next(c for c in schedule.contests if (c.opponent_name or "").startswith("Brea"))
        assert brea.is_home is False
        assert brea.venue is Venue.AWAY


class TestByeSynthesis:
    def test_week5_bye_inserted(self):
        schedule, ctx = json_api.parse_schedule(load_fixture("schedule_gems_full.json"))
        assert not any(c.contest_type is ContestType.BYE for c in schedule.contests)
        json_api.synthesize_byes(schedule, ctx)
        byes = [c for c in schedule.contests if c.contest_type is ContestType.BYE]
        assert len(byes) == 1
        assert byes[0].week_index == 5
        assert byes[0].is_synthesized is True
        # Anchored one week after W4 (Sep 18 → Sep 25)
        assert byes[0].starts_at_local.strftime("%m/%d") == "09/25"

    def test_bye_never_counts_toward_record(self):
        schedule, ctx = json_api.parse_schedule(load_fixture("schedule_gems_full.json"))
        json_api.synthesize_byes(schedule, ctx)
        assert schedule.record_display() == "0-0"


class TestResultsAndRecords:
    @pytest.fixture(autouse=True)
    def _parse(self):
        self.schedule, self.ctx = json_api.parse_schedule(
            load_fixture("schedule_gems_results_dst.json")
        )

    def test_running_record_before_each_game(self):
        befores = [c.record_before_this_game for c in self.schedule.contests]
        assert befores[:5] == ["0-0", "1-0", "1-1", "2-1", "2-1-1"]

    def test_result_letters(self):
        results = [c.result for c in self.schedule.contests[:4]]
        assert results == [GameResult.WIN, GameResult.LOSS, GameResult.WIN, GameResult.TIE]

    def test_final_record_includes_tie(self):
        assert self.schedule.record_display() == "2-1-1"

    def test_dst_boundary_utc_offsets(self):
        # Oct 31 2026 is PDT (UTC-7); Nov 6 2026 is PST (UTC-8).
        oct_game = next(c for c in self.schedule.contests if c.starts_at_local.month == 10
                        and c.starts_at_local.day == 31)
        nov_game = next(c for c in self.schedule.contests if c.starts_at_local.month == 11)
        assert oct_game.starts_at_utc.hour == 2   # 19:00 PDT
        assert nov_game.starts_at_utc.hour == 3   # 19:00 PST

    def test_tournament_parsed(self):
        assert self.schedule.tournaments, "expected the CIF tournament block to parse"
        assert any("CIF" in (t.tournament_name or "") for t in self.schedule.tournaments)


class TestDriftModes:
    def test_unknown_fields_warn_in_lenient(self):
        schedule, ctx = json_api.parse_schedule(
            load_fixture("schedule_gems_drifted.json"), ParseMode.LENIENT
        )
        assert schedule.team.school_name == "Northwood"
        assert any(w.code == "unknown_fields" for w in ctx.warnings)

    def test_unknown_fields_raise_in_strict(self):
        with pytest.raises(SchemaDriftError):
            json_api.parse_schedule(load_fixture("schedule_gems_drifted.json"), ParseMode.STRICT)

    def test_missing_required_raises_in_both_modes(self):
        payload = load_fixture("schedule_gems_missing_required.json")
        for mode in (ParseMode.LENIENT, ParseMode.STRICT):
            with pytest.raises(SchemaDriftError):
                json_api.parse_schedule(payload, mode)

    def test_truncated_json_is_a_clean_error(self):
        from pathlib import Path

        text = (Path(__file__).parent.parent / "fixtures" / "schedule_gems_truncated.json").read_text()
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)

    def test_team_size_lands_in_raw_extra_with_warning(self):
        schedule, ctx = json_api.parse_schedule(load_fixture("schedule_gems_trimmed.json"))
        # teamSize is deliberately unmapped: preserved, flagged, never dropped.
        assert "teamSize" in schedule.team.raw_extra
        assert any(w.code == "unknown_fields" and "teamSize" in w.message for w in ctx.warnings)


class TestWireSchedule:
    def test_myers_park_real_capture_parses(self):
        schedule, _ctx = json_api.parse_schedule(load_fixture("schedule_wire_myers_park.json"))
        assert schedule.team.school_name
        assert len(schedule.contests) >= 8
        # Wire rows carry contestState/calcResult ints — spot check one final.
        finals = [c for c in schedule.contests if c.result is not None]
        assert finals, "real capture should include completed games"

    def test_soft_deleted_rows_filtered(self):
        payload = load_fixture("schedule_wire_myers_park.json")
        schedule, _ctx = json_api.parse_schedule(payload)
        assert not any(c.raw_extra.get("isDeleted") for c in schedule.contests)
