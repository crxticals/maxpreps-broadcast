"""Per-host circuit breaker.

Opens after N consecutive failures; after a cooldown one half-open probe is
allowed through.  When open, callers get ``CircuitOpenError`` immediately so
the cache layer can serve last-known-good instead of hammering a dead host.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

from maxpreps_broadcast.errors import CircuitOpenError
from maxpreps_broadcast.obs import METRICS, get_logger

log = get_logger(__name__)


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        self._maybe_half_open()
        return self._state

    def _maybe_half_open(self) -> None:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self.cooldown_seconds:
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = False

    def before_request(self, host: str) -> None:
        """Raise ``CircuitOpenError`` unless a request may proceed."""
        self._maybe_half_open()
        if self._state is BreakerState.OPEN:
            remaining = self.cooldown_seconds - (self._clock() - self._opened_at)
            raise CircuitOpenError(host, cooldown_remaining=max(0.0, remaining))
        if self._state is BreakerState.HALF_OPEN:
            if self._probe_in_flight:
                remaining = self.cooldown_seconds - (self._clock() - self._opened_at)
                raise CircuitOpenError(host, cooldown_remaining=max(0.0, remaining))
            self._probe_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._probe_in_flight = False
        self._state = BreakerState.CLOSED

    def record_failure(self, host: str) -> None:
        self._probe_in_flight = False
        if self._state is BreakerState.HALF_OPEN:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
            METRICS.inc("breaker_trips_total", host=host)
            log.warning("breaker re-opened after failed probe", host=host)
            return
        self._failures += 1
        if self._failures >= self.failure_threshold and self._state is BreakerState.CLOSED:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
            METRICS.inc("breaker_trips_total", host=host)
            log.warning("breaker opened", host=host, failures=self._failures)


class BreakerRegistry:
    def __init__(self, *, failure_threshold: int = 5, cooldown_seconds: float = 30.0) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds

    def for_host(self, host: str) -> CircuitBreaker:
        if host not in self._breakers:
            self._breakers[host] = CircuitBreaker(
                failure_threshold=self._threshold, cooldown_seconds=self._cooldown
            )
        return self._breakers[host]

    def snapshot(self) -> dict[str, str]:
        return {host: b.state.value for host, b in self._breakers.items()}
