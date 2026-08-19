"""Team identity: ``TeamRef`` (the argument type accepted everywhere), ``School``
search records, and ``TeamInfo`` (the full team block on a schedule payload).

MaxPreps identifies everything with GUIDs; slugs are convenient but not stable
keys.  ``TeamRef`` therefore carries both, hashes on the identifying tuple, and
round-trips to/from a canonical URL.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from maxpreps_broadcast.sports import key_for_url as _sport_key_for

_ORIGIN = "https://www.maxpreps.com"
_SEASON_RE = re.compile(r"^\d{2}-\d{2}$")
_TABS = {"schedule", "roster", "stats", "scores", "standings", "rankings"}
_LEVELS = {"varsity", "jv", "freshman", "sophomore", "js"}
_GENDERS = {"boys", "girls", "coed"}
_SEASON_NAMES = {"fall", "winter", "spring"}


class TeamRef(BaseModel):
    """A hashable value object naming one team-season.

    ``school`` is the combined ``{school-slug}-{mascot-slug}`` segment exactly as
    it appears in the URL (e.g. ``northwood-timberwolves``).
    """

    model_config = ConfigDict(frozen=True)

    state: str = Field(min_length=2, max_length=2, description="two-letter state code, lower case")
    city: str
    school: str
    sport: str = "football"
    gender: str | None = None
    level: str | None = None
    season_name: str | None = Field(
        default=None,
        description="fall|winter|spring — a URL path segment, load-bearing for some sports",
    )
    season: str | None = Field(
        default=None, description="YY-YY, e.g. '26-27'; sent as ?year=, not a path segment"
    )
    sport_key: str | None = Field(
        default=None, description="catalogue key this ref was built from, when it came from one"
    )
    team_id: str | None = None
    sport_season_id: str | None = None
    all_season_id: str | None = None

    @field_validator("state", "city", "school", "sport", mode="before")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("season_name")
    @classmethod
    def _season_name_known(cls, v: str | None) -> str | None:
        if v is not None and v.lower() not in _SEASON_NAMES:
            raise ValueError(f"season_name must be one of {sorted(_SEASON_NAMES)}, got {v!r}")
        return v.lower() if v else v

    @field_validator("season")
    @classmethod
    def _season_shape(cls, v: str | None) -> str | None:
        if v is not None and not _SEASON_RE.match(v):
            raise ValueError(f"season must be YY-YY, got {v!r}")
        return v

    def __hash__(self) -> int:
        return hash(
            (self.state, self.city, self.school, self.sport, self.gender,
             self.level, self.season_name, self.season)
        )

    def path(self, *, tab: str | None = None, season_name: str | None = None) -> str:
        """URL path (no origin, no leading slash).

        The season *name* is a path segment; the season *year* is not — it goes
        out as ``?year=YY-YY`` (``/soccer/25-26/schedule`` is a 404, whereas
        ``/soccer/winter/schedule`` resolves).
        """
        parts = [self.state, self.city, self.school, self.sport]
        if self.gender:
            parts.append(self.gender)
        if self.level and self.level != "varsity":
            parts.append(self.level)
        eff_season = season_name if season_name is not None else self.season_name
        if eff_season:
            parts.append(eff_season)
        if tab:
            parts.append(tab)
        return "/".join(parts)

    def query(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Query parameters this ref implies (currently just the season year)."""
        out: dict[str, str] = {}
        if self.season:
            out["year"] = self.season
        out.update(extra or {})
        return out

    def for_sport(self, sport: Any, *, season: str | None = None) -> TeamRef:
        """Re-point this ref at another sport from the catalogue.

        Takes a ``sports.Sport`` (typed loosely to keep models free of a
        dependency on the catalogue, which imports errors, which imports
        nothing).  Identifiers from the old sport are dropped: a ``team_id`` for
        the football team says nothing about the volleyball team.
        """
        return self.model_copy(
            update={
                "sport": sport.slug,
                "gender": sport.gender_segment,
                "season_name": sport.season.value,
                "sport_key": sport.key,
                "season": season if season is not None else self.season,
                "team_id": None,
                "sport_season_id": None,
                "all_season_id": None,
            }
        )

    def to_url(self, *, tab: str | None = None) -> str:
        return f"{_ORIGIN}/{self.path(tab=tab)}/"

    @classmethod
    def from_url(cls, url: str) -> TeamRef:
        """Parse a canonical team URL (with or without season/tab segments)."""
        path = urlsplit(url).path if "//" in url else url
        segs = [s for s in path.strip("/").split("/") if s]
        if len(segs) < 4:
            raise ValueError(f"not a team URL: {url!r}")
        state, city, school, sport = segs[0], segs[1], segs[2], segs[3]
        gender: str | None = None
        level: str | None = None
        season: str | None = None
        season_name: str | None = None
        for seg in segs[4:]:
            if seg in _TABS:
                break
            if seg in _GENDERS:
                gender = seg
            elif seg in _LEVELS:
                level = seg
            elif seg in _SEASON_NAMES:
                season_name = seg
            elif _SEASON_RE.match(seg):
                season = seg
        return cls(
            state=state, city=city, school=school, sport=sport, gender=gender, level=level,
            season_name=season_name, season=season, sport_key=_sport_key_for(sport, gender),
        )

    def display(self) -> str:
        tail = f" {self.season}" if self.season else ""
        return f"{self.school} ({self.state.upper()}) {self.sport}{tail}"


class School(BaseModel):
    """One school search result."""

    school_id: str | None = None
    name: str
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    mascot: str | None = None
    mascot_url: str | None = None
    canonical_url: str | None = None
    ranking: int | None = None
    colors: list[str] = Field(default_factory=list, description="normalized #RRGGBB, primary first")
    raw_extra: dict[str, Any] = Field(default_factory=dict)

    def team_ref(self, *, sport: str = "football", season: str | None = None) -> TeamRef:
        """A ref for one of this school's teams.

        ``sport`` is a catalogue key (``girls-volleyball``) or display name;
        anything the catalogue does not know is passed through as a raw URL
        segment so an unlisted sport still works.
        """
        if not self.canonical_url:
            raise ValueError(f"school {self.name!r} has no canonical URL to derive a TeamRef from")
        from maxpreps_broadcast import sports
        from maxpreps_broadcast.errors import UnknownSportError

        base = TeamRef.from_url(self.canonical_url.rstrip("/") + "/football/")
        try:
            entry = sports.get(sport)
        except UnknownSportError:  # unlisted sports stay usable as raw slugs
            return base.model_copy(
                update={"sport": sport, "season": season, "team_id": self.school_id}
            )
        return base.for_sport(entry, season=season).model_copy(update={"team_id": self.school_id})

    def label(self) -> str:
        return str(self)

    def __str__(self) -> str:
        loc = ", ".join(x for x in (self.city, self.state) if x)
        return f"{self.name} ({loc})" if loc else self.name


class TeamInfo(BaseModel):
    """The subject team block on a schedule/roster payload, normalized.

    ``teamSize`` from the wire is deliberately absent: it is *not* roster size
    and must never be surfaced as a player count (it lands in ``raw_extra``).
    """

    school_name: str
    school_formatted_name: str | None = None
    school_acronym: str | None = None
    city: str | None = None
    state_code: str | None = None
    address: str | None = None
    zip_code: str | None = None
    phone: str | None = None
    mascot: str | None = None
    mascot_url: str | None = None
    color1: str | None = Field(default=None, description="#RRGGBB")
    color2: str | None = None
    color3: str | None = None
    sport: str | None = None
    sport_key: str | None = Field(
        default=None, description="catalogue key, backfilled from the request when the wire omits it"
    )
    gender: str | None = None
    level: str | None = None
    season: str | None = None
    year: str | None = None
    sport_season_name: str | None = None
    coach_name: str | None = None
    league_name: str | None = None
    section_name: str | None = None
    section_division_name: str | None = None
    state_name: str | None = None
    governing_body: str | None = None
    team_id: str | None = None
    sport_season_id: str | None = None
    all_season_id: str | None = None
    canonical_url: str | None = None
    tz_name: str | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)

    def team_ref(self) -> TeamRef:
        if self.canonical_url:
            ref = TeamRef.from_url(self.canonical_url)
            return ref.model_copy(
                update={
                    "season": self.year or ref.season,
                    "team_id": self.team_id,
                    "sport_season_id": self.sport_season_id,
                    "all_season_id": self.all_season_id,
                }
            )
        raise ValueError("TeamInfo has no canonical_url")
