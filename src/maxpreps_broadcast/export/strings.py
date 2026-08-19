"""Broadcast strings: every value pre-formatted for direct wiring to an AE
text layer.  Nothing here requires expressions beyond ``sourceText``.

Each field has an ``_upper`` variant and respects per-field character budgets
from config (graceful word-boundary truncation — a scorebug has ~12 usable
characters and knows nothing about your school's 34-character name).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from maxpreps_broadcast.config import ExportConfig, Settings
from maxpreps_broadcast.models import (
    Contest,
    ContestType,
    GameStatus,
    Schedule,
    ScoreState,
)
from maxpreps_broadcast.parsers.normalize import acronym_from_name, fit_budget, ordinal_upper, upper_display

_STATUS_LABEL = {
    GameStatus.SCHEDULED: "Scheduled",
    GameStatus.PREGAME: "Pregame",
    GameStatus.IN_PROGRESS: "Live",
    GameStatus.HALFTIME: "Halftime",
    GameStatus.FINAL: "Final",
    GameStatus.POSTPONED: "Postponed",
    GameStatus.CANCELED: "Canceled",
}


def _budget(config: ExportConfig, field: str, default: int = 64) -> int:
    return config.char_budgets.get(field, default)


def _with_upper(out: dict[str, Any], key: str, value: str | None) -> None:
    out[key] = value or ""
    out[f"{key}_upper"] = upper_display(value) if value else ""


def team_abbr(settings: Settings, name: str | None, fallback_acronym: str | None = None) -> str:
    if name is None:
        return "TBD"
    override = settings.opponent_abbr(name)
    if override:
        return override
    if fallback_acronym:
        return fallback_acronym
    return acronym_from_name(name)


def _day_display(local: datetime) -> str:
    """→ ``FRI AUG 21``.  The unpadded day is built by hand: ``%-d`` is a glibc
    extension the Windows CRT rejects with ``ValueError``."""
    return f"{local:%a %b} {local.day}".upper()


def kickoff_display(local: datetime | None, *, time_tba: bool = False) -> str:
    """→ ``FRI AUG 21 · 7:00 PM`` (or ``… · TIME TBA``)."""
    if local is None:
        return "DATE TBA"
    day = _day_display(local)
    if time_tba:
        return f"{day} · TIME TBA"
    clock = f"{local.hour % 12 or 12}:{local:%M %p}"
    return f"{day} · {clock}"


def score_line(home_abbr: str, home_score: int | None, away_abbr: str, away_score: int | None) -> str:
    """→ ``NW 21 — IRV 14`` (dashes pre-game)."""
    hs = "–" if home_score is None else str(home_score)
    as_ = "–" if away_score is None else str(away_score)
    return f"{home_abbr} {hs} — {away_abbr} {as_}"


def down_distance_display(down: int | None, distance: int | str | None) -> str:
    if down is None:
        return ""
    dist = str(distance) if distance is not None else "?"
    if isinstance(distance, str) and distance.lower() == "goal":
        dist = "GOAL"
    return f"{ordinal_upper(down)} & {dist}"


def record_display(schedule: Schedule, *, league_label: str | None = None,
                   league_record: str | None = None) -> str:
    """→ ``3-1, 2-0 IOTA`` — overall from the schedule, league part when known."""
    overall = schedule.record_display()
    league = league_record or schedule.record(league_only=True)
    label = upper_display(league_label or schedule.team.league_name or "") or "LEAGUE"
    if (league and league not in {"", "0-0"}) or (league and any(
        c.is_league_game and c.result is not None for c in schedule.contests
    )):
        return f"{overall}, {league} {label}"
    return overall


def next_game_display(contest: Contest | None, settings: Settings) -> str:
    """→ ``FRI AUG 21 · VS IRVINE`` / ``… · AT BREA OLINDA`` / ``BYE WEEK``."""
    if contest is None:
        return ""
    if contest.contest_type is ContestType.BYE:
        return "BYE WEEK"
    prefix = "AT" if contest.is_home is False else "VS"
    opp = settings.opponent_display(contest.opponent_name) or contest.opponent_display or "TBD"
    when = _day_display(contest.starts_at_local) if contest.starts_at_local else "TBA"
    return f"{when} · {prefix} {upper_display(opp)}"


def clock_display(state: ScoreState) -> str:
    if state.status in {GameStatus.SCHEDULED, GameStatus.PREGAME}:
        return kickoff_display(state.starts_at_local)
    if state.status is GameStatus.FINAL:
        return "FINAL"
    parts = [p for p in (state.period_label, state.clock) if p]
    return " ".join(parts) or _STATUS_LABEL[state.status].upper()


def strings_for_score(state: ScoreState, settings: Settings) -> dict[str, Any]:
    config = settings.export
    home_abbr = fit_budget(
        team_abbr(settings, state.home.name, state.home.abbr), _budget(config, "home_abbr", 4)
    )
    away_abbr = fit_budget(
        team_abbr(settings, state.away.name, state.away.abbr), _budget(config, "away_abbr", 4)
    )
    out: dict[str, Any] = {}
    _with_upper(out, "home_name", fit_budget(state.home.name or "TBD", 24))
    _with_upper(out, "away_name", fit_budget(state.away.name or "TBD", 24))
    out["home_abbr"] = home_abbr
    out["away_abbr"] = away_abbr
    out["home_score"] = "" if state.home_score is None else str(state.home_score)
    out["away_score"] = "" if state.away_score is None else str(state.away_score)
    out["score_line"] = fit_budget(
        score_line(home_abbr, state.home_score, away_abbr, state.away_score),
        _budget(config, "score_line", 18),
    )
    _with_upper(out, "status", _STATUS_LABEL[state.status])
    out["period_display"] = state.period_label or ""
    out["clock_display"] = clock_display(state)
    out["down_distance_display"] = state.down_and_distance or ""
    out["possession_side"] = state.possession or ""
    out["kickoff_display"] = fit_budget(
        kickoff_display(state.starts_at_local), _budget(config, "kickoff_display", 22)
    )
    out["is_live"] = state.status is GameStatus.IN_PROGRESS
    out["updated_at"] = state.updated_at.isoformat() if state.updated_at else ""
    return out


def strings_for_schedule(schedule: Schedule, settings: Settings, *, now: datetime | None = None,
                         league_record: str | None = None) -> dict[str, Any]:
    config = settings.export
    next_game = schedule.next_contest(now=now)
    out: dict[str, Any] = {}
    _with_upper(out, "team_name", schedule.team.school_name)
    _with_upper(out, "team_mascot", schedule.team.mascot)
    out["team_abbr"] = team_abbr(settings, schedule.team.school_name, schedule.team.school_acronym)
    out["record_display"] = fit_budget(
        record_display(schedule, league_record=league_record), _budget(config, "record_display", 16)
    )
    out["next_game_display"] = fit_budget(
        next_game_display(next_game, settings), _budget(config, "next_game_display", 26)
    )
    out["next_kickoff_display"] = fit_budget(
        kickoff_display(
            next_game.starts_at_local if next_game else None,
            time_tba=bool(next_game and next_game.is_time_tba),
        ),
        _budget(config, "kickoff_display", 22),
    )
    _with_upper(out, "league_name", schedule.team.league_name)
    _with_upper(out, "coach_name", schedule.team.coach_name)
    return out
