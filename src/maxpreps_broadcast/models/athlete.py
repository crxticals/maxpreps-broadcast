"""Athlete profile models.

Stats are sport-specific, so ``StatLine`` is a typed-but-open mapping plus a
``stat_groups`` ordering hint so a template can render passing/rushing/
receiving blocks in a deterministic order.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StatLine(BaseModel):
    group: str = Field(description="e.g. 'passing', 'rushing', 'hitting'")
    season: str | None = None
    sport: str | None = None
    stats: dict[str, float | int | str | None] = Field(default_factory=dict)
    display_order: list[str] = Field(default_factory=list, description="column order for rendering")


class TeamHistoryEntry(BaseModel):
    school_name: str | None = None
    sport: str | None = None
    season: str | None = None
    level: str | None = None
    canonical_url: str | None = None


class AthleteProfile(BaseModel):
    athlete_id: str | None = None
    career_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    grade_level: str | None = None
    class_year: int | None = None
    height_inches: int | None = None

    @property
    def height_display(self) -> str | None:
        if self.height_inches is None:
            return None
        return f"{self.height_inches // 12}'{self.height_inches % 12}\""
    weight_lbs: int | None = None
    positions: list[str] = Field(default_factory=list)
    jersey_number: str | None = None
    photo_url: str | None = None
    school_name: str | None = None
    canonical_url: str | None = None
    stat_lines: list[StatLine] = Field(default_factory=list)
    stat_groups: list[str] = Field(default_factory=list, description="render order of stat groups")
    team_history: list[TeamHistoryEntry] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    raw_extra: dict[str, Any] = Field(default_factory=dict)
