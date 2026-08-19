"""The sport catalogue, selection rules, and the URL grammar they encode.

The URL expectations here were verified live against maxpreps.com on
2026-08-18 (three schools).  They are the reason the table exists, so they are
asserted rather than assumed.
"""

from __future__ import annotations

import pytest

from maxpreps_broadcast import sports
from maxpreps_broadcast.config import Settings, SportsConfig
from maxpreps_broadcast.errors import TooManySportsError, UnknownSportError
from maxpreps_broadcast.models import TeamRef


class TestCatalogue:
    def test_every_season_the_user_named_is_covered(self):
        by_season = {s.value: [x.key for x in sports.for_season(s)] for s in sports.Season}
        assert "football" in by_season["fall"]
        assert "boys-basketball" in by_season["winter"]
        assert "baseball" in by_season["spring"]

    def test_keys_are_unique(self):
        keys = [s.key for s in sports.all_sports()]
        assert len(keys) == len(set(keys))

    def test_slug_gender_pairs_are_unique(self):
        """The reverse URL lookup depends on this being unambiguous."""
        pairs = [(s.slug, s.gender_segment) for s in sports.all_sports()]
        assert len(pairs) == len(set(pairs))

    def test_volleyball_default_gender_is_girls(self):
        """MaxPreps inverts volleyball: the bare slug is the girls' team, and
        the boys are the explicit one.  Verified live."""
        assert sports.get("girls-volleyball").gender_segment is None
        assert sports.get("boys-volleyball").gender_segment == "boys"
        assert sports.get("girls-volleyball").slug == "volleyball"

    def test_basketball_default_gender_is_boys(self):
        assert sports.get("boys-basketball").gender_segment is None
        assert sports.get("girls-basketball").gender_segment == "girls"

    def test_track_field_slug_is_not_the_obvious_guess(self):
        """``track-and-field`` 404s; MaxPreps spells it ``track-field``."""
        assert sports.get("track-field").slug == "track-field"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("football", ["football", "fall"]),
            ("girls-volleyball", ["volleyball", "fall"]),
            ("boys-volleyball", ["volleyball", "boys", "spring"]),
            ("girls-soccer", ["soccer", "girls", "winter"]),
            ("boys-soccer", ["soccer", "winter"]),
            ("girls-flag-football", ["flag-football", "girls", "fall"]),
        ],
    )
    def test_path_segments(self, key, expected):
        assert sports.get(key).path_segments() == expected

    def test_only_weekly_sports_are_week_indexed(self):
        weekly = {s.key for s in sports.all_sports() if s.week_indexed}
        assert weekly == {"football", "girls-flag-football"}


class TestLookup:
    @pytest.mark.parametrize(
        "name", ["girls-volleyball", "Girls Volleyball", "GIRLS VOLLEYBALL", "girls volleyball"]
    )
    def test_forgiving_names(self, name):
        """A producer or a web form may send any of these spellings."""
        assert sports.get(name).key == "girls-volleyball"

    def test_unknown_sport_names_the_alternatives(self):
        with pytest.raises(UnknownSportError) as excinfo:
            sports.get("quidditch")
        assert "football" in str(excinfo.value)

    def test_key_for_url_round_trips(self):
        for sport in sports.all_sports():
            assert sports.key_for_url(sport.slug, sport.gender_segment) == sport.key

    def test_key_for_url_unknown_is_none_not_an_error(self):
        assert sports.key_for_url("quidditch", None) is None


class TestSelection:
    def test_duplicates_collapse_and_order_is_preserved(self):
        got = sports.resolve_many(["football", "Girls Volleyball", "football"])
        assert [s.key for s in got] == ["football", "girls-volleyball"]

    def test_over_the_cap_is_refused(self):
        too_many = [s.key for s in sports.all_sports()][: sports.MAX_ACTIVE_SPORTS + 1]
        with pytest.raises(TooManySportsError):
            sports.resolve_many(too_many)

    def test_exactly_the_cap_is_allowed(self):
        at_cap = [s.key for s in sports.all_sports()][: sports.MAX_ACTIVE_SPORTS]
        assert len(sports.resolve_many(at_cap)) == sports.MAX_ACTIVE_SPORTS

    def test_season_preset_is_capped(self):
        for season in sports.Season:
            assert len(sports.preset_for(season)) <= sports.MAX_ACTIVE_SPORTS

    def test_preset_excludes_the_unlisted_girls_variants(self):
        fall = {s.key for s in sports.preset_for("fall")}
        assert "girls-cross-country" not in fall
        assert sports.get("girls-cross-country") is not None  # still selectable


class TestSettingsIntegration:
    def _settings(self, **kw):
        s = Settings()
        s.primary.state, s.primary.city, s.primary.school_slug = "ca", "irvine", "northwood-timberwolves"
        for key, value in kw.items():
            setattr(s, key, value)
        return s

    def test_falls_back_to_the_primary_sport_when_nothing_is_active(self):
        s = self._settings()
        assert [x.key for x in s.active_sports()] == ["football"]

    def test_active_selection_wins(self):
        s = self._settings(sports=SportsConfig(active=["girls-volleyball", "football"]))
        assert [x.key for x in s.active_sports()] == ["girls-volleyball", "football"]

    def test_rejected_selection_leaves_state_untouched(self):
        s = self._settings(sports=SportsConfig(active=["football"]))
        with pytest.raises(UnknownSportError):
            s.set_active_sports(["football", "quidditch"])
        assert s.sports.active == ["football"]

    def test_config_validates_on_load(self):
        """A typo in config.toml fails at load, not on the first fetch, and the
        error names the valid keys rather than pointing at a pydantic field."""
        with pytest.raises(UnknownSportError) as excinfo:
            SportsConfig(active=["quidditch"])
        assert "girls-volleyball" in str(excinfo.value)

    def test_team_ref_for_sport_builds_the_verified_path(self):
        s = self._settings()
        ref = s.team_ref_for_sport(sports.get("girls-soccer"))
        assert ref.path(tab="schedule") == (
            "ca/irvine/northwood-timberwolves/soccer/girls/winter/schedule"
        )


class TestTeamRefGrammar:
    def test_season_year_is_a_query_param_not_a_path_segment(self):
        """``/soccer/25-26/schedule`` 404s upstream; the year belongs in ?year=."""
        ref = TeamRef(
            state="ca", city="irvine", school="northwood-timberwolves",
            sport="soccer", gender="girls", season_name="winter", season="25-26",
        )
        assert ref.path(tab="schedule").endswith("soccer/girls/winter/schedule")
        assert "25-26" not in ref.path(tab="schedule")
        assert ref.query() == {"year": "25-26"}

    def test_from_url_recovers_season_name_and_sport_key(self):
        ref = TeamRef.from_url(
            "https://www.maxpreps.com/ca/irvine/northwood-timberwolves/soccer/girls/winter/schedule/"
        )
        assert (ref.sport, ref.gender, ref.season_name) == ("soccer", "girls", "winter")
        assert ref.sport_key == "girls-soccer"

    def test_for_sport_drops_the_previous_teams_identifiers(self):
        """A team_id for the football team says nothing about the volleyball team."""
        ref = TeamRef(
            state="ca", city="irvine", school="northwood-timberwolves",
            sport="football", season_name="fall", team_id="abc", sport_season_id="def",
        )
        moved = ref.for_sport(sports.get("girls-volleyball"))
        assert moved.team_id is None and moved.sport_season_id is None
        assert moved.sport_key == "girls-volleyball"

    def test_rejects_a_season_name_that_is_not_a_season(self):
        with pytest.raises(ValueError):
            TeamRef(state="ca", city="irvine", school="x", season_name="summer")
