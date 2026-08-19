"""The local broadcast service: FastAPI on 127.0.0.1:8787.

Routes (all wrapped in the standard envelope):

    GET /live       current ScoreState        GET /schedule   full schedule
    GET /roster     roster                    GET /rankings   rankings
    GET /search?q=  school search             GET /games      live+upcoming (scope)
    GET /athlete/{id}                         GET /healthz    go/no-go introspection
    GET /metrics    Prometheus text           GET /stream     SSE score deltas
    GET /sports     catalogue + active        PUT /sports/active  set the selection
    GET /schedules  every active sport        GET /lives      every active sport

``/live``, ``/schedule`` and ``/roster`` take ``?sport=`` for one sport; the
plural routes cover the whole active set and report per-sport failures inline
rather than failing the request.

Watch mode (also usable headless via ``maxpreps serve --watch``): a background
task polls the scoretracker at ``--interval``, diffs states, atomically
rewrites ``live.json``/``live.mgjson`` (+ periodic ``schedule.json`` /
``roster.json``), and publishes change events to /stream.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from maxpreps_broadcast import sports
from maxpreps_broadcast.client import MaxPrepsClient
from maxpreps_broadcast.config import Settings, save_settings
from maxpreps_broadcast.errors import (
    MaxPrepsError,
    PrimarySchoolNotConfiguredError,
    TooManySportsError,
    UnknownSportError,
)
from maxpreps_broadcast.export import json_out
from maxpreps_broadcast.export.atomic import atomic_write_json
from maxpreps_broadcast.export.mgjson import mgjson_for_live, write_mgjson
from maxpreps_broadcast.models import Response as Envelope
from maxpreps_broadcast.models import ScoreState, diff_score_states
from maxpreps_broadcast.obs import METRICS, get_logger
from maxpreps_broadcast.service.health import build_health
from maxpreps_broadcast.service.sse import LiveBroker

log = get_logger(__name__)


class Watcher:
    """Poll → diff → atomic write → publish.  Never crashes the loop."""

    def __init__(
        self,
        client: MaxPrepsClient,
        broker: LiveBroker,
        *,
        out_dir: Path,
        interval_seconds: float = 5.0,
        slow_refresh_seconds: float = 600.0,
    ) -> None:
        self.client = client
        self.broker = broker
        self.out_dir = out_dir
        self.interval = interval_seconds
        self.slow_refresh = slow_refresh_seconds
        self.previous: ScoreState | None = None
        self.history: list[tuple[datetime, int | None, int | None, int | None]] = []
        self.last_slow = 0.0
        self.ticks = 0
        self.errors = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        log.info("watch mode started", out=str(self.out_dir), interval=self.interval)
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                log.warning("watch tick failed; last good files remain in place", error=str(exc))
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.05, self.interval - elapsed))

    async def tick(self) -> None:
        self.ticks += 1
        resp = await self.client.get_scoretracker()
        state = resp.data
        now = datetime.now(UTC)
        self.history.append((now, state.home_score, state.away_score, state.period))
        if len(self.history) > 4000:
            del self.history[: len(self.history) - 4000]
        changes = diff_score_states(self.previous, state)
        first_tick = self.previous is None
        assets = None if self.client.settings.offline else self.out_dir / "assets"
        view = json_out.live_view(resp, self.client.settings, assets_dir=assets)
        view["exported_at"] = now.isoformat()
        atomic_write_json(self.out_dir / "live.json", view)
        if "mgjson" in self.client.settings.export.formats:
            write_mgjson(self.out_dir / "live.mgjson", mgjson_for_live(view, history=self.history))
        if changes or first_tick:
            await self.broker.publish(
                "changes",
                {
                    "changes": [c.model_dump(mode="json") for c in changes],
                    "state": view,
                },
            )
            METRICS.inc("watch_changes_total")
        loop_now = asyncio.get_running_loop().time()
        if loop_now - self.last_slow > self.slow_refresh:
            self.last_slow = loop_now
            schedule = await self.client.get_team_schedule(include_byes=True)
            roster = await self.client.get_team_roster()
            json_out.write_views(
                self.out_dir, self.client.settings, schedule=schedule, roster=roster
            )
        self.previous = state

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "interval_seconds": self.interval,
            "out_dir": str(self.out_dir),
            "ticks": self.ticks,
            "errors": self.errors,
            "history_points": len(self.history),
        }


def _dump(envelope: Envelope[Any]) -> dict[str, Any]:
    return envelope.model_dump(mode="json")


def _dump_multi(results: dict[str, Envelope[Any] | MaxPrepsError]) -> dict[str, Any]:
    """Serialize a per-sport map, keeping failures inline and named."""
    out: dict[str, Any] = {"sports": {}, "ok": [], "failed": []}
    for key, result in results.items():
        if isinstance(result, MaxPrepsError):
            out["sports"][key] = {"error": f"{type(result).__name__}: {result}"}
            out["failed"].append(key)
        else:
            out["sports"][key] = _dump(result)
            out["ok"].append(key)
    return out


class ActiveSportsBody(BaseModel):
    """Body of ``PUT /sports/active`` — the shape a picker UI posts."""

    sports: list[str] = Field(default_factory=list)


def create_app(
    settings: Settings | None = None,
    *,
    client: MaxPrepsClient | None = None,
    watch: bool = False,
    watch_out: Path | None = None,
    watch_interval: float = 5.0,
    persist_selection: bool = True,
) -> FastAPI:
    """
    ``persist_selection`` writes sport changes back to the config file so a
    restart keeps them.  Turn it off where the filesystem is ephemeral or
    read-only (a container image), leaving changes in memory only.
    """
    owned_client = client is None
    broker = LiveBroker()
    state: dict[str, Any] = {"client": client, "watcher": None}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if state["client"] is None:
            state["client"] = MaxPrepsClient(settings)
        if watch:
            watcher = Watcher(
                state["client"],
                broker,
                out_dir=watch_out or Path(state["client"].settings.export.out_dir),
                interval_seconds=watch_interval,
            )
            watcher.start()
            state["watcher"] = watcher
        try:
            yield
        finally:
            if state["watcher"] is not None:
                await state["watcher"].stop()
            if owned_client and state["client"] is not None:
                await state["client"].aclose()

    app = FastAPI(title="maxpreps-broadcast", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "PUT"],
        allow_headers=["*"],
    )

    def the_client() -> MaxPrepsClient:
        c = state["client"]
        if not isinstance(c, MaxPrepsClient):  # pragma: no cover — lifespan always sets it
            raise HTTPException(503, "client not ready")
        return c

    def http_error(exc: MaxPrepsError) -> HTTPException:
        if isinstance(exc, PrimarySchoolNotConfiguredError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        watcher = state["watcher"]
        return build_health(the_client(), watch=watcher.snapshot() if watcher else None)

    @app.get("/sports")
    async def sports_catalogue() -> dict[str, Any]:
        """The catalogue plus the current selection.

        A picker UI renders entirely from this: seasons, their sports, the
        per-season preset, the cap, and what is active right now.
        """
        cfg = the_client().settings
        return {
            **sports.catalogue(),
            "active": [s.summary() for s in cfg.active_sports()],
        }

    @app.put("/sports/active")
    async def set_active_sports(body: ActiveSportsBody) -> dict[str, Any]:
        """Replace the selection.  Rejects unknown names and over-long lists
        before touching state, so a bad request changes nothing."""
        cfg = the_client().settings
        try:
            active = cfg.set_active_sports(body.sports)
        except (UnknownSportError, TooManySportsError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if persist_selection:
            save_settings(cfg)
        return {"active": [s.summary() for s in active]}

    @app.get("/live")
    async def live(sport: str | None = None) -> dict[str, Any]:
        try:
            return _dump(await the_client().get_scoretracker(sport=sport))
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/schedule")
    async def schedule(
        include_byes: bool = True, season: str | None = None, sport: str | None = None
    ) -> dict[str, Any]:
        try:
            return _dump(
                await the_client().get_team_schedule(
                    include_byes=include_byes, season=season, sport=sport
                )
            )
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/schedules")
    async def schedules(include_byes: bool = True, season: str | None = None) -> dict[str, Any]:
        """Every active sport's schedule in one response.

        Per-sport failures come back as ``{"error": ...}`` entries rather than
        failing the whole request — the point of one call is that five good
        sports still arrive when the sixth is dark.
        """
        results = await the_client().get_schedules(season=season, include_byes=include_byes)
        return _dump_multi(results)

    @app.get("/lives")
    async def lives() -> dict[str, Any]:
        """Current scorebug state for every active sport."""
        return _dump_multi(await the_client().get_scoretrackers())

    @app.get("/roster")
    async def roster(sort: str = "jersey") -> dict[str, Any]:
        try:
            return _dump(await the_client().get_team_roster(sort=sort))
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/rankings")
    async def rankings(scope: str = "state") -> dict[str, Any]:
        try:
            return _dump(await the_client().get_rankings(scope=scope))
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/search")
    async def search(q: str = Query(min_length=2), state_code: str | None = None) -> dict[str, Any]:
        try:
            return _dump(await the_client().search_schools(q, state=state_code))
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/games")
    async def games(scope: str = "league", window_hours: float = 48.0) -> dict[str, Any]:
        try:
            return _dump(
                await the_client().get_live_and_upcoming_games(scope=scope, window_hours=window_hours)
            )
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/athlete/{athlete_id}")
    async def athlete(athlete_id: str) -> dict[str, Any]:
        try:
            return _dump(await the_client().get_athlete_profile(athlete_id))
        except MaxPrepsError as exc:
            raise http_error(exc) from exc

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        return METRICS.render_prometheus()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(
            broker.subscribe(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
