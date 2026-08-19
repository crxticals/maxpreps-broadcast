"""Tier-3 parser: rendered HTML tables.  Last resort, deliberately conservative.

Built against the live schedule table observed during reconnaissance
(columns: Date/Time | Opponent | Tickets | Watch | Game Info; ``vs``/``@``
prefix on the opponent cell; a trailing ``*`` marks a league game).  Selector
drift degrades to warnings and partial data, never a crash — a stale value
flagged as stale beats a missing value.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup
from bs4.element import Tag

from maxpreps_broadcast.models import (
    Contest,
    ContestSide,
    ParseMode,
    Roster,
    RosterEntry,
    Schedule,
    TeamInfo,
    Venue,
)
from maxpreps_broadcast.parsers.json_api import (
    Ctx,
    _assign_records_before,
    _assign_week_indexes,
    apply_sport_hint,
)
from maxpreps_broadcast.parsers.normalize import norm_jersey, parse_height_to_inches, safe_int
from maxpreps_broadcast.parsers.timezones import localize_naive, tz_for_school

_DATE_CELL_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s*(?:(\d{1,2}):(\d{2})\s*(am|pm))?", re.IGNORECASE)
_CONTEST_ID_RE = re.compile(r"[?&]c=([0-9a-f-]{36})", re.IGNORECASE)


def _season_start_year(season: str | None) -> int:
    """``'26-27'`` → 2026.  Months Aug–Dec belong to the first year."""
    if season and re.fullmatch(r"\d{2}-\d{2}", season):
        return 2000 + int(season[:2])
    return datetime.now().year


def parse_schedule_html(
    html: str,
    *,
    team_hint: TeamInfo | None = None,
    season: str | None = None,
    sport_hint: str | None = None,
    mode: ParseMode = ParseMode.LENIENT,
) -> tuple[Schedule, Ctx]:
    ctx = Ctx(mode)
    soup = BeautifulSoup(html, "html.parser")
    team = team_hint or _team_from_html(soup, ctx)
    tz_name = team.tz_name or tz_for_school(team.state_code, team.zip_code)[0]
    start_year = _season_start_year(season or team.year)

    contests: list[Contest] = []
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        m = _DATE_CELL_RE.search(date_text)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        hour = int(m.group(3) or 19)
        minute = int(m.group(4) or 0)
        if m.group(5) and m.group(5).lower() == "pm" and hour != 12:
            hour += 12
        year = start_year if month >= 7 else start_year + 1
        try:
            naive = datetime(year, month, day, hour, minute)
        except ValueError:
            ctx.warn("bad_date", f"unparseable date cell {date_text!r}", "table.tr.date")
            continue
        local, utc = localize_naive(naive, tz_name)

        opp_cell = cells[1]
        opp_text = opp_cell.get_text(" ", strip=True)
        venue = Venue.AWAY if opp_text.lstrip().startswith("@") else Venue.HOME
        is_league = opp_text.rstrip().endswith("*") or None
        opp_name = opp_text.lstrip("@ ").lstrip("vs").strip().rstrip("*").strip()
        opp_link = opp_cell.find("a", href=True)
        opp_url = str(opp_link["href"]) if isinstance(opp_link, Tag) else None

        contest_id = None
        tickets_url = stream_url = None
        for link in row.find_all("a", href=True):
            href = str(link["href"])
            if (cm := _CONTEST_ID_RE.search(href)) and contest_id is None:
                contest_id = cm.group(1)
            if "gofan.co" in href:
                tickets_url = href
            if "nfhs" in href or "/contest/watch/" in href:
                stream_url = href

        contests.append(
            Contest(
                contest_id=contest_id,
                subject=ContestSide(school_name=team.school_name, is_home=venue is Venue.HOME),
                opponent=ContestSide(
                    school_name=opp_name or None,
                    is_home=venue is Venue.AWAY,
                    canonical_url=opp_url,
                ),
                venue=venue,
                opponent_name=opp_name or None,
                is_league_game=is_league,
                starts_at_local=local,
                starts_at_utc=utc,
                tz_name=tz_name,
                tickets_url=tickets_url,
                stream_url=stream_url,
            )
        )
    if not contests:
        ctx.warn("html_empty", "no schedule rows recognized in HTML — selectors may have drifted", "table")
    schedule = Schedule(team=team, contests=contests)
    apply_sport_hint(schedule, sport_hint, ctx)
    schedule.contests.sort(key=lambda c: (c.starts_at_utc is None, c.starts_at_utc))
    _assign_week_indexes(schedule)
    _assign_records_before(schedule)
    return schedule, ctx


def _team_from_html(soup: BeautifulSoup, ctx: Ctx) -> TeamInfo:
    title = soup.find("title")
    name = "?"
    state_code = None
    if title:
        text = title.get_text(strip=True)
        m = re.search(r"([A-Za-z .'\-]+?)\s+(?:Timberwolves|[A-Z][a-z]+)?\s*\(([^,]+),\s*([A-Z]{2})\)", text)
        if m:
            name, state_code = m.group(1).strip(), m.group(3)
    if name == "?":
        ctx.warn("html_team_unknown", "could not read team name from HTML title", "title")
    tz_name, tz_warn = tz_for_school(state_code, None)
    if tz_warn:
        ctx.warnings.append(tz_warn)
    return TeamInfo(school_name=name, state_code=state_code, tz_name=tz_name)


def parse_roster_html(html: str, *, mode: ParseMode = ParseMode.LENIENT) -> tuple[Roster, Ctx]:
    ctx = Ctx(mode)
    soup = BeautifulSoup(html, "html.parser")
    entries: list[RosterEntry] = []
    for row in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 3:
            continue
        jersey = norm_jersey(cells[0]) if re.fullmatch(r"\d{1,3}", cells[0] or "") else None
        name = cells[1] if jersey is not None else cells[0]
        if not name or name.lower() in {"name", "player"}:
            continue
        parts = name.split()
        first, last = (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (None, parts[0])
        rest = cells[2:] if jersey is not None else cells[1:]
        positions: list[str] = []
        height = weight = None
        grade = None
        for cell in rest:
            if re.fullmatch(r"(?:[A-Z]{1,3})(?:\s*[,/]\s*[A-Z]{1,3})*", cell or ""):
                positions = [p.strip() for p in re.split(r"[,/]", cell) if p.strip()]
            elif (h := parse_height_to_inches(cell)) is not None:
                height = h
            elif re.fullmatch(r"\d{2,3}", cell or "") and 80 <= int(cell) <= 400:
                weight = int(cell)
            elif cell in {"Fr.", "So.", "Jr.", "Sr.", "Freshman", "Sophomore", "Junior", "Senior"}:
                grade = cell if cell.endswith(".") else {"Freshman": "Fr.", "Sophomore": "So.",
                                                          "Junior": "Jr.", "Senior": "Sr."}[cell]
        entries.append(
            RosterEntry(
                jersey_number=jersey,
                first_name=first,
                last_name=last,
                display_name=name,
                positions=positions,
                grade_level=grade,
                height_inches=height,
                weight_lbs=safe_int(weight),
            )
        )
    if not entries:
        ctx.warn("html_empty", "no roster rows recognized in HTML — selectors may have drifted", "table")
    return Roster(entries=entries), ctx
