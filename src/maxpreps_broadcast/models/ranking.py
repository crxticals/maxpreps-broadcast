"""Ranking models."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, computed_field

from maxpreps_broadcast.models.team import TeamRef


class RankingEntry(BaseModel):
    rank: int
    previous_rank: int | None = None
    school_name: str | None = None
    school_formatted_name: str | None = None
    state_code: str | None = None
    record: str | None = None
    rating: float | None = None
    strength: float | None = None
    team_path: str | None = Field(default=None, description="site-relative team path when known")
    raw_extra: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rank_delta(self) -> int | None:
        """Positive = moved up (improved)."""
        if self.previous_rank is None:
            return None
        return self.previous_rank - self.rank


class Rankings(BaseModel):
    scope: str = "state"
    ranking_type: str | None = None
    as_of: date | None = None
    total_count: int | None = None
    entries: list[RankingEntry] = Field(default_factory=list)

    def find(self, team: TeamRef | str) -> RankingEntry | None:
        """The given team's entry within this scope, or None."""
        if isinstance(team, TeamRef):
            needles = {team.school, team.school.split("-")[0]}
            for e in self.entries:
                path = (e.team_path or "").lower()
                if any(n and n in path for n in needles):
                    return e
            name = team.school.replace("-", " ").lower()
            for e in self.entries:
                if (e.school_name or "").lower() in name or name.startswith((e.school_name or "~").lower()):
                    return e
            return None
        needle = team.lower()
        for e in self.entries:
            if needle in (e.school_name or "").lower() or needle in (e.school_formatted_name or "").lower():
                return e
        return None
