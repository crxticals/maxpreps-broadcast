"""Politeness: async token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Token bucket at ``rps`` with a burst of ``burst`` tokens.

    ``acquire()`` waits until a token is available; time source is injectable
    for tests.
    """

    def __init__(
        self,
        rps: float,
        burst: int = 2,
        *,
        clock: object = None,
    ) -> None:
        if rps <= 0:
            raise ValueError("rps must be > 0")
        self.rps = rps
        self.capacity = float(max(1, burst))
        self._tokens = self.capacity
        self._clock = clock if callable(clock) else time.monotonic
        self._last = float(self._clock())
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = float(self._clock())
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rps)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self.rps
            await asyncio.sleep(min(deficit, 1.0))
