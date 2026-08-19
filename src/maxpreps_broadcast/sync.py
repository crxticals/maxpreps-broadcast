"""Synchronous facade over the async core.

A ``SyncClient`` owns one background event loop thread, so HTTP keep-alive,
the token bucket, breakers, and the memory cache all persist across calls —
``asyncio.run`` per call would throw that away.  Module-level convenience
functions lazily share one default client.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from maxpreps_broadcast.client import MaxPrepsClient
from maxpreps_broadcast.config import Settings, load_settings
from maxpreps_broadcast.errors import MaxPrepsError
from maxpreps_broadcast.models import (
    AthleteProfile,
    Rankings,
    Response,
    Roster,
    RosterSort,
    Schedule,
    School,
    ScoreState,
    TeamRef,
)

T = TypeVar("T")


class SyncClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="maxpreps-loop")
        self._thread.start()
        self.settings = settings or load_settings()
        self._client: MaxPrepsClient = self._run(self._make_client())
        atexit.register(self.close)

    async def _make_client(self) -> MaxPrepsClient:
        return MaxPrepsClient(self.settings)

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        if not self._loop.is_running():
            raise RuntimeError("sync client loop is not running (already closed?)")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # ------------------------------------------------------- the eight

    def search_schools(self, query: str, **kw: Any) -> Response[list[School]]:
        return self._run(self._client.search_schools(query, **kw))

    def resolve_school(self, query: str, **kw: Any) -> TeamRef:
        return self._run(self._client.resolve_school(query, **kw))

    def get_team_schedule(self, team: TeamRef | str | None = None, **kw: Any) -> Response[Schedule]:
        return self._run(self._client.get_team_schedule(team, **kw))

    def get_schedules(
        self, sports_wanted: list[Any] | None = None, **kw: Any
    ) -> dict[str, Response[Schedule] | MaxPrepsError]:
        return self._run(self._client.get_schedules(sports_wanted, **kw))

    def get_scoretrackers(
        self, sports_wanted: list[Any] | None = None, **kw: Any
    ) -> dict[str, Response[ScoreState] | MaxPrepsError]:
        return self._run(self._client.get_scoretrackers(sports_wanted, **kw))

    def get_team_roster(
        self, team: TeamRef | str | None = None, *, sort: RosterSort | str = RosterSort.JERSEY, **kw: Any
    ) -> Response[Roster]:
        return self._run(self._client.get_team_roster(team, sort=sort, **kw))

    def get_scoretracker(self, team: TeamRef | str | None = None, **kw: Any) -> Response[ScoreState]:
        return self._run(self._client.get_scoretracker(team, **kw))

    def get_scoretracker_by_id(self, contest_id: str, **kw: Any) -> Response[ScoreState]:
        return self._run(self._client.get_scoretracker_by_id(contest_id, **kw))

    def get_live_and_upcoming_games(self, **kw: Any) -> Response[list[ScoreState]]:
        return self._run(self._client.get_live_and_upcoming_games(**kw))

    def get_athlete_profile(self, athlete: str, **kw: Any) -> Response[AthleteProfile]:
        return self._run(self._client.get_athlete_profile(athlete, **kw))

    def get_rankings(self, **kw: Any) -> Response[Rankings]:
        return self._run(self._client.get_rankings(**kw))

    def doctor(self) -> list[dict[str, Any]]:
        return self._run(self._client.doctor())

    # ------------------------------------------------------- lifecycle

    def close(self) -> None:
        if self._loop.is_closed():
            return
        if self._loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._client.aclose(), self._loop).result(timeout=10)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        self._loop.close()

    def __enter__(self) -> SyncClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_default: SyncClient | None = None
_default_lock = threading.Lock()


def default_client(settings: Settings | None = None) -> SyncClient:
    global _default
    with _default_lock:
        if _default is None:
            _default = SyncClient(settings)
        return _default


def search_schools(query: str, **kw: Any) -> Response[list[School]]:
    return default_client().search_schools(query, **kw)


def get_team_schedule(team: TeamRef | str | None = None, **kw: Any) -> Response[Schedule]:
    return default_client().get_team_schedule(team, **kw)


def get_team_roster(team: TeamRef | str | None = None, **kw: Any) -> Response[Roster]:
    return default_client().get_team_roster(team, **kw)


def get_scoretracker(team: TeamRef | str | None = None, **kw: Any) -> Response[ScoreState]:
    return default_client().get_scoretracker(team, **kw)


def get_scoretracker_by_id(contest_id: str, **kw: Any) -> Response[ScoreState]:
    return default_client().get_scoretracker_by_id(contest_id, **kw)


def get_live_and_upcoming_games(**kw: Any) -> Response[list[ScoreState]]:
    return default_client().get_live_and_upcoming_games(**kw)


def get_athlete_profile(athlete: str, **kw: Any) -> Response[AthleteProfile]:
    return default_client().get_athlete_profile(athlete, **kw)


def get_rankings(**kw: Any) -> Response[Rankings]:
    return default_client().get_rankings(**kw)
