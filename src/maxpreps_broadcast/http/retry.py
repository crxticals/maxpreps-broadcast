"""Retry policy: exponential backoff with full jitter, capped.

Retries connection errors, timeouts, 429, 408 and 5xx.  Never retries other
4xx.  Honors ``Retry-After`` exactly when present (seconds or HTTP-date).
"""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from maxpreps_broadcast.errors import CircuitOpenError, RetryableError, TerminalError
from maxpreps_broadcast.obs import METRICS, get_logger

log = get_logger(__name__)
T = TypeVar("T")

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait, from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    parsed = email.utils.parsedate_to_datetime(value) if "," in value or ":" in value else None
    if parsed is None:
        return None
    now = now if now is not None else time.time()
    return max(0.0, parsed.timestamp() - now)


def classify_response(resp: httpx.Response) -> RetryableError | TerminalError | None:
    """None if OK; otherwise the error to raise."""
    if resp.status_code < 400:
        return None
    if resp.status_code in RETRYABLE_STATUSES:
        return RetryableError(
            f"HTTP {resp.status_code} from {resp.request.url}",
            retry_after=parse_retry_after(resp.headers.get("Retry-After")),
        )
    return TerminalError(f"HTTP {resp.status_code} from {resp.request.url}")


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 4
    base_seconds: float = 0.5
    cap_seconds: float = 15.0

    def backoff(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**attempt))."""
        ceiling = min(self.cap_seconds, self.base_seconds * (2**attempt))
        return (rng or random).uniform(0.0, ceiling)

    async def run(
        self,
        op: Callable[[], Awaitable[T]],
        *,
        label: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> T:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await op()
            except CircuitOpenError:
                raise  # fail fast so the cache layer can serve last-known-good
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout,
                    httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
                last = RetryableError(f"transport error: {exc}")
            except RetryableError as exc:
                last = exc
            except TerminalError:
                raise
            if attempt >= self.max_retries:
                break
            retry_after = getattr(last, "retry_after", None)
            delay = retry_after if retry_after is not None else self.backoff(attempt, rng=rng)
            METRICS.inc("retries_total", op=label)
            log.warning("retrying", op=label, attempt=attempt + 1, delay=round(delay, 2), error=str(last))
            await sleep(delay)
        assert last is not None
        raise last
