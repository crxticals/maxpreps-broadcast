"""Exception hierarchy.

The split that matters operationally is ``RetryableError`` vs ``TerminalError``:
the retry loop backs off and retries the former and fails fast on the latter.
Everything raised by this package derives from ``MaxPrepsError``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AmbiguousTeamError",
    "CacheMissError",
    "CircuitOpenError",
    "ContestNotFoundError",
    "FetchError",
    "MaxPrepsError",
    "OfflineMissError",
    "PrimarySchoolNotConfiguredError",
    "RetryableError",
    "RobotsDisallowedError",
    "SchemaDriftError",
    "TerminalError",
    "TooManySportsError",
    "UnknownSportError",
]


class MaxPrepsError(Exception):
    """Base class for every error raised by maxpreps-broadcast."""


class RetryableError(MaxPrepsError):
    """Transient failure: connection error, timeout, 429, 408, or 5xx."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TerminalError(MaxPrepsError):
    """Failure that retrying will not fix (4xx other than 408/429, parse dead-ends)."""


class FetchError(TerminalError):
    """A fetch failed terminally across every source tier."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RobotsDisallowedError(TerminalError):
    """The target path is disallowed by robots.txt.  We fail closed."""

    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows fetching {url}")
        self.url = url


class CircuitOpenError(RetryableError):
    """The per-host circuit breaker is open; the fetch was not attempted."""

    def __init__(self, host: str, *, cooldown_remaining: float) -> None:
        super().__init__(f"circuit open for {host} ({cooldown_remaining:.1f}s cooldown remaining)")
        self.host = host
        self.cooldown_remaining = cooldown_remaining


class SchemaDriftError(TerminalError):
    """A *required* field is missing or has the wrong shape.

    Carries the JSON-ish path of the offending payload location so ``doctor``
    and test output can point at exactly what moved.
    """

    def __init__(self, message: str, *, path: str, payload: Any = None) -> None:
        super().__init__(f"{message} (at {path})")
        self.path = path
        self.payload = payload


class AmbiguousTeamError(TerminalError):
    """``resolve()`` matched more than one school; candidates are attached."""

    def __init__(self, query: str, candidates: list[Any]) -> None:
        names = ", ".join(str(c) for c in candidates[:5])
        super().__init__(f"query {query!r} is ambiguous: {names}" + ("…" if len(candidates) > 5 else ""))
        self.query = query
        self.candidates = candidates


class ContestNotFoundError(TerminalError):
    """A contest id could not be located in any reachable schedule."""


class PrimarySchoolNotConfiguredError(TerminalError):
    """No team argument was given and no primary school is configured.

    Run ``maxpreps init`` (or set ``MAXPREPS_PRIMARY_*``) to fix.
    """


class UnknownSportError(TerminalError):
    """A sport name is not in the catalogue.  Carries the valid keys."""

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(f"unknown sport {name!r}; known sports: {', '.join(known)}")
        self.name = name
        self.known = known


class TooManySportsError(TerminalError):
    """More sports were selected than the graphics loop is allowed to rotate."""

    def __init__(self, requested: int, maximum: int, keys: list[str]) -> None:
        super().__init__(
            f"{requested} sports selected but the maximum is {maximum}: {', '.join(keys)}"
        )
        self.requested = requested
        self.maximum = maximum
        self.keys = keys


class CacheMissError(MaxPrepsError):
    """Internal: no cached value exists for a key."""


class OfflineMissError(TerminalError):
    """``--offline`` is active and neither cache tier holds the requested data."""
