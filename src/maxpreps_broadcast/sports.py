"""The sport catalogue: what a school plays, how MaxPreps spells it, and when.

MaxPreps' URL grammar, verified live against three schools on 2026-08-18:

    {state}/{city}/{school}/{slug}[/{gender}][/{season}]/{tab}

Two quirks are encoded in the table below rather than derived, because neither
is inferable from the sport's name:

* **One gender is implicit per sport, and it is not always the boys.**
  ``basketball`` is the boys' team and ``basketball/girls`` the girls'.
  Volleyball inverts it: ``volleyball`` is *girls* volleyball and the boys are
  explicit at ``volleyball/boys``.  ``gender_segment`` holds the segment to
  emit; ``None`` means this variant is the bare slug.
* **The season segment is load-bearing for some sports and redundant for
  others.**  ``soccer/schedule`` is a 404 while ``soccer/winter/schedule``
  resolves, but ``wrestling/winter/schedule`` 308-redirects back to
  ``wrestling/schedule``.  The tier-1 data route returns byte-identical
  payloads with or without it, so this module always emits it: safe where it is
  redundant, required where it is not.

Slugs are MaxPreps' own, scraped from school nav markup — several do not match
the obvious guess (``track-field``, not ``track-and-field``).

Adding a sport is a one-line edit to ``_CATALOGUE``.  Everything else — CLI
choices, the ``/sports`` API, season presets, export filenames — reads from it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from maxpreps_broadcast.errors import TooManySportsError, UnknownSportError

#: A graphics loop that rotates through more than this many sports stops being
#: legible to an operator mid-broadcast.  Enforced wherever sports are selected.
MAX_ACTIVE_SPORTS = 6


class Season(StrEnum):
    FALL = "fall"
    WINTER = "winter"
    SPRING = "spring"


class Sport(BaseModel):
    """One team-season a school fields, and how to address it on MaxPreps."""

    model_config = ConfigDict(frozen=True)

    key: str
    """Stable identifier: CLI values, export filename suffixes, API keys."""

    display: str
    slug: str
    """The MaxPreps URL sport segment."""

    season: Season
    gender_segment: str | None = None
    """Emitted after the slug.  ``None`` = this variant is the bare slug."""

    week_indexed: bool = False
    """Weekly cadence, so gaps are meaningful bye weeks worth synthesizing."""

    regulation_periods: int = 4
    period_noun: str = "quarter"

    in_preset: bool = True
    """Part of its season's default roster.  False for the girls' variants of
    sports the season lists name without a gender — catalogued and selectable,
    but not assumed."""

    def path_segments(self) -> list[str]:
        """The sport portion of a team URL, season segment included."""
        parts = [self.slug]
        if self.gender_segment:
            parts.append(self.gender_segment)
        parts.append(self.season.value)
        return parts

    def summary(self) -> dict[str, Any]:
        """JSON-friendly shape for the ``/sports`` API and CLI listings."""
        return {
            "key": self.key,
            "display": self.display,
            "season": self.season.value,
            "slug": self.slug,
            "gender_segment": self.gender_segment,
            "week_indexed": self.week_indexed,
        }


def _s(
    key: str,
    display: str,
    slug: str,
    season: Season,
    gender: str | None = None,
    *,
    week_indexed: bool = False,
    periods: int = 4,
    noun: str = "quarter",
    in_preset: bool = True,
) -> Sport:
    return Sport(
        key=key,
        display=display,
        slug=slug,
        season=season,
        gender_segment=gender,
        week_indexed=week_indexed,
        regulation_periods=periods,
        period_noun=noun,
        in_preset=in_preset,
    )


_F, _W, _P = Season.FALL, Season.WINTER, Season.SPRING

# Ordered within each season; the season presets preserve this order.
_CATALOGUE: tuple[Sport, ...] = (
    # ---------------------------------------------------------------- fall
    _s("football", "Football", "football", _F, week_indexed=True),
    _s("girls-volleyball", "Girls Volleyball", "volleyball", _F, None, periods=5, noun="set"),
    _s("boys-water-polo", "Boys Water Polo", "water-polo", _F),
    _s("cross-country", "Cross Country", "cross-country", _F, periods=1, noun="race"),
    _s("girls-tennis", "Girls Tennis", "tennis", _F, "girls", periods=3, noun="set"),
    _s("girls-golf", "Girls Golf", "golf", _F, "girls", periods=1, noun="round"),
    _s("girls-flag-football", "Girls Flag Football", "flag-football", _F, "girls",
       week_indexed=True, periods=2, noun="half"),
    # -------------------------------------------------------------- winter
    _s("boys-basketball", "Boys Basketball", "basketball", _W),
    _s("girls-basketball", "Girls Basketball", "basketball", _W, "girls"),
    _s("boys-soccer", "Boys Soccer", "soccer", _W, None, periods=2, noun="half"),
    _s("girls-soccer", "Girls Soccer", "soccer", _W, "girls", periods=2, noun="half"),
    _s("girls-water-polo", "Girls Water Polo", "water-polo", _W, "girls"),
    _s("wrestling", "Wrestling", "wrestling", _W, None, periods=3, noun="period"),
    # -------------------------------------------------------------- spring
    _s("baseball", "Baseball", "baseball", _P, periods=7, noun="inning"),
    _s("softball", "Softball", "softball", _P, periods=7, noun="inning"),
    _s("boys-volleyball", "Boys Volleyball", "volleyball", _P, "boys", periods=5, noun="set"),
    _s("boys-lacrosse", "Boys Lacrosse", "lacrosse", _P),
    _s("girls-lacrosse", "Girls Lacrosse", "lacrosse", _P, "girls"),
    _s("boys-tennis", "Boys Tennis", "tennis", _P, None, periods=3, noun="set"),
    _s("boys-golf", "Boys Golf", "golf", _P, None, periods=1, noun="round"),
    _s("track-field", "Track and Field", "track-field", _P, periods=1, noun="event"),
    _s("swimming", "Swimming", "swimming", _P, None, periods=1, noun="event"),
    # Girls' variants of the sports the season lists name without a gender.
    # Selectable, but not assumed: whether a school fields them separately is a
    # per-school fact this table has no way to know.
    _s("girls-cross-country", "Girls Cross Country", "cross-country", _F, "girls",
       periods=1, noun="race", in_preset=False),
    _s("girls-wrestling", "Girls Wrestling", "wrestling", _W, "girls",
       periods=3, noun="period", in_preset=False),
    _s("girls-track-field", "Girls Track and Field", "track-field", _P, "girls",
       periods=1, noun="event", in_preset=False),
    _s("girls-swimming", "Girls Swimming", "swimming", _P, "girls",
       periods=1, noun="event", in_preset=False),
)

BY_KEY: dict[str, Sport] = {sport.key: sport for sport in _CATALOGUE}

#: Each season's default roster, in the order a producer would list them.
SEASON_PRESETS: dict[Season, tuple[str, ...]] = {
    season: tuple(s.key for s in _CATALOGUE if s.season is season and s.in_preset)
    for season in Season
}


def all_sports() -> tuple[Sport, ...]:
    return _CATALOGUE


def for_season(season: Season | str) -> list[Sport]:
    want = Season(str(season).lower())
    return [s for s in _CATALOGUE if s.season is want]


def _normalize(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.strip().lower()).strip("-")


def get(name: str) -> Sport:
    """Look up a sport by key, or forgivingly by display name.

    Producers type ``"Girls Volleyball"`` as often as ``girls-volleyball``, and
    a website will POST whichever it happens to hold.
    """
    key = _normalize(name)
    if key in BY_KEY:
        return BY_KEY[key]
    collapsed = key.replace("-", "")
    for sport in _CATALOGUE:
        if collapsed in {sport.key.replace("-", ""), _normalize(sport.display).replace("-", "")}:
            return sport
    raise UnknownSportError(name, sorted(BY_KEY))


def resolve_many(names: list[str] | tuple[str, ...]) -> list[Sport]:
    """Resolve and de-duplicate a selection, enforcing the active-sport cap.

    Order is the caller's — it is the order graphics rotate in, so it is
    preserved rather than sorted.
    """
    out: list[Sport] = []
    seen: set[str] = set()
    for name in names:
        sport = get(name)
        if sport.key in seen:
            continue
        seen.add(sport.key)
        out.append(sport)
    if len(out) > MAX_ACTIVE_SPORTS:
        raise TooManySportsError(len(out), MAX_ACTIVE_SPORTS, [s.key for s in out])
    return out


def preset_for(season: Season | str) -> list[Sport]:
    """A season's default sports, truncated to the active-sport cap."""
    want = Season(str(season).lower())
    keys = SEASON_PRESETS[want][:MAX_ACTIVE_SPORTS]
    return [BY_KEY[k] for k in keys]


def key_for_url(slug: str, gender_segment: str | None) -> str | None:
    """Reverse a URL's sport segments back to a catalogue key, if we know it.

    ``(slug, gender_segment)`` is unique across the table — including the pairs
    that share a slug across seasons, like boys' (spring) and girls' (fall)
    tennis.  Returns ``None`` for sports MaxPreps has and this table does not,
    which is a normal outcome, not an error.
    """
    for sport in _CATALOGUE:
        if sport.slug == slug and sport.gender_segment == gender_segment:
            return sport.key
    return None


def catalogue() -> dict[str, Any]:
    """The whole table, grouped by season — what a selection UI renders from."""
    return {
        "max_active": MAX_ACTIVE_SPORTS,
        "seasons": {
            season.value: {
                "preset": list(SEASON_PRESETS[season][:MAX_ACTIVE_SPORTS]),
                "sports": [s.summary() for s in for_season(season)],
            }
            for season in Season
        },
    }
