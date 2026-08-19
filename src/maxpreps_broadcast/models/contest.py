"""Contest (game) models.

The wire's ``home_team`` / ``away_team`` keys are misnamed — they mean *subject
team* and *opponent* — so this model refuses to encode venue positionally.
There is exactly one venue field, derived from the only source of truth
(``is_home`` / ``homeAwayType``), and the sides are named ``subject`` and
``opponent``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from maxpreps_broadcast.models.team import TeamInfo


class Venue(str, Enum):
    HOME = "home"
    AWAY = "away"
    NEUTRAL = "neutral"


class GameResult(str, Enum):
    WIN = "W"
    LOSS = "L"
    TIE = "T"


class ContestType(str, Enum):
    GAME = "game"
    BYE = "bye"
    TOURNAMENT = "tournament"


class ContestSide(BaseModel):
    """One side of a contest.  Scores/results are None pre-game — always."""

    school_name: str | None = None
    formatted_name: str | None = None
    acronym: str | None = None
    city: str | None = None
    state: str | None = None
    mascot_url: str | None = None
    color1: str | None = None
    color2: str | None = None
    team_id: str | None = None
    sport_season_id: str | None = None
    canonical_url: str | None = None
    is_home: bool | None = None
    score: int | None = None
    live_score: int | None = None
    result: GameResult | None = None
    result_text: str | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)


class Contest(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    contest_id: str | None = None
    contest_type: ContestType = ContestType.GAME
    subject: ContestSide
    opponent: ContestSide
    venue: Venue | None = None
    starts_at_local: datetime | None = Field(default=None, description="tz-aware, school-local")
    starts_at_utc: datetime | None = None
    tz_name: str | None = None
    is_date_tba: bool = False
    is_time_tba: bool = False
    location: str | None = None
    details: str | None = None
    is_league_game: bool | None = None
    is_playoff: bool | None = None
    week_index: int | None = None
    opponent_name: str | None = None
    opponent_display: str | None = Field(default=None, description="after the opponent override table")
    record_before_this_game: str | None = None
    result: GameResult | None = None
    result_text: str | None = None
    canonical_url: str | None = None
    tickets_url: str | None = None
    stream_url: str | None = None
    is_live: bool = False
    current_live_period: int | None = None
    is_synthesized: bool = Field(default=False, description="True for synthesized BYE entries")
    raw_extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_home(self) -> bool | None:
        if self.venue is None:
            return None
        return self.venue == Venue.HOME

    def days_until(self, *, today: date | None = None) -> int | None:
        """Whole days from ``today`` to kickoff in the school's timezone."""
        if self.starts_at_local is None:
            return None
        today = today or datetime.now(self.starts_at_local.tzinfo or UTC).date()
        return (self.starts_at_local.date() - today).days

    @property
    def has_result(self) -> bool:
        return self.result is not None or self.subject.score is not None

    def score_pair(self) -> tuple[int | None, int | None]:
        """(subject, opponent) preferring final score, falling back to live."""
        s = self.subject.score if self.subject.score is not None else self.subject.live_score
        o = self.opponent.score if self.opponent.score is not None else self.opponent.live_score
        return s, o


class Tournament(BaseModel):
    tournament_id: str | None = None
    tournament_name: str | None = None
    bracket_name: str | None = None
    tournament_url: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    is_playoff: bool | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)


class Schedule(BaseModel):
    """A full season schedule: team info, ordered contests, and tournaments."""

    team: TeamInfo
    contests: list[Contest] = Field(default_factory=list)
    tournaments: list[Tournament] = Field(default_factory=list)

    def record(self, *, league_only: bool = False) -> tuple[int, int, int]:
        w = losses = t = 0
        for c in self.contests:
            if c.contest_type is ContestType.BYE or c.result is None:
                continue
            if league_only and not c.is_league_game:
                continue
            if c.result is GameResult.WIN:
                w += 1
            elif c.result is GameResult.LOSS:
                losses += 1
            else:
                t += 1
        return w, losses, t

    def record_display(self) -> str:
        w, losses, t = self.record()
        return f"{w}-{losses}" + (f"-{t}" if t else "")

    def next_contest(self, *, now: datetime | None = None) -> Contest | None:
        now = now or datetime.now(UTC)
        upcoming = [
            c
            for c in self.contests
            if c.contest_type is not ContestType.BYE
            and c.starts_at_utc is not None
            and c.starts_at_utc >= now
            and c.result is None
        ]
        return min(upcoming, key=lambda c: c.starts_at_utc or now) if upcoming else None

    def live_contest(self) -> Contest | None:
        for c in self.contests:
            if c.is_live:
                return c
        return None

    def last_completed(self) -> Contest | None:
        done = [c for c in self.contests if c.has_result and c.starts_at_utc is not None]
        return max(done, key=lambda c: c.starts_at_utc or datetime.now(UTC)) if done else None
