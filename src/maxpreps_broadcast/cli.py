"""The ``maxpreps`` command line.

    maxpreps init                       # interactive setup wizard
    maxpreps search "northwood"         # school search
    maxpreps sports season fall         # cover a season's sports (max 6)
    maxpreps sports set|add|remove|list # adjust the active sports
    maxpreps schedule [--byes]          # season schedule table
    maxpreps roster [--sort jersey]     # roster table
    maxpreps live                       # current scorebug state
    maxpreps game <contest-id>          # one specific contest
    maxpreps games --scope league       # everything live/upcoming in scope
    maxpreps athlete "j nguyen"         # athlete profile
    maxpreps rankings --scope state     # rankings (+ where's-my-team)
    maxpreps export --format json,mgjson,csv
    maxpreps render --template scorebug # apply a template mapping
    maxpreps serve [--watch]            # local API / watch daemon
    maxpreps doctor                     # live shape checks vs expectations
    maxpreps stats                      # counters and latency percentiles

Commands that read team data cover every *active* sport by default and accept
``--sport`` to narrow to one.

Global flags: ``--config``, ``--profile``, ``--offline``, ``--strict``.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from maxpreps_broadcast import __version__, sports
from maxpreps_broadcast.config import (
    DEFAULT_CONFIG_PATH,
    OpponentOverride,
    Settings,
    load_settings,
    save_settings,
)
from maxpreps_broadcast.errors import MaxPrepsError
from maxpreps_broadcast.models import Response, RosterSort
from maxpreps_broadcast.obs import METRICS, configure_logging
from maxpreps_broadcast.sync import SyncClient

app = typer.Typer(
    name="maxpreps",
    help="Resilient MaxPreps data for live school broadcasts.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

_state: dict[str, Any] = {"settings": None, "client": None}


def _settings() -> Settings:
    if _state["settings"] is None:
        _state["settings"] = load_settings()
    settings: Settings = _state["settings"]
    return settings


def _client() -> SyncClient:
    if _state["client"] is None:
        _state["client"] = SyncClient(_settings())
    client: SyncClient = _state["client"]
    return client


@app.callback()
def main(
    config: Path | None = typer.Option(None, "--config", help="config TOML path"),
    profile: str | None = typer.Option(None, "--profile", help="named [profiles.*] block"),
    offline: bool = typer.Option(False, "--offline", help="serve caches only; no network"),
    strict: bool = typer.Option(False, "--strict", help="schema drift raises instead of warning"),
    log_json: bool = typer.Option(False, "--log-json", help="structured JSON logs"),
) -> None:
    configure_logging(json_output=log_json)
    try:
        settings = load_settings(config)
    except MaxPrepsError as exc:
        # A bad sport name in config.toml lands here; the message already names
        # the valid keys, so show it plainly instead of a traceback.
        typer.secho(f"config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    settings.offline = offline or settings.offline
    settings.strict = strict or settings.strict
    _state["settings"] = settings
    _state["profile"] = profile
    _state["config_path"] = config


def _profile() -> str | None:
    return _state.get("profile")


def _save(settings: Settings) -> Path:
    """Persist to the config file the invocation actually named.

    ``save_settings`` defaults to the standard location, so writing without
    threading ``--config`` through would edit the wrong file — silently, and in
    the one case where the caller was explicitly pointing somewhere else.
    """
    return save_settings(settings, _state.get("config_path"))


def _echo_meta(resp: Response[Any]) -> None:
    stale = " STALE" if resp.stale else ""
    typer.secho(
        f"[{resp.source_tier} · {resp.cache_state} · {resp.data_age_seconds:.0f}s old{stale}"
        f" · {len(resp.warnings)} warnings]",
        fg=typer.colors.YELLOW if resp.stale else typer.colors.GREEN,
    )
    for warning in resp.warnings[:8]:
        typer.secho(f"  ! {warning.code}: {warning.message}", fg=typer.colors.YELLOW, err=True)


def _fail(exc: MaxPrepsError) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


# -------------------------------------------------------------------- init


@app.command()
def init() -> None:
    """Interactive setup: pick your school once, never type it again."""
    settings = _settings()
    typer.echo("maxpreps-broadcast setup — your primary school\n")
    query = typer.prompt("School name to search for", default="Northwood")
    state_code = typer.prompt("State code (blank to skip)", default="") or None
    try:
        results = _client().search_schools(query, state=state_code, limit=8)
    except MaxPrepsError as exc:
        _fail(exc)
        return
    if not results.data:
        typer.secho("No schools found — check spelling/state.", fg=typer.colors.RED)
        raise typer.Exit(1)
    for i, school in enumerate(results.data, 1):
        typer.echo(f"  {i}. {school}")
    choice = typer.prompt("Pick a number", type=int, default=1)
    school = results.data[max(0, min(len(results.data), choice) - 1)]
    sport = typer.prompt("Sport", default="football")
    season = typer.prompt("Season (YY-YY, blank = current)", default="") or None
    ref = school.team_ref(sport=sport, season=season)
    settings.primary.state = ref.state
    settings.primary.city = ref.city
    settings.primary.school_slug = ref.school
    settings.primary.sport = sport
    settings.primary.season_year = season
    settings.primary.team_id = school.school_id
    settings.primary.display_name = school.name
    if typer.confirm("Add a short abbreviation for scorebugs?", default=True):
        settings.primary.abbreviation = typer.prompt("Abbreviation", default=school.name[:2].upper())
    out = typer.prompt("Export directory", default=settings.export.out_dir)
    settings.export.out_dir = out
    path = _save(settings)
    typer.secho(f"\nSaved {path}", fg=typer.colors.GREEN)
    typer.echo("Try: maxpreps schedule")


# ---------------------------------------------------------------- the eight


@app.command()
def search(query: str, state: str | None = typer.Option(None), limit: int = 10) -> None:
    """Search schools by (partial) name."""
    try:
        resp = _client().search_schools(query, state=state, limit=limit)
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _echo_meta(resp)
    for school in resp.data:
        colors = f"  {'/'.join(school.colors)}" if school.colors else ""
        typer.echo(f"  {school}{colors}")
        if school.canonical_url:
            typer.echo(f"      {school.canonical_url}")


@app.command()
def schedule(
    team: str | None = typer.Option(None, help="team URL (default: configured primary)"),
    sport: str | None = typer.Option(None, "--sport", help="one sport (default: all active)"),
    season: str | None = typer.Option(None, help="season year, YY-YY"),
    byes: bool = typer.Option(False, "--byes", help="synthesize bye-week rows (weekly sports only)"),
) -> None:
    """Season schedule with venue, results, and week numbers.

    Covers every active sport unless ``--sport`` or ``--team`` narrows it.
    """
    client = _client()
    if team is not None:
        try:
            resp = client.get_team_schedule(
                team, sport=sport, season=season, include_byes=byes, profile=_profile()
            )
        except MaxPrepsError as exc:
            _fail(exc)
            return
        _echo_meta(resp)
        _print_schedule(resp.data)
        return
    try:
        results = client.get_schedules(
            [sport] if sport else None, season=season, include_byes=byes, profile=_profile()
        )
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _print_multi(results, _print_schedule)


def _print_schedule(sched: Any) -> None:
    typer.echo(f"{sched.team.school_name} {sched.team.mascot or ''} — {sched.record_display()}")
    if not sched.contests:
        typer.secho("    no contests published yet", fg=typer.colors.YELLOW)
        return
    for contest in sched.contests:
        week = f"W{contest.week_index}" if contest.week_index is not None else "  "
        if contest.is_synthesized:
            typer.echo(f"  {week:>3}  {'—':<16} BYE")
            continue
        when = contest.starts_at_local.strftime("%a %m/%d %I:%M%p") if contest.starts_at_local else "TBA"
        vs_at = "@" if contest.is_home is False else "vs"
        league = "*" if contest.is_league_game else " "
        result = f"  {contest.result_text}" if contest.result_text else ""
        live = "  LIVE" if contest.is_live else ""
        opponent = contest.opponent_display or contest.opponent_name or "TBD"
        typer.echo(f"  {week:>3}  {when:<16} {vs_at} {opponent}{league}{result}{live}")


def _print_multi(
    results: dict[str, Any], render: Any, *, meta: bool = True
) -> None:
    """Render a per-sport result map, keeping failures visible but non-fatal.

    Exits non-zero only if *every* sport failed — one dark sport should not fail
    a broadcast-morning script that got the other five.
    """
    failures = 0
    for key, result in results.items():
        entry = sports.BY_KEY.get(key)
        typer.secho(f"\n=== {entry.display if entry else key} ===", fg=typer.colors.CYAN, bold=True)
        if isinstance(result, MaxPrepsError):
            failures += 1
            typer.secho(f"  error: {result}", fg=typer.colors.RED, err=True)
            continue
        if meta:
            _echo_meta(result)
        render(result.data)
    if results and failures == len(results):
        raise typer.Exit(code=1)


@app.command()
def roster(
    team: str | None = typer.Option(None),
    sort: str = typer.Option("jersey", help="jersey|last_name|position|grade"),
) -> None:
    """Roster with lower-third-ready names."""
    try:
        resp = _client().get_team_roster(team, sort=RosterSort(sort), profile=_profile())
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _echo_meta(resp)
    for entry in resp.data.entries:
        captain = " ©" if entry.is_captain else ""
        typer.echo(
            f"  #{entry.jersey_padded}  {entry.lower_third_name:<20} "
            f"{entry.positions_display:<10} {entry.grade_level or '':<4} "
            f"{entry.height_display or '':<6} {entry.weight_lbs or ''}{captain}"
        )


@app.command()
def live(
    team: str | None = typer.Option(None),
    sport: str | None = typer.Option(None, "--sport", help="one sport (default: all active)"),
) -> None:
    """Current scorebug state (live, else last final, else next game).

    Covers every active sport unless ``--sport`` or ``--team`` narrows it.
    """
    client = _client()
    if team is not None:
        try:
            resp = client.get_scoretracker(team, sport=sport, profile=_profile())
        except MaxPrepsError as exc:
            _fail(exc)
            return
        _echo_meta(resp)
        _print_state(resp.data)
        return
    try:
        results = client.get_scoretrackers([sport] if sport else None, profile=_profile())
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _print_multi(results, _print_state)


def _print_state(state: Any) -> None:
    from maxpreps_broadcast.export.strings import clock_display, score_line

    line = score_line(
        state.home.abbr or (state.home.name or "?")[:4].upper(),
        state.home_score,
        state.away.abbr or (state.away.name or "?")[:4].upper(),
        state.away_score,
    )
    typer.echo(f"\n  {line}")
    typer.echo(f"  {state.status.value.upper()} · {clock_display(state)}\n")


@app.command()
def game(contest_id: str, team: str | None = typer.Option(None)) -> None:
    """Look up one contest by its GUID."""
    try:
        resp = _client().get_scoretracker_by_id(contest_id, team=team, profile=_profile())
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _echo_meta(resp)
    _print_state(resp.data)


@app.command()
def games(
    scope: str = typer.Option("league", help="team|league|section|division|state"),
    window: float = typer.Option(48.0, help="hours ahead to include"),
) -> None:
    """Everything live and upcoming across a scope."""
    try:
        resp = _client().get_live_and_upcoming_games(scope=scope, window_hours=window, profile=_profile())
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _echo_meta(resp)
    for state in resp.data:
        marker = "● LIVE " if state.status.value == "in_progress" else "        "
        when = state.starts_at_local.strftime("%a %I:%M%p") if state.starts_at_local else "TBA"
        typer.echo(
            f"  {marker}{when:<12} {state.away.name or '?'} at {state.home.name or '?'}"
            + (
                f"  {state.away_score}-{state.home_score}"
                if state.home_score is not None
                else ""
            )
        )


@app.command()
def athlete(who: str, team: str | None = typer.Option(None)) -> None:
    """Athlete profile by name (roster lookup), career GUID, or URL."""
    try:
        resp = _client().get_athlete_profile(who, team=team, profile=_profile())
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _echo_meta(resp)
    p = resp.data
    typer.echo(f"\n  {p.full_name}  #{p.jersey_number or '—'}  {'/'.join(p.positions)}")
    typer.echo(f"  {p.grade_level or ''} {p.height_display or ''} {p.weight_lbs or ''}")
    for line in p.stat_lines:
        typer.echo(f"\n  [{line.group}]")
        for key in line.display_order or list(line.stats):
            typer.echo(f"    {key}: {line.stats.get(key)}")


@app.command()
def rankings(
    scope: str = typer.Option("state", help="state|national|league|division|section"),
    limit: int = 25,
) -> None:
    """Rankings for a scope, with your team highlighted if present."""
    try:
        resp = _client().get_rankings(scope=scope, limit=limit, profile=_profile())
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _echo_meta(resp)
    mine = _settings().primary.display_name or _settings().primary.school_slug or ""
    for entry in resp.data.entries:
        marker = "→" if mine and mine.lower() in (entry.school_name or "").lower() else " "
        delta = f" ({entry.rank_delta:+d})" if entry.rank_delta else ""
        typer.echo(f"  {marker} {entry.rank:>3}. {entry.school_name:<28} {entry.record or '':<7}{delta}")
    found = resp.data.find(mine) if mine else None
    if found and found not in resp.data.entries:
        typer.echo(f"  …\n  → {found.rank:>3}. {found.school_name}")


# ------------------------------------------------------------------- sports

sports_app = typer.Typer(help="Choose which sports this station is covering.", no_args_is_help=True)
app.add_typer(sports_app, name="sports")


def _echo_active(active: list[Any]) -> None:
    typer.secho(f"active sports ({len(active)}/{sports.MAX_ACTIVE_SPORTS}):", fg=typer.colors.GREEN)
    for i, entry in enumerate(active, 1):
        typer.echo(f"  {i}. {entry.key:<22} {entry.display:<24} {entry.season.value}")


@sports_app.command("list")
def sports_list(
    season: str | None = typer.Option(None, help="fall|winter|spring — only this season"),
    all_sports: bool = typer.Option(False, "--all", help="the whole catalogue, not just active"),
) -> None:
    """Show the catalogue, or what is currently active."""
    if all_sports or season:
        wanted = sports.for_season(season) if season else list(sports.all_sports())
        active = {s.key for s in _settings().active_sports(_profile())}
        for entry in wanted:
            mark = "*" if entry.key in active else " "
            preset = "" if entry.in_preset else "   (not in season preset)"
            typer.echo(
                f" {mark} {entry.key:<22} {entry.display:<24} {entry.season.value:<7}"
                f" /{'/'.join(entry.path_segments())}{preset}"
            )
        return
    _echo_active(_settings().active_sports(_profile()))


@sports_app.command("set")
def sports_set(names: list[str] = typer.Argument(help="catalogue keys or display names")) -> None:
    """Replace the active selection (this is what a UI will drive)."""
    settings = _settings()
    try:
        active = settings.set_active_sports(names)
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _save(settings)
    _echo_active(active)


@sports_app.command("add")
def sports_add(names: list[str] = typer.Argument(help="catalogue keys or display names")) -> None:
    """Append sports to the active selection."""
    settings = _settings()
    current = [s.key for s in settings.active_sports(_profile())]
    try:
        active = settings.set_active_sports(current + list(names))
    except MaxPrepsError as exc:
        _fail(exc)
        return
    _save(settings)
    _echo_active(active)


@sports_app.command("remove")
def sports_remove(names: list[str] = typer.Argument(help="catalogue keys or display names")) -> None:
    """Drop sports from the active selection."""
    settings = _settings()
    try:
        drop = {sports.get(n).key for n in names}
    except MaxPrepsError as exc:
        _fail(exc)
        return
    keep = [s.key for s in settings.active_sports(_profile()) if s.key not in drop]
    active = settings.set_active_sports(keep)
    _save(settings)
    _echo_active(active)


@sports_app.command("season")
def sports_season(
    which: str = typer.Argument(help="fall|winter|spring"),
) -> None:
    """Activate a season's default sports, capped at the maximum."""
    settings = _settings()
    try:
        preset = sports.preset_for(which)
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    active = settings.set_active_sports([s.key for s in preset])
    _save(settings)
    _echo_active(active)
    dropped = [s for s in sports.for_season(which) if s.in_preset and s not in preset]
    for entry in dropped:
        typer.secho(
            f"  not activated (over the {sports.MAX_ACTIVE_SPORTS}-sport limit): {entry.key}",
            fg=typer.colors.YELLOW,
        )


# ------------------------------------------------------------------- export


@app.command()
def export(
    out: Path | None = typer.Option(None, help="output directory (default from config)"),
    formats: str = typer.Option("", help="comma list: json,mgjson,csv,tsv,xml (default from config)"),
    byes: bool = typer.Option(True, help="include synthesized bye rows in schedule"),
    sport: str | None = typer.Option(None, "--sport", help="one sport (default: all active)"),
    index: bool = typer.Option(True, help="also write sports.json listing what was exported"),
) -> None:
    """One-shot export of live + schedule + roster for every active sport.

    Files are suffixed per sport (``live.football.json``) into one flat
    directory, so a Drive-synced production machine sees every sport side by
    side.  A sport that fails is reported and skipped; the rest still export.
    """
    settings = _settings()
    out_dir = out or Path(settings.export.out_dir)
    wanted = [f.strip() for f in formats.split(",") if f.strip()] or settings.export.formats
    client = _client()
    try:
        active = (
            [sports.get(sport)] if sport else settings.active_sports(_profile())
        )
    except MaxPrepsError as exc:
        _fail(exc)
        return
    if not active:
        typer.secho("no sports configured — run `maxpreps sports season fall`", fg=typer.colors.RED)
        raise typer.Exit(1)

    written: list[Path] = []
    exported: list[dict[str, Any]] = []
    failed = 0
    for entry in active:
        typer.secho(f"\n=== {entry.display} ===", fg=typer.colors.CYAN, bold=True)
        try:
            written += _export_one(entry, out_dir, wanted, byes, settings, client)
        except MaxPrepsError as exc:
            failed += 1
            typer.secho(f"  error: {exc}", fg=typer.colors.RED, err=True)
            continue
        exported.append(entry.summary())
    if index:
        written.append(_write_sports_index(out_dir, exported, wanted))
    for path in written:
        typer.secho(f"  wrote {path}", fg=typer.colors.GREEN)
    if failed == len(active):
        raise typer.Exit(code=1)


def _export_one(
    entry: Any,
    out_dir: Path,
    wanted: list[str],
    byes: bool,
    settings: Settings,
    client: Any,
) -> list[Path]:
    """Every requested format for one sport.  Raises on fetch failure."""
    from maxpreps_broadcast.export import csv_out, json_out, xml_out
    from maxpreps_broadcast.export.atomic import atomic_write_json
    from maxpreps_broadcast.export.mgjson import mgjson_for_live, write_mgjson

    key = entry.key
    live_resp = client.get_scoretracker(sport=entry, profile=_profile())
    sched_resp = client.get_team_schedule(sport=entry, include_byes=byes, profile=_profile())
    roster_resp = client.get_team_roster(sport=entry, profile=_profile())

    assets = None if settings.offline else out_dir / "assets"
    live_view = json_out.live_view(live_resp, settings, assets_dir=assets, sport=key)
    sched_view = json_out.schedule_view(sched_resp, settings)
    roster_view = json_out.roster_view(roster_resp, settings, sport=key)
    stamp = datetime.now(UTC).isoformat()
    for view in (live_view, sched_view, roster_view):
        view["exported_at"] = stamp

    def path_for(stem: str, suffix: str) -> Path:
        return out_dir / json_out.sport_filename(stem, suffix, key)

    written: list[Path] = []
    if "json" in wanted:
        written += [
            atomic_write_json(path_for("live", "json"), live_view),
            atomic_write_json(path_for("schedule", "json"), sched_view),
            atomic_write_json(path_for("roster", "json"), roster_view),
        ]
    if "mgjson" in wanted:
        written.append(write_mgjson(path_for("live", "mgjson"), mgjson_for_live(live_view)))
    if "csv" in wanted:
        written += [
            csv_out.write_csv(path_for("schedule", "csv"), sched_view["games"]),
            csv_out.write_csv(path_for("roster", "csv"), roster_view["players"]),
        ]
    if "tsv" in wanted:
        written += [
            csv_out.write_tsv(path_for("schedule", "tsv"), sched_view["games"]),
            csv_out.write_tsv(path_for("roster", "tsv"), roster_view["players"]),
        ]
    if "xml" in wanted:
        written += [
            xml_out.write_xml(path_for("live", "xml"), live_view),
            xml_out.write_xml(path_for("schedule", "xml"), sched_view),
            xml_out.write_xml(path_for("roster", "xml"), roster_view),
        ]
    return written


def _write_sports_index(out_dir: Path, exported: list[dict[str, Any]], formats: list[str]) -> Path:
    """One small file naming every sport that exported, and its filenames.

    The graphics loop reads this to know what to rotate through, instead of
    globbing a directory and guessing which sports are live this season.
    """
    from maxpreps_broadcast.export.atomic import atomic_write_json
    from maxpreps_broadcast.export.json_out import sport_filename

    return atomic_write_json(
        out_dir / "sports.json",
        {
            "exported_at": datetime.now(UTC).isoformat(),
            "count": len(exported),
            "sports": [
                {
                    **summary,
                    "files": {
                        stem: sport_filename(stem, "json", summary["key"])
                        for stem in ("live", "schedule", "roster")
                    },
                }
                for summary in exported
            ],
            "formats": formats,
        },
    )


@app.command()
def render(
    template: str = typer.Option(..., "--template", help="name of templates/{name}.mapping.yaml"),
    out: Path | None = typer.Option(None),
) -> None:
    """Resolve a template mapping into {template}.render.json (AE layer values)."""
    from maxpreps_broadcast.export import json_out
    from maxpreps_broadcast.export.mapping import TemplateMapping, find_template, write_render

    settings = _settings()
    out_dir = out or Path(settings.export.out_dir)
    search_dirs = [
        Path.cwd() / "templates",
        Path(__file__).resolve().parent.parent.parent.parent / "templates",
        DEFAULT_CONFIG_PATH.expanduser().parent / "templates",
    ]
    try:
        mapping = TemplateMapping.load(find_template(template, search_dirs=search_dirs))
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    client = _client()
    try:
        if mapping.source == "schedule":
            view = json_out.schedule_view(
                client.get_team_schedule(include_byes=True, profile=_profile()), settings
            )
        elif mapping.source == "roster":
            view = json_out.roster_view(client.get_team_roster(profile=_profile()), settings)
        else:
            view = json_out.live_view(
                client.get_scoretracker(profile=_profile()), settings,
                assets_dir=None if settings.offline else out_dir / "assets",
            )
    except MaxPrepsError as exc:
        _fail(exc)
        return
    path = write_render(out_dir, mapping, view)
    typer.secho(f"  wrote {path}", fg=typer.colors.GREEN)


# ------------------------------------------------------------------ service


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8787),
    watch: bool = typer.Option(False, "--watch", help="poll + rewrite exports continuously"),
    interval: float = typer.Option(5.0, "--interval", help="watch poll seconds"),
    out: Path | None = typer.Option(None, "--out", help="watch output directory"),
) -> None:
    """Run the local API (and optional watch daemon) for After Effects."""
    import uvicorn

    from maxpreps_broadcast.service.app import create_app

    settings = _settings()
    application = create_app(
        settings,
        watch=watch,
        watch_out=out or Path(settings.export.out_dir),
        watch_interval=interval,
    )
    typer.secho(f"serving on http://{host}:{port}  (watch={'on' if watch else 'off'})",
                fg=typer.colors.GREEN)
    uvicorn.run(application, host=host, port=port, log_level="info")


# --------------------------------------------------------------- diagnostics


@app.command()
def doctor() -> None:
    """Live shape checks against the fixtures' expectations.  Run this the
    morning of a broadcast."""
    checks = _client().doctor()
    failed = 0
    for check in checks:
        if check["ok"]:
            tier = check.get("tier", "?")
            warnings = ",".join(check.get("warnings", [])) or "none"
            typer.secho(
                f"  ✓ {check['check']:<14} tier={tier:<9} cache={check.get('cache', '?'):<6}"
                f" warnings={warnings} ({check['seconds']}s)",
                fg=typer.colors.GREEN,
            )
        else:
            failed += 1
            typer.secho(f"  ✗ {check['check']:<14} {check['error']}", fg=typer.colors.RED)
    if failed:
        typer.secho(f"\n{failed} check(s) failed — see RUNBOOK.md before going on air.",
                    fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("\nAll checks passed.", fg=typer.colors.GREEN)


@app.command()
def stats() -> None:
    """Counters and latency percentiles from this process."""
    snapshot = METRICS.snapshot()
    typer.echo(json.dumps(snapshot, indent=2))


@app.command()
def override(
    name: str = typer.Argument(help="MaxPreps school name, exactly as it appears"),
    display: str | None = typer.Option(None, help="scorebug display name"),
    abbr: str | None = typer.Option(None, help="scorebug abbreviation"),
    logo: str | None = typer.Option(None, help="path/URL to a preferred logo"),
) -> None:
    """Set a per-opponent display override (names are always too long)."""
    settings = _settings()
    settings.opponents[name] = OpponentOverride(display=display, abbr=abbr, logo=logo)
    path = _save(settings)
    typer.secho(f"  saved override for {name!r} in {path}", fg=typer.colors.GREEN)


@app.command()
def version() -> None:
    typer.echo(f"maxpreps-broadcast {__version__} (python {sys.version.split()[0]})")


if __name__ == "__main__":
    app()
