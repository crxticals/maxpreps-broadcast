"""Tier-1 parser: JSON payloads → normalized models.

Two JSON dialects arrive here and produce the *same* models:

* **gems** — the envelope shape of the canonical fixture in §2 of the build
  brief (``{"status": …, "data": {"team": …, "contests": [...]}}``), with its
  misnamed ``home_team``/``away_team`` keys.
* **wire** — the ``_next/data`` pageProps shape, whose ``contests`` rows are
  positional arrays (see ``keys.py``).

Traps handled here, per the brief:
  - ``home_team``/``away_team`` mean subject/opponent; venue comes only from
    ``is_home``/``homeAwayType``, never from key position.
  - Naive local datetimes get the school's IANA zone attached (DST-correct).
  - Scores/results are None pre-game; nothing downstream may assume them.
  - ``teamSize`` is untrusted and lands in ``raw_extra`` only.
  - Colors are normalized to ``#RRGGBB``.
  - ``tournaments`` is parsed as its own array.
  - Bye weeks are synthesized as entries only on request (``include_byes``).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from maxpreps_broadcast import sports
from maxpreps_broadcast.errors import SchemaDriftError, UnknownSportError
from maxpreps_broadcast.models import (
    AthleteProfile,
    Contest,
    ContestSide,
    ContestType,
    GameResult,
    GameStatus,
    ParseMode,
    ParseWarning,
    RankingEntry,
    Rankings,
    Roster,
    RosterEntry,
    Schedule,
    School,
    ScoreState,
    SideInfo,
    StatLine,
    TeamInfo,
    Tournament,
    Venue,
)
from maxpreps_broadcast.parsers import keys as K
from maxpreps_broadcast.parsers.normalize import (
    norm_hex_color,
    norm_jersey,
    parse_height_to_inches,
    pop_known,
    safe_float,
    safe_int,
    safe_str,
)
from maxpreps_broadcast.parsers.timezones import localize_naive, parse_naive_iso, tz_for_school

_PREGAME_WINDOW = timedelta(minutes=90)

#: Sports whose contests fall on a weekly cadence, so a gap is a real bye week.
#: Derived from the catalogue rather than restated, so adding a sport there is
#: enough.
WEEK_INDEXED_SPORTS = {s.slug for s in sports.all_sports() if s.week_indexed}


def _sport_entry(team: TeamInfo) -> sports.Sport | None:
    """The catalogue entry for a parsed team, by key first then by slug.

    ``sport_key`` is exact; the wire's own ``sport`` string ("Girls Volleyball",
    "Boys Volleyball") only reaches the same place after normalization, and
    gets there for the common cases.
    """
    if team.sport_key and team.sport_key in sports.BY_KEY:
        return sports.BY_KEY[team.sport_key]
    if not team.sport:
        return None
    with contextlib.suppress(UnknownSportError):
        return sports.get(team.sport)
    slug = team.sport.strip().lower().replace(" ", "-")
    return next((s for s in sports.all_sports() if s.slug == slug), None)


class Ctx:
    """Parse context: collects warnings; strict mode turns drift fatal."""

    def __init__(self, mode: ParseMode = ParseMode.LENIENT) -> None:
        self.mode = mode
        self.warnings: list[ParseWarning] = []

    def warn(self, code: str, message: str, path: str | None = None) -> None:
        if self.mode is ParseMode.STRICT and code in {"unknown_fields", "shape_unexpected", "arity_drift"}:
            raise SchemaDriftError(message, path=path or "?")
        self.warnings.append(ParseWarning(code=code, message=message, path=path))

    def require(self, d: dict[str, Any], key: str, path: str) -> Any:
        if key not in d or d[key] is None:
            raise SchemaDriftError(f"required field {key!r} missing", path=f"{path}.{key}", payload=d)
        return d[key]

    def note_unknown(self, extra: dict[str, Any], path: str) -> None:
        if extra:
            self.warn("unknown_fields", f"unknown fields preserved in raw_extra: {sorted(extra)}", path)


# ---------------------------------------------------------------- team block

_TEAM_GEMS_MAP = {
    "schoolName": "school_name", "schoolFormattedName": "school_formatted_name",
    "schoolNameAcronym": "school_acronym", "schoolCity": "city", "stateCode": "state_code",
    "schoolAddress": "address", "schoolZipCode": "zip_code", "schoolPhone": "phone",
    "schoolMascot": "mascot", "schoolMascotUrl": "mascot_url",
    "sportSeasonName": "sport_season_name", "formattedSportSeasonName": "sport",
    "gender": "gender", "sport": "sport", "season": "season", "level": "level", "year": "year",
    "coachName": "coach_name", "leagueName": "league_name", "sectionName": "section_name",
    "sectionDivisionName": "section_division_name", "stateName": "state_name",
    "schoolAssociationGoverningBodyAbbreviation": "governing_body",
    "teamId": "team_id", "sportSeasonId": "sport_season_id", "allSeasonId": "all_season_id",
    "canonicalUrl": "canonical_url",
}


def parse_team_block(raw: dict[str, Any], ctx: Ctx, *, path: str = "data.team") -> TeamInfo:
    ctx.require(raw, "schoolName", path)
    fields: dict[str, Any] = {}
    for src, dst in _TEAM_GEMS_MAP.items():
        if src in raw and raw[src] is not None:
            val = raw[src]
            fields[dst] = safe_str(val) if isinstance(val, str) else val
    # "sport" wins over formattedSportSeasonName when both present
    if raw.get("sport"):
        fields["sport"] = safe_str(raw["sport"])
    for i in (1, 2, 3):
        color = norm_hex_color(raw.get(f"schoolColor{i}"))
        if color:
            fields[f"color{i}"] = color
        elif raw.get(f"schoolColor{i}") is not None:
            ctx.warn("bad_color", f"unparseable schoolColor{i}={raw[f'schoolColor{i}']!r}", path)
    if "teamSize" in raw:
        ctx.warn(
            "team_size_untrusted",
            "teamSize present but is NOT roster size; preserved in raw_extra only",
            f"{path}.teamSize",
        )
    known = set(_TEAM_GEMS_MAP) | {"schoolColor1", "schoolColor2", "schoolColor3"}
    extra = pop_known(raw, known)
    ctx.note_unknown(extra, path)
    tz_name, tz_warn = tz_for_school(fields.get("state_code"), fields.get("zip_code"))
    if tz_warn:
        ctx.warnings.append(tz_warn)
    return TeamInfo(**fields, tz_name=tz_name, raw_extra=extra)


def team_info_from_wire_side(side: dict[str, Any], ctx: Ctx) -> TeamInfo:
    """Derive a TeamInfo from a wire currentTeam block (schedule pageProps has
    no separate team header)."""
    tz_name, tz_warn = tz_for_school(safe_str(side.get("state")), safe_str(side.get("zipCode")))
    if tz_warn:
        ctx.warnings.append(tz_warn)
    return TeamInfo(
        school_name=safe_str(side.get("name")) or "?",
        school_formatted_name=safe_str(side.get("formattedName")),
        school_acronym=safe_str(side.get("schoolNameAcronym")),
        city=safe_str(side.get("city")),
        state_code=safe_str(side.get("state")),
        address=safe_str(side.get("address")),
        zip_code=safe_str(side.get("zipCode")),
        mascot=safe_str(side.get("mascot")),
        mascot_url=safe_str(side.get("mascotUrl")),
        color1=norm_hex_color(side.get("color1")),
        color2=norm_hex_color(side.get("color2")),
        team_id=safe_str(side.get("teamId")),
        sport_season_id=safe_str(side.get("sportSeasonId")),
        canonical_url=safe_str(side.get("teamCanonicalUrl")),
        tz_name=tz_name,
    )


# ------------------------------------------------------------- contest sides

_RESULT_MAP = {"W": GameResult.WIN, "L": GameResult.LOSS, "T": GameResult.TIE}
_SIDE_GEMS_KNOWN = {"school_name", "city", "state", "result_text", "result", "score", "is_home"}


def _parse_side_gems(raw: dict[str, Any], ctx: Ctx, path: str) -> ContestSide:
    result_raw = safe_str(raw.get("result"))
    result = _RESULT_MAP.get(result_raw.upper()) if result_raw else None
    if result_raw and result is None:
        ctx.warn("bad_result", f"unrecognized result {result_raw!r}", f"{path}.result")
    extra = pop_known(raw, _SIDE_GEMS_KNOWN)
    ctx.note_unknown(extra, path)
    return ContestSide(
        school_name=safe_str(raw.get("school_name")),
        city=safe_str(raw.get("city")),
        state=safe_str(raw.get("state")),
        is_home=raw.get("is_home") if isinstance(raw.get("is_home"), bool) else None,
        score=safe_int(raw.get("score")),
        result=result,
        result_text=safe_str(raw.get("result_text")),
        raw_extra=extra,
    )


def _parse_side_wire(raw: dict[str, Any] | None, ctx: Ctx, path: str) -> ContestSide:
    if raw is None:
        return ContestSide()
    ha = raw.get("homeAwayType")
    is_home: bool | None
    if ha == K.HOME_AWAY_HOME:
        is_home = True
    elif ha == 1:
        is_home = False
    else:
        is_home = None
        if ha is not None:
            ctx.warn("home_away_unknown", f"homeAwayType={ha!r} not 0/1; venue may be neutral", path)
    result_raw = safe_str(raw.get("result"))
    result = _RESULT_MAP.get(result_raw.upper()) if result_raw else None
    calc = raw.get("calculatedTeamContestResult")
    if result is None and calc in (K.CALC_RESULT_WIN, K.CALC_RESULT_LOSS):
        result = GameResult.WIN if calc == K.CALC_RESULT_WIN else GameResult.LOSS
    return ContestSide(
        school_name=safe_str(raw.get("name")),
        formatted_name=safe_str(raw.get("formattedName")),
        acronym=safe_str(raw.get("schoolNameAcronym")),
        city=safe_str(raw.get("city")),
        state=safe_str(raw.get("state")),
        mascot_url=safe_str(raw.get("mascotUrl")),
        color1=norm_hex_color(raw.get("color1")),
        color2=norm_hex_color(raw.get("color2")),
        team_id=safe_str(raw.get("teamId")),
        sport_season_id=safe_str(raw.get("sportSeasonId")),
        canonical_url=safe_str(raw.get("teamCanonicalUrl")),
        is_home=is_home,
        score=safe_int(raw.get("score")),
        live_score=safe_int(raw.get("currentLiveScore")),
        result=result,
        result_text=safe_str(raw.get("resultString")),
    )


def _venue_from_sides(subject: ContestSide, opponent: ContestSide, ctx: Ctx, path: str) -> Venue | None:
    """Venue from the only source of truth.  Never inferred from key position."""
    if subject.is_home is True:
        if opponent.is_home is True:
            ctx.warn("venue_conflict", "both sides claim is_home=True; treating as neutral", path)
            return Venue.NEUTRAL
        return Venue.HOME
    if subject.is_home is False:
        if opponent.is_home is False:
            return Venue.NEUTRAL
        return Venue.AWAY
    return None


# ------------------------------------------------------------------ contests


def _finish_contest(
    contest: Contest, *, tz_name: str, naive_date: datetime | None, ctx: Ctx, path: str
) -> Contest:
    if naive_date is not None:
        local, utc = localize_naive(naive_date, tz_name)
        contest.starts_at_local = local
        contest.starts_at_utc = utc
        contest.tz_name = tz_name
    elif not contest.is_date_tba:
        ctx.warn("date_missing", "contest has no parseable date", f"{path}.date")
    return contest


def parse_contest_gems(raw: dict[str, Any], ctx: Ctx, *, tz_name: str, path: str) -> Contest:
    subject = _parse_side_gems(dict(ctx.require(raw, "home_team", path)), ctx, f"{path}.home_team")
    away_raw = raw.get("away_team")
    opponent = (
        _parse_side_gems(dict(away_raw), ctx, f"{path}.away_team")
        if isinstance(away_raw, dict)
        else ContestSide()
    )
    venue = _venue_from_sides(subject, opponent, ctx, path)
    if venue is None:
        ctx.warn("venue_unknown", "is_home missing on subject side; venue unknown", f"{path}.home_team")
    naive = parse_naive_iso(raw["date"]) if isinstance(raw.get("date"), str) else None
    known = {"date", "home_team", "away_team", "opponent", "result"}
    extra = pop_known(raw, known)
    ctx.note_unknown(extra, path)
    contest = Contest(
        subject=subject,
        opponent=opponent,
        venue=venue,
        opponent_name=safe_str(raw.get("opponent")) or opponent.school_name,
        result=subject.result,
        result_text=subject.result_text or safe_str(raw.get("result")),
        raw_extra=extra,
    )
    return _finish_contest(contest, tz_name=tz_name, naive_date=naive, ctx=ctx, path=path)


def parse_contest_wire(row: object, ctx: Ctx, *, tz_name: str | None, path: str) -> Contest | None:
    c = K.deserialize_object(K.CONTEST_KEYS, row)
    if c is None:
        ctx.warn("shape_unexpected", "contest row is neither array nor object", path)
        return None
    if isinstance(row, list) and len(row) != len(K.CONTEST_KEYS):
        ctx.warn(
            "arity_drift",
            f"contest row has {len(row)} fields, expected {len(K.CONTEST_KEYS)} — key list may have drifted",
            path,
        )
    subject_raw = K.deserialize_object(K.TEAM_KEYS, c.get("currentTeam"))
    opponent_raw = K.deserialize_object(K.TEAM_KEYS, c.get("opponentTeam"))
    if subject_raw is None:
        ctx.warn("shape_unexpected", "contest has no currentTeam block", f"{path}.currentTeam")
        return None
    subject = _parse_side_wire(subject_raw, ctx, f"{path}.currentTeam")
    opponent = _parse_side_wire(opponent_raw, ctx, f"{path}.opponentTeam")
    venue = _venue_from_sides(subject, opponent, ctx, path)
    if tz_name is None:
        tz_name, tz_warn = tz_for_school(subject.state, None)
        if tz_warn:
            ctx.warnings.append(tz_warn)
    naive = parse_naive_iso(c["date"]) if isinstance(c.get("date"), str) else None
    is_playoff = bool(c.get("tournamentId") or c.get("tournamentBracketId")) or None
    contest = Contest(
        contest_id=safe_str(c.get("contestId")),
        subject=subject,
        opponent=opponent,
        venue=venue,
        is_date_tba=c.get("isDateTba") is True,
        is_time_tba=c.get("isTimeTba") is True,
        location=safe_str(c.get("location")),
        details=safe_str(c.get("details")) or safe_str(c.get("description")),
        is_playoff=is_playoff,
        opponent_name=opponent.school_name,
        result=subject.result,
        result_text=subject.result_text,
        canonical_url=safe_str(c.get("canonicalUrl")),
        tickets_url=safe_str(c.get("goFanUrl")),
        stream_url=safe_str(c.get("nfhsStreamUrl")),
        is_live=c.get("isLiveGameInProgress") is True,
        current_live_period=safe_int(c.get("currentLivePeriod")),
        raw_extra={"contestState": c.get("contestState"), "isDeleted": c.get("isDeleted")},
    )
    return _finish_contest(contest, tz_name=tz_name, naive_date=naive, ctx=ctx, path=path)


# --------------------------------------------------------------- tournaments

_TOURN_KNOWN = {
    "tournamentId", "tournamentName", "bracketName", "tournamentUrl",
    "startDate", "endDate", "isTournamentPlayOff",
}


def parse_tournament(raw: dict[str, Any], ctx: Ctx, path: str) -> Tournament:
    extra = pop_known(raw, _TOURN_KNOWN)
    ctx.note_unknown(extra, path)
    return Tournament(
        tournament_id=safe_str(raw.get("tournamentId")),
        tournament_name=safe_str(raw.get("tournamentName")),
        bracket_name=safe_str(raw.get("bracketName")),
        tournament_url=safe_str(raw.get("tournamentUrl")),
        start_date=parse_naive_iso(raw["startDate"]) if isinstance(raw.get("startDate"), str) else None,
        end_date=parse_naive_iso(raw["endDate"]) if isinstance(raw.get("endDate"), str) else None,
        is_playoff=(raw.get("isTournamentPlayOff")
                    if isinstance(raw.get("isTournamentPlayOff"), bool) else None),
        raw_extra=extra,
    )


# ------------------------------------------------------------- schedule root


def parse_schedule(
    payload: dict[str, Any],
    mode: ParseMode = ParseMode.LENIENT,
    *,
    sport_hint: str | None = None,
) -> tuple[Schedule, Ctx]:
    """Dispatch on dialect; both produce the same ``Schedule``.

    ``sport_hint`` is the catalogue key the caller requested.  It has to arrive
    before the derived pass, not after: week indexes and bye weeks are
    sport-dependent, and the wire frequently omits the sport entirely.
    """
    ctx = Ctx(mode)
    page_props = payload.get("pageProps")
    if isinstance(page_props, dict) and "contests" in page_props:
        schedule = _parse_schedule_wire(page_props, ctx)
    else:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict) or "team" not in data:
            raise SchemaDriftError("schedule payload has neither pageProps.contests nor data.team", path="$")
        schedule = _parse_schedule_gems(data, ctx)
    apply_sport_hint(schedule, sport_hint, ctx)
    schedule.contests.sort(key=lambda c: (c.starts_at_utc is None, c.starts_at_utc))
    _assign_week_indexes(schedule)
    _assign_records_before(schedule)
    return schedule, ctx


def apply_sport_hint(schedule: Schedule, sport_hint: str | None, ctx: Ctx) -> None:
    """Teach a parsed schedule which sport it is, from the request that made it.

    The wire does not reliably carry ``sport``, and everything that varies by
    sport — week indexes, bye synthesis, period labels — silently no-ops when it
    is ``None``.  The requested sport always knows, so it fills the gap.
    """
    if not sport_hint:
        return
    entry = sports.BY_KEY.get(sport_hint)
    if entry is None:
        return
    schedule.team.sport_key = entry.key
    if not schedule.team.sport:
        schedule.team.sport = entry.slug
        ctx.warn(
            "sport_backfilled",
            f"payload carried no sport; assumed {entry.key!r} from the request",
            "team.sport",
        )


def _parse_schedule_gems(data: dict[str, Any], ctx: Ctx) -> Schedule:
    team = parse_team_block(dict(ctx.require(data, "team", "data")), ctx)
    contests_raw = data.get("contests")
    if not isinstance(contests_raw, list):
        raise SchemaDriftError("contests is not a list", path="data.contests", payload=data.get("contests"))
    tz_name = team.tz_name or "UTC"
    contests = [
        parse_contest_gems(dict(c), ctx, tz_name=tz_name, path=f"data.contests[{i}]")
        for i, c in enumerate(contests_raw)
        if isinstance(c, dict)
    ]
    tournaments_raw = data.get("tournaments")
    tournaments: list[Tournament] = []
    if isinstance(tournaments_raw, list):
        tournaments = [
            parse_tournament(dict(t), ctx, f"data.tournaments[{i}]")
            for i, t in enumerate(tournaments_raw)
            if isinstance(t, dict)
        ]
    elif tournaments_raw is not None:
        ctx.warn("shape_unexpected", "tournaments is not a list", "data.tournaments")
    extra = pop_known(data, {"team", "contests", "tournaments"})
    ctx.note_unknown(extra, "data")
    return Schedule(team=team, contests=contests, tournaments=tournaments)


def _wire_team_info(rows: list[Any], ctx: Ctx) -> TeamInfo | None:
    for row in rows:
        c = K.deserialize_object(K.CONTEST_KEYS, row)
        if c is None:
            continue
        side = K.deserialize_object(K.TEAM_KEYS, c.get("currentTeam"))
        if side and side.get("name"):
            return team_info_from_wire_side(side, ctx)
    return None


def _parse_schedule_wire(page_props: dict[str, Any], ctx: Ctx) -> Schedule:
    rows = page_props.get("contests")
    if not isinstance(rows, list):
        raise SchemaDriftError("pageProps.contests is not a list", path="pageProps.contests")
    team = _wire_team_info(rows, ctx)
    if team is None:
        ctx.warn("shape_unexpected", "could not derive team info from any contest row", "pageProps.contests")
        team = TeamInfo(school_name="?")
    contests: list[Contest] = []
    deleted = 0
    for i, row in enumerate(rows):
        contest = parse_contest_wire(row, ctx, tz_name=team.tz_name, path=f"pageProps.contests[{i}]")
        if contest is None:
            continue
        if contest.raw_extra.get("isDeleted") is True:
            deleted += 1
            continue
        contests.append(contest)
    if deleted:
        ctx.warn(
            "deleted_rows",
            f"filtered {deleted} soft-deleted contest rows (the site hides them)",
            "pageProps.contests",
        )
    tournaments_raw = page_props.get("tournaments")
    tournaments = (
        [
            parse_tournament(dict(t), ctx, f"pageProps.tournaments[{i}]")
            for i, t in enumerate(tournaments_raw)
            if isinstance(t, dict)
        ]
        if isinstance(tournaments_raw, list)
        else []
    )
    return Schedule(team=team, contests=contests, tournaments=tournaments)


# ------------------------------------------------- derived fields, byes, etc.


def _assign_week_indexes(schedule: Schedule) -> None:
    entry = _sport_entry(schedule.team)
    if entry is None or not entry.week_indexed:
        return
    dated = [c for c in schedule.contests if c.starts_at_local is not None]
    if not dated:
        return
    anchor = min(c.starts_at_local for c in dated if c.starts_at_local is not None)
    assert anchor is not None
    for c in dated:
        assert c.starts_at_local is not None
        delta_days = (c.starts_at_local.date() - anchor.date()).days
        c.week_index = round(delta_days / 7)


def synthesize_byes(schedule: Schedule, ctx: Ctx | None = None) -> None:
    """Insert ``ContestType.BYE`` entries for missing weeks (week-indexed sports).

    Bye weeks are gaps in the wire, not entries; this makes them explicit so a
    schedule crawl can render "WEEK 6 — BYE" instead of silently skipping.

    Only weekly sports get this.  A volleyball team playing three times in a
    week has no such thing as a bye, and inventing one would put a phantom row
    on air."""
    entry = _sport_entry(schedule.team)
    if entry is None or not entry.week_indexed:
        return
    real = [c for c in schedule.contests if c.week_index is not None and not c.is_synthesized]
    if len(real) < 2:
        return
    weeks_present = {c.week_index for c in real}
    anchor_contest = min(real, key=lambda c: c.week_index or 0)
    anchor = anchor_contest.starts_at_local
    if anchor is None:
        return
    max_week = max(w for w in weeks_present if w is not None)
    for week in range(0, max_week + 1):
        if week in weeks_present:
            continue
        bye_local = anchor + timedelta(days=7 * week)
        bye = Contest(
            contest_type=ContestType.BYE,
            subject=ContestSide(school_name=schedule.team.school_name, is_home=None),
            opponent=ContestSide(),
            venue=None,
            opponent_name=None,
            opponent_display="BYE",
            week_index=week,
            is_synthesized=True,
            starts_at_local=bye_local,
            starts_at_utc=bye_local.astimezone(UTC),
            tz_name=schedule.team.tz_name,
        )
        schedule.contests.append(bye)
        if ctx:
            ctx.warn("bye_synthesized", f"synthesized BYE for week {week}", "contests")
    schedule.contests.sort(key=lambda c: (c.starts_at_utc is None, c.starts_at_utc))


def _assign_records_before(schedule: Schedule) -> None:
    if not any(c.result is not None for c in schedule.contests):
        return
    w = losses = t = 0
    for c in schedule.contests:
        if c.contest_type is ContestType.BYE:
            continue
        c.record_before_this_game = f"{w}-{losses}" + (f"-{t}" if t else "")
        if c.result is GameResult.WIN:
            w += 1
        elif c.result is GameResult.LOSS:
            losses += 1
        elif c.result is GameResult.TIE:
            t += 1


# ---------------------------------------------------------------- scoreboard


_PERIOD_PREFIX = {"quarter": "Q", "half": "H", "period": "P", "set": "SET ", "inning": "INN "}


def _period_label(
    period: int | None, sport: sports.Sport | str | None, *, ot_alias: str | None = None
) -> str | None:
    """``Q3``/``H2``/``SET 4`` — the scorebug's period box.

    Regulation length and the noun both come from the catalogue: soccer plays
    halves, volleyball sets, baseball innings, and labelling any of them "Q3"
    would be wrong on air.
    """
    if period is None:
        return None
    entry = sport if isinstance(sport, sports.Sport) else None
    if entry is None and isinstance(sport, str):
        with contextlib.suppress(UnknownSportError):
            entry = sports.get(sport)
    regulation = entry.regulation_periods if entry else 4
    noun = entry.period_noun if entry else "quarter"
    if period <= regulation:
        return f"{_PERIOD_PREFIX.get(noun, 'P')}{period}"
    if ot_alias:
        return ot_alias
    ot_n = period - regulation
    return "OT" if ot_n == 1 else f"{ot_n}OT"


def build_score_state(
    contest: Contest,
    team: TeamInfo,
    *,
    now: datetime,
    ctx: Ctx,
) -> ScoreState:
    """Map a subject/opponent contest onto the home/away frame of a scorebug."""
    subject_home = contest.venue is not Venue.AWAY  # HOME or NEUTRAL → subject renders as home side
    subj_score, opp_score = contest.score_pair()
    subject_info = SideInfo(
        name=contest.subject.school_name or team.school_name,
        abbr=contest.subject.acronym or team.school_acronym,
        mascot_url=contest.subject.mascot_url or team.mascot_url,
        color1=contest.subject.color1 or team.color1,
        color2=contest.subject.color2 or team.color2,
        team_id=contest.subject.team_id or team.team_id,
    )
    opponent_info = SideInfo(
        name=contest.opponent.school_name,
        abbr=contest.opponent.acronym,
        mascot_url=contest.opponent.mascot_url,
        color1=contest.opponent.color1,
        color2=contest.opponent.color2,
        team_id=contest.opponent.team_id,
    )
    if contest.is_live:
        status = GameStatus.IN_PROGRESS
    elif contest.result is not None or contest.subject.score is not None:
        status = GameStatus.FINAL
    else:
        text = " ".join(x for x in (contest.details, contest.result_text) if x).lower()
        if "postpon" in text:
            status = GameStatus.POSTPONED
        elif "cancel" in text:
            status = GameStatus.CANCELED
        elif (contest.starts_at_utc is not None
              and timedelta(0) <= contest.starts_at_utc - now <= _PREGAME_WINDOW):
            status = GameStatus.PREGAME
        else:
            status = GameStatus.SCHEDULED
    period = contest.current_live_period if status is GameStatus.IN_PROGRESS else None
    return ScoreState(
        contest_id=contest.contest_id,
        status=status,
        period=period,
        period_label=_period_label(period, _sport_entry(team) or team.sport),
        clock=None,  # not carried by the server-rendered surface — see ENDPOINTS.md
        home=subject_info if subject_home else opponent_info,
        away=opponent_info if subject_home else subject_info,
        home_score=subj_score if subject_home else opp_score,
        away_score=opp_score if subject_home else subj_score,
        possession=None,
        down_and_distance=None,
        ball_on=None,
        timeouts_remaining=None,
        last_play=None,
        scoring_plays=[],
        starts_at_local=contest.starts_at_local,
        starts_at_utc=contest.starts_at_utc,
        tz_name=contest.tz_name,
        subject_is_home=contest.is_home,
        updated_at=now,
    )


# --------------------------------------------------------------- search etc.


def parse_search(payload: dict[str, Any], mode: ParseMode = ParseMode.LENIENT) -> tuple[list[School], Ctx]:
    ctx = Ctx(mode)
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    results = props.get("initialSchoolResults") if isinstance(props, dict) else None
    if results is None:
        # gems-ish alternate: {"data": {"schools": [...]}}
        data = payload.get("data")
        if isinstance(data, dict):
            results = data.get("schools")
    if results is None:
        ctx.warn("shape_unexpected", "no initialSchoolResults in search payload; returning empty", "$")
        return [], ctx
    if not isinstance(results, list):
        raise SchemaDriftError("school results is not a list", path="initialSchoolResults")
    known = {"schoolId", "name", "city", "state", "zip", "mascot", "mascotUrl", "canonicalUrl", "ranking",
             "color1", "color2"}
    schools: list[School] = []
    for i, raw in enumerate(results):
        if not isinstance(raw, dict):
            continue
        colors = [c for c in (norm_hex_color(raw.get("color1")), norm_hex_color(raw.get("color2"))) if c]
        extra = pop_known(raw, known)
        ctx.note_unknown(extra, f"initialSchoolResults[{i}]")
        schools.append(
            School(
                school_id=safe_str(raw.get("schoolId")),
                name=safe_str(raw.get("name")) or "?",
                city=safe_str(raw.get("city")),
                state=safe_str(raw.get("state")),
                zip_code=safe_str(raw.get("zip")),
                mascot=safe_str(raw.get("mascot")),
                mascot_url=safe_str(raw.get("mascotUrl")),
                canonical_url=safe_str(raw.get("canonicalUrl")),
                ranking=safe_int(raw.get("ranking")),
                colors=colors,
                raw_extra=extra,
            )
        )
    return schools, ctx


def parse_roster(payload: dict[str, Any], mode: ParseMode = ParseMode.LENIENT) -> tuple[Roster, Ctx]:
    ctx = Ctx(mode)
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    rows = props.get("athleteData") if isinstance(props, dict) else None
    if rows is None and isinstance(payload.get("data"), dict):
        rows = payload["data"].get("roster")
    if not isinstance(rows, list):
        raise SchemaDriftError("roster payload has no athleteData/roster list", path="$")
    entries: list[RosterEntry] = []
    deleted = 0
    for i, row in enumerate(rows):
        a = K.deserialize_object(K.ROSTER_KEYS, row)
        if a is None:
            ctx.warn("shape_unexpected", "roster row is neither array nor object", f"athleteData[{i}]")
            continue
        if isinstance(row, list) and len(row) != len(K.ROSTER_KEYS):
            ctx.warn(
                "arity_drift",
                f"roster row has {len(row)} fields, expected {len(K.ROSTER_KEYS)}",
                f"athleteData[{i}]",
            )
        if a.get("isDeleted") is True:
            deleted += 1
            continue
        class_year = safe_int(a.get("classYear"))
        grade = safe_str(a.get("formattedClassYear")) or {9: "Fr.", 10: "So.", 11: "Jr.", 12: "Sr."}.get(
            class_year or 0
        )
        positions = [p for p in (safe_str(a.get(f"position{n}")) for n in (1, 2, 3)) if p]
        if not positions:
            formatted = safe_str(a.get("formattedPositions"))
            if formatted:
                positions = [p.strip() for p in formatted.replace("/", ",").split(",") if p.strip()]
        entries.append(
            RosterEntry(
                jersey_number=norm_jersey(a.get("jersey")),
                first_name=safe_str(a.get("firstName")),
                last_name=safe_str(a.get("lastName")),
                display_name=safe_str(a.get("formattedName"))
                or " ".join(x for x in (safe_str(a.get("firstName")), safe_str(a.get("lastName"))) if x),
                positions=positions,
                grade_level=grade,
                class_year=class_year,
                height_inches=parse_height_to_inches(
                    a.get("calculatedHeight"), feet=a.get("heightFeet"), inches=a.get("heightInches")
                ),
                weight_lbs=safe_int(a.get("weight")),
                athlete_id=safe_str(a.get("athleteId")),
                career_profile_id=safe_str(a.get("careerProfileId")),
                photo_url=safe_str(a.get("photoUrl")),
                is_captain=a.get("isCaptain") if isinstance(a.get("isCaptain"), bool) else None,
                canonical_url=safe_str(a.get("canonicalUrl")),
            )
        )
    if deleted:
        ctx.warn(
            "deleted_rows",
            f"filtered {deleted} soft-deleted roster rows — unfiltered counts are wrong",
            "athleteData",
        )
    return Roster(entries=entries), ctx


def parse_rankings_list(
    payload: dict[str, Any], *, scope: str, mode: ParseMode = ParseMode.LENIENT
) -> tuple[Rankings, Ctx]:
    ctx = Ctx(mode)
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    lst = props.get("rankingsListData") if isinstance(props, dict) else None
    if not isinstance(lst, dict):
        raise SchemaDriftError("no rankingsListData in payload", path="pageProps.rankingsListData")
    entries: list[RankingEntry] = []
    rankings_raw = lst.get("rankings")
    if not isinstance(rankings_raw, list):
        raise SchemaDriftError("rankingsListData.rankings is not a list", path="rankingsListData.rankings")
    for i, raw in enumerate(rankings_raw):
        if not isinstance(raw, dict):
            continue
        rank = safe_int(raw.get("rank"))
        if rank is None:
            ctx.warn("bad_rank", "ranking entry without numeric rank skipped", f"rankings[{i}]")
            continue
        movement = safe_int(raw.get("movement"))
        team_link = safe_str(raw.get("teamLink"))
        team_path = None
        if team_link:
            team_path = team_link.split("://", 1)[-1].split("/", 1)[-1] if "://" in team_link else team_link
            for suffix in ("/schedule/", "/schedule"):
                if team_path.endswith(suffix):
                    team_path = team_path[: -len(suffix)]
        entries.append(
            RankingEntry(
                rank=rank,
                previous_rank=(rank + movement) if movement is not None else None,
                school_name=safe_str(raw.get("schoolName")),
                school_formatted_name=safe_str(raw.get("schoolFormattedName")),
                state_code=safe_str(raw.get("stateCode")),
                record=safe_str(raw.get("overall")),
                rating=safe_float(raw.get("rating")),
                strength=safe_float(raw.get("strength")),
                team_path=team_path,
            )
        )
    as_of_raw = safe_str(lst.get("lastUpdated"))
    as_of = None
    if as_of_raw:
        parsed = parse_naive_iso(as_of_raw)
        as_of = parsed.date() if parsed else None
    return (
        Rankings(
            scope=scope,
            ranking_type=safe_str(lst.get("year")) or "computer",
            as_of=as_of,
            total_count=safe_int(lst.get("totalCount")),
            entries=entries,
        ),
        ctx,
    )


def parse_standings_members(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """League member (school_name, team_canonical_url) pairs from a standings tab."""
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    out: list[tuple[str, str]] = []
    data = props.get("standingsData") if isinstance(props, dict) else None
    sections = data.get("standingSections") if isinstance(data, dict) else None
    if not isinstance(sections, list):
        return out
    for section in sections:
        rows = section.get("standings") if isinstance(section, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = safe_str(row.get("schoolName"))
            url = safe_str(row.get("teamCanonicalUrl"))
            if name and url:
                out.append((name, url))
    return out


def parse_league_record(payload: dict[str, Any], school_name: str) -> str | None:
    """This school's league (conference) record from a standings tab, if present."""
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    data = props.get("standingsData") if isinstance(props, dict) else None
    sections = data.get("standingSections") if isinstance(data, dict) else None
    if not isinstance(sections, list):
        return None
    target = school_name.casefold()
    for section in sections:
        rows = section.get("standings") if isinstance(section, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and safe_str(row.get("schoolName", "")).casefold() == target:  # type: ignore[union-attr]
                return safe_str(row.get("conferenceWinLossTies"))
    return None


# ------------------------------------------------------------------ athlete

_STAT_GROUP_HINTS = ("Passing", "Rushing", "Receiving", "Defense", "Kicking", "Returns",
                     "Scoring", "Hitting", "Pitching", "Fielding", "Shooting", "Serving")


def parse_athlete(
    payload: dict[str, Any],
    *,
    fallback: RosterEntry | None = None,
    mode: ParseMode = ParseMode.LENIENT,
) -> tuple[AthleteProfile, Ctx]:
    """Defensive athlete-page parse.

    The athlete page shape was only partially verified during reconnaissance
    (see ENDPOINTS.md §assumptions): known names are tried first, then a
    conservative walk collects table-shaped stat structures.  Anything found
    lands in ``stat_lines``; anything odd lands in warnings — never a crash.
    """
    ctx = Ctx(mode)
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    if not isinstance(props, dict):
        props = {}

    def first_str(*keys: str) -> str | None:
        for key in keys:
            for container in (props, props.get("careerContext") or {}, props.get("athleteContext") or {}):
                if isinstance(container, dict):
                    val = safe_str(container.get(key))
                    if val:
                        return val
        return None

    full_name = first_str("athleteName", "fullName", "careerName", "name")
    first = last = None
    if full_name and " " in full_name:
        first, _, last = full_name.partition(" ")
    if full_name is None and fallback is not None:
        full_name = fallback.display_name
        first, last = fallback.first_name, fallback.last_name

    stat_lines: list[StatLine] = []
    stat_groups: list[str] = []

    def collect(node: Any, path: str, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            group = safe_str(node.get("groupName") or node.get("statGroupName") or node.get("title"))
            columns = node.get("columns")
            rows = node.get("rows") or node.get("stats")
            if group and isinstance(columns, list) and isinstance(rows, list):
                header = [safe_str(c) or f"col{i}" for i, c in enumerate(columns)]
                for row in rows:
                    if isinstance(row, list) and len(row) == len(header):
                        stats = {str(header[i]): row[i] for i in range(len(header))}
                        stat_lines.append(
                            StatLine(group=group, stats=stats, display_order=[str(h) for h in header])
                        )
                if group not in stat_groups:
                    stat_groups.append(group)
                return
            for key, value in node.items():
                collect(value, f"{path}.{key}", depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node[:50]):
                collect(item, f"{path}[{i}]", depth + 1)

    collect(props, "pageProps")
    if not stat_lines:
        # Flat name→value dicts keyed by a recognizable group name.
        for key, value in props.items():
            if isinstance(value, dict) and any(h.lower() in key.lower() for h in _STAT_GROUP_HINTS):
                flat = {k: v for k, v in value.items() if isinstance(v, (int, float, str))}
                if flat:
                    stat_lines.append(StatLine(group=key, stats=flat, display_order=list(flat)))
                    stat_groups.append(key)
    if not stat_lines:
        ctx.warn("athlete_stats_unrecognized",
                 "no stat tables recognized on athlete page; profile carries identity only",
                 "pageProps")

    profile = AthleteProfile(
        athlete_id=first_str("athleteId", "careerId", "careerProfileId")
        or (fallback.career_profile_id if fallback else None),
        full_name=full_name or "?",
        first_name=first,
        last_name=last,
        school_name=first_str("schoolName"),
        positions=list(fallback.positions) if fallback else [],
        jersey_number=fallback.jersey_number if fallback else None,
        grade_level=first_str("classYear", "grade") or (fallback.grade_level if fallback else None),
        height_inches=fallback.height_inches if fallback else None,
        weight_lbs=fallback.weight_lbs if fallback else None,
        photo_url=first_str("photoUrl", "athletePhotoUrl") or (fallback.photo_url if fallback else None),
        canonical_url=first_str("canonicalUrl", "careerCanonicalUrl")
        or (fallback.canonical_url if fallback else None),
        stat_lines=stat_lines,
        stat_groups=stat_groups,
    )
    return profile, ctx


def parse_rankings_contexts(payload: dict[str, Any], *, scope: str, want: str) -> Rankings | None:
    """League/division windows from a *team* rankings tab (contexts list).

    Shape assumed from reconnaissance notes, parsed defensively: a list of
    context blocks each carrying a name and a small entries window."""
    props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else payload
    data = props.get("rankingsData") if isinstance(props, dict) else None
    contexts = data.get("contexts") if isinstance(data, dict) else None
    if not isinstance(contexts, list):
        return None
    want_l = want.casefold()
    for context in contexts:
        if not isinstance(context, dict):
            continue
        name = safe_str(context.get("name") or context.get("contextName") or context.get("title")) or ""
        if want_l not in name.casefold():
            continue
        rows = context.get("entries") or context.get("rankings") or context.get("teams")
        if not isinstance(rows, list):
            continue
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rank = safe_int(row.get("rank"))
            if rank is None:
                continue
            entries.append(
                RankingEntry(
                    rank=rank,
                    school_name=safe_str(row.get("schoolName") or row.get("name")),
                    record=safe_str(row.get("overall") or row.get("record")),
                    rating=safe_float(row.get("rating")),
                    team_path=safe_str(row.get("teamLink") or row.get("teamCanonicalUrl")),
                )
            )
        if entries:
            return Rankings(scope=scope, ranking_type=name, entries=entries, total_count=len(entries))
    return None
