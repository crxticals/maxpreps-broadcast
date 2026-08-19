"""Roster models.

Jersey numbers are strings: ``"00"`` and ``"07"`` are real and distinct from
``"0"`` and ``"7"``.  Broadcast-formatted variants (``lower_third_name``,
``full_upper``, ``jersey_padded``) are precomputed so the AE artist never
writes string logic in an expression.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field

from maxpreps_broadcast.models.team import TeamInfo
from maxpreps_broadcast.parsers.normalize import fit_budget, initial_last, upper_display

_POSITION_GROUP_ORDER = [
    ("QB",), ("RB", "FB", "HB", "TB"), ("WR", "SE", "FL"), ("TE",),
    ("OL", "C", "G", "OG", "T", "OT", "LT", "RT"),
    ("DL", "DE", "DT", "NT", "NG"), ("LB", "ILB", "OLB", "MLB", "EDGE"),
    ("DB", "CB", "S", "FS", "SS"), ("K", "PK"), ("P",), ("LS",), ("ATH",),
]


def position_group_rank(positions: list[str]) -> int:
    for pos in positions:
        p = pos.upper().strip()
        for i, group in enumerate(_POSITION_GROUP_ORDER):
            if p in group:
                return i
    return len(_POSITION_GROUP_ORDER)


class RosterSort(str, Enum):
    JERSEY = "jersey"
    LAST_NAME = "last_name"
    POSITION = "position"
    GRADE = "grade"


class RosterEntry(BaseModel):
    jersey_number: str | None = Field(default=None, description="string — leading zeros and 00 are real")
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    positions: list[str] = Field(default_factory=list)
    grade_level: str | None = Field(default=None, description="e.g. 'Sr.'")
    class_year: int | None = Field(default=None, description="numeric 9–12 when known")
    height_inches: int | None = Field(default=None, description="total inches")
    weight_lbs: int | None = None
    athlete_id: str | None = None
    career_profile_id: str | None = None
    photo_url: str | None = None
    is_captain: bool | None = None
    canonical_url: str | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lower_third_name(self) -> str:
        """``J. NGUYEN`` — first initial + upper-cased last name."""
        return initial_last(self.first_name, self.last_name)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_upper(self) -> str:
        name = self.display_name or " ".join(x for x in (self.first_name, self.last_name) if x)
        return upper_display(name)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jersey_padded(self) -> str:
        """Zero-padded to two characters; ``"00"`` stays ``"00"``."""
        j = (self.jersey_number or "").strip()
        if not j:
            return "--"
        return j if len(j) >= 2 else j.zfill(2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def height_display(self) -> str | None:
        if self.height_inches is None:
            return None
        return f"{self.height_inches // 12}'{self.height_inches % 12}\""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def positions_display(self) -> str:
        return "/".join(self.positions)

    def truncate(self, n: int) -> str:
        """The lower-third name fitted to an ``n``-character budget."""
        return fit_budget(self.lower_third_name, n)

    def _jersey_sort_key(self) -> tuple[int, str]:
        j = (self.jersey_number or "").strip()
        try:
            return (int(j), j)
        except ValueError:
            return (10_000, j)


def sort_roster(entries: list[RosterEntry], by: RosterSort = RosterSort.JERSEY) -> list[RosterEntry]:
    if by is RosterSort.JERSEY:
        return sorted(entries, key=lambda e: e._jersey_sort_key())
    if by is RosterSort.LAST_NAME:
        return sorted(entries, key=lambda e: ((e.last_name or "~").lower(), (e.first_name or "").lower()))
    if by is RosterSort.POSITION:
        return sorted(entries, key=lambda e: (position_group_rank(e.positions), e._jersey_sort_key()))
    return sorted(entries, key=lambda e: (-(e.class_year or 0), (e.last_name or "~").lower()))


class Roster(BaseModel):
    team: TeamInfo | None = None
    entries: list[RosterEntry] = Field(default_factory=list)

    def sorted(self, by: RosterSort = RosterSort.JERSEY) -> Roster:
        return Roster(team=self.team, entries=sort_roster(self.entries, by))
