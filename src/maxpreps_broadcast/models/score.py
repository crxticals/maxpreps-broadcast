"""Live score state — the model the scorebug binds to.

Sport-specific fields (down/distance, possession, timeouts…) degrade to
``None`` rather than raising for sports that lack them, and for source tiers
that do not carry them.  MaxPreps' server-rendered surface exposes live
period and live scores but not the play-by-play channel, so those fields are
honest ``None``s unless a richer source appears (see docs/ENDPOINTS.md).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

Possession = Literal["home", "away"]


class GameStatus(str, Enum):
    SCHEDULED = "scheduled"
    PREGAME = "pregame"
    IN_PROGRESS = "in_progress"
    HALFTIME = "halftime"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"


class ScoringPlay(BaseModel):
    period: int | None = None
    clock: str | None = None
    team: Literal["home", "away"] | None = None
    description: str | None = None
    home_score: int | None = None
    away_score: int | None = None


class SideInfo(BaseModel):
    """Identity for one side of the scorebug."""

    name: str | None = None
    abbr: str | None = None
    mascot_url: str | None = None
    color1: str | None = None
    color2: str | None = None
    team_id: str | None = None


class ScoreState(BaseModel):
    contest_id: str | None = None
    status: GameStatus = GameStatus.SCHEDULED
    period: int | None = None
    period_label: str | None = Field(default=None, description='"Q3", "OT", "HALF"')
    clock: str | None = None
    home: SideInfo = Field(default_factory=SideInfo)
    away: SideInfo = Field(default_factory=SideInfo)
    home_score: int | None = None
    away_score: int | None = None
    line_score_home: list[int] | None = None
    line_score_away: list[int] | None = None
    possession: Possession | None = None
    down_and_distance: str | None = None
    ball_on: str | None = None
    timeouts_remaining: dict[str, int] | None = None
    last_play: str | None = None
    scoring_plays: list[ScoringPlay] = Field(default_factory=list)
    starts_at_local: datetime | None = None
    starts_at_utc: datetime | None = None
    tz_name: str | None = None
    subject_is_home: bool | None = Field(
        default=None, description="which side the primary team is, when derived from its schedule"
    )
    updated_at: datetime | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)

    def score_tuple(self) -> tuple[int | None, int | None]:
        return self.home_score, self.away_score


class ChangeEvent(BaseModel):
    """One observed delta between consecutive polls of the same contest."""

    field: Literal["score", "period", "possession", "status", "clock"]
    old: Any = None
    new: Any = None


def diff_score_states(prev: ScoreState | None, cur: ScoreState) -> list[ChangeEvent]:
    """Emit changes only when something actually changed, so the template can
    fire an animation on a real event rather than on every poll."""
    if prev is None or prev.contest_id != cur.contest_id:
        return []
    changes: list[ChangeEvent] = []
    if (prev.home_score, prev.away_score) != (cur.home_score, cur.away_score):
        changes.append(
            ChangeEvent(
                field="score",
                old={"home": prev.home_score, "away": prev.away_score},
                new={"home": cur.home_score, "away": cur.away_score},
            )
        )
    if prev.period != cur.period:
        changes.append(ChangeEvent(field="period", old=prev.period, new=cur.period))
    if prev.possession != cur.possession:
        changes.append(ChangeEvent(field="possession", old=prev.possession, new=cur.possession))
    if prev.status != cur.status:
        changes.append(ChangeEvent(field="status", old=prev.status.value, new=cur.status.value))
    if prev.clock != cur.clock and (prev.clock or cur.clock):
        changes.append(ChangeEvent(field="clock", old=prev.clock, new=cur.clock))
    return changes
