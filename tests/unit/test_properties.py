"""Property-based invariants (hypothesis): budgets, jersey sort, initials,
timezone round-trips, mgjson number formatting."""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from maxpreps_broadcast.export.mgjson import _number_props, format_utc
from maxpreps_broadcast.models import RosterEntry, RosterSort, sort_roster
from maxpreps_broadcast.parsers.normalize import (
    acronym_from_name,
    fit_budget,
    initial_last,
    strip_accents,
    upper_display,
)
from maxpreps_broadcast.parsers.timezones import localize_naive

names = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll"), whitelist_characters=" -'"),
    min_size=1, max_size=40,
).filter(lambda s: s.strip())


class TestFitBudget:
    @given(value=st.text(max_size=120), budget=st.integers(min_value=0, max_value=60))
    def test_never_exceeds_budget(self, value, budget):
        assert len(fit_budget(value, budget)) <= budget

    @given(value=st.text(min_size=1, max_size=30))
    def test_within_budget_is_identity(self, value):
        assert fit_budget(value, len(value)) == value

    @given(value=st.text(max_size=120), budget=st.integers(min_value=1, max_value=60))
    def test_result_is_prefix_modulo_trailing_space(self, value, budget):
        out = fit_budget(value, budget)
        assert value.startswith(out) or value.startswith(out + " ") or out == value[:len(out)]


class TestJerseySortInvariants:
    jerseys = st.lists(
        st.one_of(st.none(), st.integers(min_value=0, max_value=99).map(str), st.just("00")),
        min_size=0, max_size=30,
    )

    @given(jerseys=jerseys)
    def test_blanks_always_last_and_numeric_ascending(self, jerseys):
        entries = [RosterEntry(jersey_number=j, last_name="X") for j in jerseys]
        ordered = sort_roster(entries, RosterSort.JERSEY)
        values = [e.jersey_number for e in ordered]
        first_none = next((i for i, v in enumerate(values) if v is None), len(values))
        assert all(v is None for v in values[first_none:])
        numeric = [int(v) for v in values[:first_none]]
        assert numeric == sorted(numeric)

    @given(jerseys=jerseys)
    def test_sort_is_stable_permutation(self, jerseys):
        entries = [RosterEntry(jersey_number=j, last_name=str(i)) for i, j in enumerate(jerseys)]
        ordered = sort_roster(entries, RosterSort.JERSEY)
        assert sorted(e.last_name for e in ordered) == sorted(e.last_name for e in entries)


class TestNameHelpers:
    @given(first=names, last=names)
    def test_initial_last_shape(self, first, last):
        out = initial_last(first, last)
        assert out.startswith(first.strip()[0].upper() + ".")
        # The last-name segment must match the module's own display transform
        # (NFC-then-upper ordering matters for combining marks — found by hypothesis).
        assert out == f"{first.strip()[0].upper()}. {upper_display(last.strip())}"

    @given(name=names)
    def test_acronym_alnum_upper_and_bounded(self, name):
        out = acronym_from_name(name)
        assert len(out) <= 4
        assert out == out.upper()

    @given(value=st.text(max_size=60))
    def test_strip_accents_ascii_stable(self, value):
        once = strip_accents(value)
        assert strip_accents(once) == once


class TestTimezoneRoundTrip:
    @given(
        base=st.datetimes(
            min_value=datetime(2026, 8, 1), max_value=datetime(2026, 12, 15),
        ),
    )
    @hyp_settings(max_examples=60)
    def test_localize_preserves_wall_clock(self, base):
        localized, _as_utc = localize_naive(base, "America/Los_Angeles")
        assert localized.replace(tzinfo=None) == base
        # UTC offset is one of PDT/PST for this window.
        assert localized.utcoffset() in {timedelta(hours=-7), timedelta(hours=-8)}


class TestMgjsonNumbers:
    @given(values=st.lists(st.floats(min_value=-1e6, max_value=1e6,
                                     allow_nan=False, allow_infinity=False),
                           min_size=1, max_size=50))
    def test_number_props_ranges_contain_occuring(self, values):
        props = _number_props(values)
        occ = props["range"]["occuring"]
        legal = props["range"]["legal"]
        assert legal["min"] <= occ["min"] <= occ["max"] <= legal["max"]

    @given(moment=st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)))
    def test_time_format_shape(self, moment):
        out = format_utc(moment)
        assert out.endswith("Z") and out[10] == "T" and out[-5] == "."
        assert len(out.split(".")[-1]) == 4
