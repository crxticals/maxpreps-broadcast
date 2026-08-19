"""The async client core.

Every public function follows the same shape:

    resolve team → tiered fetch through the SWR cache → parse → derive →
    ``Response[T]`` envelope (source_tier, cache_state, data_age, warnings)

Tier chain per request:

    1. ``json_api``  — ``/_next/data/{buildId}/{path}.json`` (self-heals a
       stale buildId once on 404)
    2. ``hydration`` — the page's ``__NEXT_DATA__`` blob (same payload)
    3. ``html``      — rendered tables (schedule/roster only)

The cache stores ``{"tier": ..., "payload": ...}`` wrappers of the *raw*
upstream payload, so a parser fix retroactively improves last-known-good
snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TypeVar
from urllib.parse import quote, urlencode, urlsplit

from maxpreps_broadcast import sports
from maxpreps_broadcast.cache.disk import DiskCache
from maxpreps_broadcast.cache.memory import MemoryCache
from maxpreps_broadcast.cache.snapshots import SnapshotStore
from maxpreps_broadcast.cache.swr import CachedFetcher, CacheHit, NotModified
from maxpreps_broadcast.config import Settings
from maxpreps_broadcast.errors import (
    AmbiguousTeamError,
    ContestNotFoundError,
    MaxPrepsError,
    SchemaDriftError,
    TerminalError,
)
from maxpreps_broadcast.http.transport import Transport
from maxpreps_broadcast.models import (
    AthleteProfile,
    CacheState,
    Contest,
    GameStatus,
    ParseMode,
    ParseWarning,
    Rankings,
    Response,
    Roster,
    RosterSort,
    Schedule,
    School,
    ScoreState,
    SourceTier,
    TeamRef,
)
from maxpreps_broadcast.obs import METRICS, get_logger, new_request_id
from maxpreps_broadcast.parsers import html as html_parser
from maxpreps_broadcast.parsers import hydration, json_api
from maxpreps_broadcast.parsers.normalize import strip_accents
from maxpreps_broadcast.sports import Sport

log = get_logger(__name__)

T = TypeVar("T")

BASE = "https://www.maxpreps.com"
_BUILD_ID_TTL = 6 * 3600.0


def _qs_key(query: dict[str, str]) -> str:
    """Stable cache-key suffix — two years of the same schedule are two entries."""
    return "?" + urlencode(sorted(query.items())) if query else ""


class MaxPrepsClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.settings = settings or Settings()
        http = self.settings.http
        self.transport = transport or Transport(
            user_agent=http.user_agent,
            requests_per_second=http.requests_per_second,
            max_concurrency=http.max_concurrency,
            timeout_seconds=http.timeout_seconds,
            max_retries=http.max_retries,
            breaker_failure_threshold=http.breaker_failure_threshold,
            breaker_cooldown_seconds=http.breaker_cooldown_seconds,
            respect_robots=http.respect_robots,
        )
        cache_dir = self.settings.cache.cache_dir()
        self.disk = DiskCache(cache_dir / "cache.sqlite3")
        self.memory = MemoryCache(self.settings.cache.memory_max_entries)
        self.snapshots = SnapshotStore(self.disk)
        self.fetcher = CachedFetcher(
            self.memory, self.disk, self.snapshots, offline=self.settings.offline
        )
        self._build_id: str | None = None
        self._build_id_at = 0.0
        self._parse_mode = ParseMode.STRICT if self.settings.strict else ParseMode.LENIENT

    # ----------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self.fetcher.wait_for_revalidations()
        await self.transport.aclose()
        self.disk.close()

    async def __aenter__(self) -> MaxPrepsClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------- buildId

    async def _get_build_id(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._build_id and now - self._build_id_at < _BUILD_ID_TTL:
            return self._build_id
        result = await self.transport.get(BASE + "/", label="build_id")
        build_id = hydration.extract_build_id(result.text())
        if not build_id:
            raise SchemaDriftError("could not find buildId on maxpreps.com homepage", path="html.buildId")
        self._build_id = build_id
        self._build_id_at = now
        log.info("resolved buildId", build_id=build_id)
        return build_id

    # -------------------------------------------------------- tiered fetch

    async def _fetch_tiered(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        allow_html_tier: bool = False,
        etag: str | None = None,
        last_modified: str | None = None,
        label: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """→ (wrapper{tier,payload}, meta).  Raises NotModified on 304."""
        path = path.strip("/")
        qs = f"?{urlencode(query)}" if query else ""

        # Tier 1: the JSON data route.
        tier1_error: Exception | None = None
        try:
            build_id = await self._get_build_id()
            url = f"{BASE}/_next/data/{quote(build_id)}/{path}.json{qs}"
            try:
                result = await self.transport.get(url, etag=etag, last_modified=last_modified, label=label)
            except TerminalError:
                # Likely a stale buildId → self-heal once and retry.
                build_id = await self._get_build_id(force=True)
                url = f"{BASE}/_next/data/{quote(build_id)}/{path}.json{qs}"
                result = await self.transport.get(url, etag=etag, last_modified=last_modified, label=label)
            if result.not_modified:
                raise NotModified()
            import json as _json

            payload = _json.loads(result.text())
            if not isinstance(payload, dict):
                raise SchemaDriftError("data route returned non-object JSON", path=path)
            METRICS.inc("tier_used_total", tier="json_api")
            meta = {
                "source_tier": "json_api",
                "etag": result.etag,
                "last_modified": result.last_modified,
                "url": url,
            }
            return {"tier": "json_api", "payload": payload}, meta
        except NotModified:
            raise
        except MaxPrepsError as exc:
            tier1_error = exc
            log.warning("tier1 json_api failed; falling back", path=path, error=str(exc))

        # Tier 2/3: fetch the page HTML once; hydration first, tables second.
        page_url = f"{BASE}/{path}/{qs}"
        result = await self.transport.get(page_url, label=label + "_html")
        html_text = result.text()
        try:
            payload = hydration.page_props_from_html(html_text)
            METRICS.inc("tier_used_total", tier="hydration")
            return (
                {"tier": "hydration", "payload": payload},
                {"source_tier": "hydration", "url": page_url},
            )
        except SchemaDriftError as exc:
            if not allow_html_tier:
                raise (tier1_error or exc) from exc
            log.warning("tier2 hydration failed; using html tables", path=path, error=str(exc))
        METRICS.inc("tier_used_total", tier="html")
        return (
            {"tier": "html", "payload": html_text},
            {"source_tier": "html", "url": page_url},
        )

    async def _cached_page(
        self,
        cache_key: str,
        path: str,
        *,
        ttl: float,
        query: dict[str, str] | None = None,
        allow_html_tier: bool = False,
        label: str,
    ) -> CacheHit:
        async def fetch(etag: str | None, last_modified: str | None) -> tuple[Any, dict[str, Any]]:
            return await self._fetch_tiered(
                path,
                query=query,
                allow_html_tier=allow_html_tier,
                etag=etag,
                last_modified=last_modified,
                label=label,
            )

        return await self.fetcher.get(cache_key, ttl=ttl, fetch=fetch)

    # ------------------------------------------------------------ envelope

    def _envelope(
        self,
        data: T,
        hit: CacheHit,
        warnings: list[ParseWarning],
    ) -> Response[T]:
        tier: SourceTier = hit.meta.get("source_tier", "json_api")
        state: CacheState = hit.state
        return Response.wrap(
            data,
            fetched_at=datetime.fromtimestamp(hit.stored_at, tz=UTC),
            source_tier=tier,
            cache_state=state,
            warnings=warnings,
            request_id=new_request_id(),
        )

    # ================================================== 1. search_schools

    async def search_schools(
        self,
        query: str,
        *,
        state: str | None = None,
        city: str | None = None,
        limit: int = 10,
    ) -> Response[list[School]]:
        needle = strip_accents(query).casefold().strip()
        hit = await self._cached_page(
            f"search:{needle}",
            "search",
            ttl=self.settings.cache.ttl_search,
            query={"q": query.strip()},
            label="search",
        )
        wrapper = hit.value
        schools, ctx = json_api.parse_search(wrapper["payload"], self._parse_mode)
        if state:
            schools = [s for s in schools if (s.state or "").casefold() == state.casefold()]
        if city:
            schools = [s for s in schools if (s.city or "").casefold() == city.casefold()]

        def score(school: School) -> tuple[int, int]:
            name = strip_accents(school.name).casefold()
            exact = 0 if name == needle else 1
            prefix = 0 if name.startswith(needle) else 1
            return (exact, prefix)

        schools.sort(key=score)
        return self._envelope(schools[:limit], hit, ctx.warnings)

    async def resolve_school(
        self,
        query: str,
        *,
        sport: str | None = None,
        state: str | None = None,
        city: str | None = None,
    ) -> TeamRef:
        """Exactly one TeamRef, or ``AmbiguousTeamError`` listing candidates."""
        resp = await self.search_schools(query, state=state, city=city, limit=8)
        schools = resp.data
        if not schools:
            raise AmbiguousTeamError(query, [])
        needle = strip_accents(query).casefold().strip()
        exact = [s for s in schools if strip_accents(s.name).casefold() == needle]
        pool = exact if len(exact) == 1 else schools
        if len(pool) > 1:
            raise AmbiguousTeamError(query, [s.label() for s in pool])
        try:
            return pool[0].team_ref(sport=sport or self.settings.primary.sport)
        except ValueError as exc:
            raise AmbiguousTeamError(query, [pool[0].label()]) from exc

    # =============================================== 2. get_team_schedule

    async def get_team_schedule(
        self,
        team: TeamRef | str | None = None,
        *,
        sport: Sport | str | None = None,
        season: str | None = None,
        profile: str | None = None,
        include_results: bool = True,
        include_byes: bool = False,
        ttl: float | None = None,
    ) -> Response[Schedule]:
        ref = self._ref_for(team, sport=sport, profile=profile)
        path = ref.path(tab="schedule")
        query = ref.query({"year": season} if season else None)
        hit = await self._cached_page(
            f"sched:{path}{_qs_key(query)}",
            path,
            query=query or None,
            ttl=ttl if ttl is not None else self.settings.cache.ttl_schedule,
            allow_html_tier=True,
            label="schedule",
        )
        schedule, ctx = self._parse_schedule_wrapper(hit.value, ref)
        if include_byes:
            json_api.synthesize_byes(schedule, ctx)
        if not include_results:
            for contest in schedule.contests:
                contest.result = None
                contest.result_text = None
                contest.subject.score = None
                contest.opponent.score = None
        self._apply_opponent_overrides(schedule.contests)
        return self._envelope(schedule, hit, ctx.warnings)

    def _ref_for(
        self,
        team: TeamRef | str | None,
        *,
        sport: Sport | str | None = None,
        profile: str | None = None,
    ) -> TeamRef:
        """Resolve the team, then re-point it at ``sport`` when one is named."""
        ref = self.settings.resolve_team(team, profile=profile)
        if sport is None:
            return ref
        entry = sports.get(sport) if isinstance(sport, str) else sport
        return ref.for_sport(entry, season=ref.season)

    def _parse_schedule_wrapper(
        self, wrapper: dict[str, Any], ref: TeamRef | None
    ) -> tuple[Schedule, json_api.Ctx]:
        hint = ref.sport_key if ref else None
        if wrapper["tier"] == "html":
            return html_parser.parse_schedule_html(
                wrapper["payload"],
                season=ref.season if ref else None,
                sport_hint=hint,
                mode=self._parse_mode,
            )
        return json_api.parse_schedule(wrapper["payload"], self._parse_mode, sport_hint=hint)

    def _apply_opponent_overrides(self, contests: list[Contest]) -> None:
        for contest in contests:
            if contest.opponent_display is None and contest.opponent_name is not None:
                contest.opponent_display = self.settings.opponent_display(contest.opponent_name)

    # =========================================== 2b. multi-sport fan-out

    async def get_schedules(
        self,
        sports_wanted: list[Sport | str] | None = None,
        *,
        profile: str | None = None,
        season: str | None = None,
        include_byes: bool = True,
    ) -> dict[str, Response[Schedule] | MaxPrepsError]:
        """Every active sport's schedule, fetched concurrently.

        One sport failing must not cost a producer the other five, so failures
        are returned in place rather than raised: the caller gets a complete map
        keyed by catalogue key and decides what a partial result means.  Ordering
        follows the active-sports list, which is the graphics rotation order.
        """
        entries = self._sport_list(sports_wanted, profile=profile)

        async def one(entry: Sport) -> Response[Schedule] | MaxPrepsError:
            try:
                return await self.get_team_schedule(
                    sport=entry, profile=profile, season=season, include_byes=include_byes
                )
            except MaxPrepsError as exc:
                log.warning("schedule failed for sport", sport=entry.key, error=str(exc))
                return exc

        results = await asyncio.gather(*(one(e) for e in entries))
        return dict(zip((e.key for e in entries), results, strict=True))

    async def get_scoretrackers(
        self,
        sports_wanted: list[Sport | str] | None = None,
        *,
        profile: str | None = None,
    ) -> dict[str, Response[ScoreState] | MaxPrepsError]:
        """Current scorebug state per active sport.  Failure policy as above —
        an out-of-season sport with no contests yet is an expected miss, not an
        outage."""
        entries = self._sport_list(sports_wanted, profile=profile)

        async def one(entry: Sport) -> Response[ScoreState] | MaxPrepsError:
            try:
                return await self.get_scoretracker(sport=entry, profile=profile)
            except MaxPrepsError as exc:
                log.warning("live state failed for sport", sport=entry.key, error=str(exc))
                return exc

        results = await asyncio.gather(*(one(e) for e in entries))
        return dict(zip((e.key for e in entries), results, strict=True))

    def _sport_list(
        self, wanted: list[Sport | str] | None, *, profile: str | None = None
    ) -> list[Sport]:
        if wanted:
            return sports.resolve_many([w if isinstance(w, str) else w.key for w in wanted])
        return self.settings.active_sports(profile)

    # ================================================= 3. get_team_roster

    async def get_team_roster(
        self,
        team: TeamRef | str | None = None,
        *,
        sport: Sport | str | None = None,
        season: str | None = None,
        profile: str | None = None,
        sort: RosterSort | str = RosterSort.JERSEY,
    ) -> Response[Roster]:
        ref = self._ref_for(team, sport=sport, profile=profile)
        path = ref.path(tab="roster")
        query = ref.query({"year": season} if season else None)
        hit = await self._cached_page(
            f"roster:{path}{_qs_key(query)}",
            path,
            query=query or None,
            ttl=self.settings.cache.ttl_roster,
            allow_html_tier=True,
            label="roster",
        )
        wrapper = hit.value
        if wrapper["tier"] == "html":
            roster, ctx = html_parser.parse_roster_html(wrapper["payload"], mode=self._parse_mode)
        else:
            roster, ctx = json_api.parse_roster(wrapper["payload"], self._parse_mode)
        roster = roster.sorted(RosterSort(sort))
        return self._envelope(roster, hit, ctx.warnings)

    # ================================================ 4/5. scoretracker(s)

    async def get_scoretracker(
        self,
        team: TeamRef | str | None = None,
        *,
        sport: Sport | str | None = None,
        profile: str | None = None,
    ) -> Response[ScoreState]:
        """Live state if a game is in progress; otherwise the most recent
        final; otherwise the next upcoming game as ``scheduled``."""
        ref = self._ref_for(team, sport=sport, profile=profile)
        resp = await self.get_team_schedule(
            ref, profile=profile, ttl=self.settings.cache.ttl_live_score
        )
        schedule = resp.data
        now = datetime.now(UTC)
        contest = schedule.live_contest() or schedule.last_completed() or schedule.next_contest(now=now)
        if contest is None:
            raise ContestNotFoundError("schedule has no contests at all")
        ctx = json_api.Ctx(self._parse_mode)
        state = json_api.build_score_state(contest, schedule.team, now=now, ctx=ctx)
        return Response.wrap(
            state,
            fetched_at=resp.fetched_at,
            source_tier=resp.source_tier,
            cache_state=resp.cache_state,
            warnings=[*resp.warnings, *ctx.warnings],
            request_id=new_request_id(),
        )

    async def get_scoretracker_by_id(
        self,
        contest_id: str,
        *,
        team: TeamRef | str | None = None,
        profile: str | None = None,
    ) -> Response[ScoreState]:
        """Find a specific contest.  Search order: the hinted (or primary)
        team's schedule, then every league member's schedule.  There is no
        contest-id lookup endpoint on the server-rendered surface."""
        wanted = contest_id.casefold()
        candidates: list[TeamRef] = []
        with contextlib.suppress(MaxPrepsError):
            candidates.append(self.settings.resolve_team(team, profile=profile))
        searched: list[str] = []
        for ref in candidates:
            resp = await self.get_team_schedule(ref, ttl=self.settings.cache.ttl_live_score)
            searched.append(ref.display())
            for contest in resp.data.contests:
                if (contest.contest_id or "").casefold() == wanted:
                    return self._score_from(contest, resp)
        if candidates:
            for member_ref in await self._league_member_refs(candidates[0]):
                if any(member_ref.path() == c.path() for c in candidates):
                    continue
                try:
                    resp = await self.get_team_schedule(
                        member_ref, ttl=self.settings.cache.ttl_live_score
                    )
                except MaxPrepsError:
                    continue
                searched.append(member_ref.display())
                for contest in resp.data.contests:
                    if (contest.contest_id or "").casefold() == wanted:
                        return self._score_from(contest, resp)
        raise ContestNotFoundError(
            f"contest {contest_id} not found in schedules of: {', '.join(searched) or 'no teams'} — "
            "pass team= to hint which school's schedule carries it"
        )

    def _score_from(self, contest: Contest, resp: Response[Schedule]) -> Response[ScoreState]:
        ctx = json_api.Ctx(self._parse_mode)
        state = json_api.build_score_state(contest, resp.data.team, now=datetime.now(UTC), ctx=ctx)
        return Response.wrap(
            state,
            fetched_at=resp.fetched_at,
            source_tier=resp.source_tier,
            cache_state=resp.cache_state,
            warnings=[*resp.warnings, *ctx.warnings],
            request_id=new_request_id(),
        )

    # ==================================== 6. get_live_and_upcoming_games

    async def get_live_and_upcoming_games(
        self,
        *,
        scope: str = "league",
        teams: list[TeamRef | str] | None = None,
        window_hours: float = 48.0,
        finals_within_hours: float = 12.0,
        profile: str | None = None,
        max_teams: int = 24,
    ) -> Response[list[ScoreState]]:
        refs = await self._resolve_scope(scope, teams=teams, profile=profile, max_teams=max_teams)
        warnings: list[ParseWarning] = []
        now = datetime.now(UTC)
        results = await asyncio.gather(
            *(
                self.get_team_schedule(ref, ttl=self.settings.cache.ttl_live_score)
                for ref in refs
            ),
            return_exceptions=True,
        )
        states: dict[str, ScoreState] = {}
        oldest = now.timestamp()
        tier: SourceTier = "json_api"
        cache_state: CacheState = "fresh"
        for ref, result in zip(refs, results, strict=True):
            if isinstance(result, BaseException):
                warnings.append(
                    ParseWarning(
                        code="scope_partial",
                        message=f"schedule for {ref.display()} unavailable: {result}",
                        path=ref.path(),
                    )
                )
                continue
            oldest = min(oldest, result.fetched_at.timestamp())
            tier = result.source_tier
            if result.cache_state != "fresh":
                cache_state = result.cache_state
            warnings.extend(result.warnings)
            for contest in result.data.contests:
                state = self._window_state(contest, result.data.team, now, window_hours, finals_within_hours)
                if state is None:
                    continue
                key = state.contest_id or f"{state.home.name}|{state.away.name}|{state.starts_at_utc}"
                existing = states.get(key)
                if existing is None or (state.status is GameStatus.IN_PROGRESS):
                    states[key] = state
        ordered = sorted(
            states.values(),
            key=lambda s: (
                s.status is not GameStatus.IN_PROGRESS,
                s.starts_at_utc or now,
            ),
        )
        return Response.wrap(
            ordered,
            fetched_at=datetime.fromtimestamp(oldest, tz=UTC),
            source_tier=tier,
            cache_state=cache_state,
            warnings=warnings,
            request_id=new_request_id(),
        )

    def _window_state(
        self,
        contest: Contest,
        team: Any,
        now: datetime,
        window_hours: float,
        finals_within_hours: float,
    ) -> ScoreState | None:
        if contest.is_synthesized or contest.starts_at_utc is None:
            return None
        delta_hours = (contest.starts_at_utc - now).total_seconds() / 3600.0
        if contest.is_live:
            pass
        elif contest.has_result:
            if delta_hours < -finals_within_hours:
                return None
        elif not (0 <= delta_hours <= window_hours):
            return None
        ctx = json_api.Ctx(self._parse_mode)
        return json_api.build_score_state(contest, team, now=now, ctx=ctx)

    async def _resolve_scope(
        self,
        scope: str,
        *,
        teams: list[TeamRef | str] | None,
        profile: str | None,
        max_teams: int,
    ) -> list[TeamRef]:
        if teams:
            return [self.settings.resolve_team(t) for t in teams][:max_teams]
        primary = self.settings.resolve_team(None, profile=profile)
        if scope == "team":
            return [primary]
        if scope == "league":
            members = await self._league_member_refs(primary)
            return ([primary] + [m for m in members if m.path() != primary.path()])[:max_teams]
        if scope in {"state", "section", "division"}:
            refs = await self._rankings_refs(primary, scope)
            merged = [primary] + [r for r in refs if r.path() != primary.path()]
            return merged[:max_teams]
        raise ValueError(f"unknown scope {scope!r}; use team|league|section|division|state or teams=[...]")

    async def _league_member_refs(self, primary: TeamRef) -> list[TeamRef]:
        path = primary.path(tab="standings")
        try:
            hit = await self._cached_page(
                f"standings:{path}", path, ttl=self.settings.cache.ttl_standings, label="standings"
            )
        except MaxPrepsError as exc:
            log.warning("standings unavailable; league scope reduced to primary team", error=str(exc))
            return []
        wrapper = hit.value
        if wrapper["tier"] == "html":
            return []
        members = json_api.parse_standings_members(wrapper["payload"])
        refs: list[TeamRef] = []
        for _, url in members:
            try:
                ref = TeamRef.from_url(url)
            except ValueError:
                continue
            refs.append(
                TeamRef(
                    **{
                        **ref.model_dump(),
                        "sport": primary.sport,
                        "gender": primary.gender,
                        "level": primary.level,
                        "season": primary.season,
                    }
                )
            )
        return refs

    async def _rankings_refs(self, primary: TeamRef, scope: str) -> list[TeamRef]:
        rankings = await self.get_rankings(scope="state", profile=None, limit=50, _primary=primary)
        refs = []
        for entry in rankings.data.entries:
            if not entry.team_path:
                continue
            try:
                refs.append(TeamRef.from_url(entry.team_path))
            except ValueError:
                continue
        return refs

    # =============================================== 7. get_athlete_profile

    async def get_athlete_profile(
        self,
        athlete: str,
        *,
        team: TeamRef | str | None = None,
        profile: str | None = None,
    ) -> Response[AthleteProfile]:
        """``athlete`` is a careerId GUID, an athlete page URL, or a player
        name to look up on the (primary) team roster."""
        roster_entry = None
        athlete_path: str | None = None
        query: dict[str, str] | None = None

        candidate = athlete.strip()
        if candidate.startswith("http"):
            parts = urlsplit(candidate)
            athlete_path = parts.path.strip("/")
            if "careerid" in parts.query.lower():
                query = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair)
        elif _looks_like_guid(candidate):
            athlete_path = "local/player/stats.aspx"
            query = {"careerid": candidate}
        else:
            roster_resp = await self.get_team_roster(team, profile=profile)
            needle = strip_accents(candidate).casefold()
            matches = [
                e
                for e in roster_resp.data.entries
                if needle in strip_accents(e.display_name or "").casefold()
            ]
            if not matches:
                raise ContestNotFoundError(f"no roster player matching {candidate!r}")
            roster_entry = matches[0]
            if roster_entry.canonical_url:
                parts = urlsplit(roster_entry.canonical_url)
                athlete_path = parts.path.strip("/")
                if parts.query:
                    query = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair)
        if athlete_path is None:
            raise ContestNotFoundError(f"could not derive an athlete page for {athlete!r}")
        cache_key = f"athlete:{athlete_path}:{(query or {}).get('careerid', '')}"
        hit = await self._cached_page(
            cache_key,
            athlete_path,
            ttl=self.settings.cache.ttl_athlete,
            query=query,
            label="athlete",
        )
        wrapper = hit.value
        profile_data, ctx = json_api.parse_athlete(
            wrapper["payload"] if wrapper["tier"] != "html" else {},
            fallback=roster_entry,
            mode=self._parse_mode,
        )
        return self._envelope(profile_data, hit, ctx.warnings)

    # ===================================================== 8. get_rankings

    async def get_rankings(
        self,
        *,
        scope: str = "state",
        profile: str | None = None,
        limit: int = 25,
        page: int = 1,
        _primary: TeamRef | None = None,
    ) -> Response[Rankings]:
        primary = _primary or self.settings.resolve_team(None, profile=profile)
        sport = primary.sport
        season = primary.season
        if scope in {"league", "division", "section"}:
            # Windows on the team rankings tab; falls back to state on drift.
            path = primary.path(tab="rankings")
            hit = await self._cached_page(
                f"rankings:{path}", path, ttl=self.settings.cache.ttl_rankings, label="rankings"
            )
            wrapper = hit.value
            if wrapper["tier"] != "html":
                found = json_api.parse_rankings_contexts(
                    wrapper["payload"], scope=scope, want=scope
                )
                if found is not None:
                    return self._envelope(found, hit, [])
            warn = ParseWarning(
                code="rankings_scope_fallback",
                message=f"{scope} rankings window not found on team page; serving state rankings",
                path=path,
            )
            state_resp = await self.get_rankings(
                scope="state", profile=profile, limit=limit, page=page, _primary=primary
            )
            state_resp.warnings.append(warn)
            return state_resp

        if scope == "state":
            segments = [primary.state, sport]
        elif scope == "national":
            segments = [sport]
        else:
            raise ValueError(f"unknown rankings scope {scope!r}")
        if season:
            segments.append(season)
        path = "/".join([*segments, "rankings", str(page)])
        hit = await self._cached_page(
            f"rankings:{path}", path, ttl=self.settings.cache.ttl_rankings, label="rankings"
        )
        wrapper = hit.value
        rankings, ctx = json_api.parse_rankings_list(
            wrapper["payload"], scope=scope, mode=self._parse_mode
        )
        rankings.entries = rankings.entries[:limit]
        return self._envelope(rankings, hit, ctx.warnings)

    # ------------------------------------------------------------- doctor

    async def doctor(self) -> list[dict[str, Any]]:
        """Live shape checks against expectations — run before broadcast day."""
        checks: list[dict[str, Any]] = []

        async def run(name: str, coro: Any) -> None:
            started = time.monotonic()
            try:
                resp = await coro
                warn_codes = sorted({w.code for w in getattr(resp, "warnings", [])})
                checks.append(
                    {
                        "check": name,
                        "ok": True,
                        "tier": getattr(resp, "source_tier", "?"),
                        "cache": getattr(resp, "cache_state", "?"),
                        "warnings": warn_codes,
                        "seconds": round(time.monotonic() - started, 2),
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "check": name,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "seconds": round(time.monotonic() - started, 2),
                    }
                )

        await run("build_id", self._doctor_build_id())
        await run("schedule", self.get_team_schedule())
        await run("roster", self.get_team_roster())
        await run("scoretracker", self.get_scoretracker())
        await run("rankings", self.get_rankings(scope="state"))
        await run("search", self.search_schools(self.settings.primary.school_slug or "northwood"))
        return checks

    async def _doctor_build_id(self) -> Response[str]:
        build_id = await self._get_build_id(force=True)
        return Response.wrap(
            build_id,
            fetched_at=datetime.now(UTC),
            source_tier="hydration",
            cache_state="fresh",
            warnings=[],
            request_id=new_request_id(),
        )


def _looks_like_guid(value: str) -> bool:
    parts = value.split("-")
    return len(value) == 36 and len(parts) == 5 and all(
        all(ch in "0123456789abcdefABCDEF" for ch in p) for p in parts
    )
