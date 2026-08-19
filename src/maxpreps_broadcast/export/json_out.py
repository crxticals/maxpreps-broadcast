"""Flat JSON views for After Effects.

AE expression access is miserable beyond one nesting level, so every export
here is flat (or a flat list of flat objects), stringly-typed where a text
layer is the consumer, and paired with the colors + broadcast strings blocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from maxpreps_broadcast import sports
from maxpreps_broadcast.config import Settings
from maxpreps_broadcast.export import strings as S
from maxpreps_broadcast.export.atomic import atomic_write_json
from maxpreps_broadcast.export.colors import TeamColorBlock, cache_mascot
from maxpreps_broadcast.models import (
    ContestType,
    Response,
    Roster,
    Schedule,
    ScoreState,
)
from maxpreps_broadcast.parsers.normalize import upper_display


def sport_fields(sport_key: str | None) -> dict[str, Any]:
    """Identity block so a template can label the sport it is showing.

    Every export carries it, including single-sport ones — a graphics loop that
    rotates six files needs to know which one it just loaded.
    """
    entry = sports.BY_KEY.get(sport_key or "")
    return {
        "sport": entry.key if entry else (sport_key or ""),
        "sport_display": entry.display if entry else "",
        "sport_display_upper": upper_display(entry.display) if entry else "",
        "sport_season": entry.season.value if entry else "",
    }


def _meta(resp: Response[Any]) -> dict[str, Any]:
    return {
        "fetched_at": resp.fetched_at.isoformat(),
        "source_tier": resp.source_tier,
        "cache_state": resp.cache_state,
        "data_age_seconds": round(resp.data_age_seconds, 1),
        "stale": resp.stale,
        "warning_count": len(resp.warnings),
    }


def live_view(
    resp: Response[ScoreState],
    settings: Settings,
    *,
    assets_dir: Path | None = None,
    sport: str | None = None,
) -> dict[str, Any]:
    state = resp.data
    out: dict[str, Any] = {
        **S.strings_for_score(state, settings), **_meta(resp), **sport_fields(sport)
    }
    out.update(TeamColorBlock.from_hexes(state.home.color1, state.home.color2).flat("home"))
    out.update(TeamColorBlock.from_hexes(state.away.color1, state.away.color2).flat("away"))
    if assets_dir is not None:
        home_logo = cache_mascot(state.home.mascot_url, assets_dir)
        away_logo = cache_mascot(state.away.mascot_url, assets_dir)
        out["home_logo_path"] = str(home_logo) if home_logo else ""
        out["away_logo_path"] = str(away_logo) if away_logo else ""
    out["contest_id"] = state.contest_id or ""
    return out


def schedule_view(resp: Response[Schedule], settings: Settings) -> dict[str, Any]:
    schedule = resp.data
    rows: list[dict[str, Any]] = []
    for contest in schedule.contests:
        is_bye = contest.contest_type is ContestType.BYE
        rows.append(
            {
                "week": contest.week_index if contest.week_index is not None else "",
                "date_display": S.kickoff_display(
                    contest.starts_at_local, time_tba=contest.is_time_tba
                ) if not is_bye else "",
                "opponent": "" if is_bye else (settings.opponent_display(contest.opponent_name) or ""),
                "opponent_upper": "" if is_bye else (
                    upper_display(settings.opponent_display(contest.opponent_name) or "")
                ),
                "home_away": "BYE" if is_bye else (
                    "HOME" if contest.is_home
                    else "AWAY" if contest.is_home is False
                    else "NEUTRAL"
                ),
                "vs_at": "" if is_bye else ("AT" if contest.is_home is False else "VS"),
                "result": contest.result.value.upper() if contest.result else "",
                "result_text": contest.result_text or "",
                "score_us": "" if contest.subject.score is None else str(contest.subject.score),
                "score_them": "" if contest.opponent.score is None else str(contest.opponent.score),
                "record_before": contest.record_before_this_game or "",
                "is_league": bool(contest.is_league_game),
                "is_live": contest.is_live,
                "contest_id": contest.contest_id or "",
            }
        )
    out: dict[str, Any] = {
        **S.strings_for_schedule(schedule, settings),
        **_meta(resp),
        **sport_fields(schedule.team.sport_key),
        "games": rows,
    }
    out.update(
        TeamColorBlock.from_hexes(
            schedule.team.color1, schedule.team.color2, schedule.team.color3
        ).flat("team")
    )
    return out


def roster_view(
    resp: Response[Roster], settings: Settings, *, sport: str | None = None
) -> dict[str, Any]:
    roster = resp.data
    budget = settings.export.char_budgets.get("lower_third_name", 18)
    players = [
        {
            "jersey": entry.jersey_number or "",
            "jersey_padded": entry.jersey_padded,
            "name": entry.display_name or "",
            "lower_third_name": entry.truncate(budget),
            "full_upper": entry.full_upper,
            "positions": entry.positions_display,
            "grade": entry.grade_level or "",
            "height": entry.height_display or "",
            "weight": "" if entry.weight_lbs is None else str(entry.weight_lbs),
            "is_captain": bool(entry.is_captain),
        }
        for entry in roster.entries
    ]
    return {
        "players": players, "player_count": len(players), **_meta(resp), **sport_fields(sport)
    }


def sport_filename(stem: str, suffix: str, sport: str | None) -> str:
    """``live.json`` → ``live.girls-volleyball.json``.

    The sport goes in the filename rather than a directory so every export
    lands in one flat folder — which is what a Google Drive sync mirrors onto a
    production machine most reliably, and what lets an operator see all six
    sports at a glance.  ``None`` keeps the bare name, so single-sport setups
    and existing After Effects projects are untouched.
    """
    return f"{stem}.{sport}.{suffix}" if sport else f"{stem}.{suffix}"


def write_views(
    out_dir: Path,
    settings: Settings,
    *,
    live: Response[ScoreState] | None = None,
    schedule: Response[Schedule] | None = None,
    roster: Response[Roster] | None = None,
    assets_dir: Path | None = None,
    sport: str | None = None,
) -> list[Path]:
    written: list[Path] = []
    stamp = datetime.now(UTC).isoformat()

    def name(stem: str) -> Path:
        return out_dir / sport_filename(stem, "json", sport)

    if live is not None:
        view = live_view(live, settings, assets_dir=assets_dir, sport=sport)
        view["exported_at"] = stamp
        written.append(atomic_write_json(name("live"), view))
    if schedule is not None:
        view = schedule_view(schedule, settings)
        view["exported_at"] = stamp
        written.append(atomic_write_json(name("schedule"), view))
    if roster is not None:
        view = roster_view(roster, settings, sport=sport)
        view["exported_at"] = stamp
        written.append(atomic_write_json(name("roster"), view))
    return written
