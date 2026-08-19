"""Resilience primitives: retry timing, rate limit, breaker, robots."""

from __future__ import annotations

import random

import httpx
import pytest

from maxpreps_broadcast.errors import (
    CircuitOpenError,
    RetryableError,
    RobotsDisallowedError,
    TerminalError,
)
from maxpreps_broadcast.http.breaker import BreakerState, CircuitBreaker
from maxpreps_broadcast.http.ratelimit import TokenBucket
from maxpreps_broadcast.http.retry import RetryPolicy, classify_response, parse_retry_after
from maxpreps_broadcast.http.robots import RobotsGate


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("GET", "https://x/"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestClassification:
    def test_ok(self):
        assert classify_response(_response(200)) is None
        assert classify_response(_response(304)) is None

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable(self, status):
        error = classify_response(_response(status))
        assert isinstance(error, RetryableError)

    @pytest.mark.parametrize("status", [400, 403, 404, 410])
    def test_terminal(self, status):
        error = classify_response(_response(status))
        assert isinstance(error, TerminalError)
        assert not isinstance(error, RetryableError)

    def test_retry_after_header_lands_on_error(self):
        error = classify_response(_response(429, {"Retry-After": "7"}))
        assert isinstance(error, RetryableError)
        assert error.retry_after == 7.0

    def test_parse_retry_after_forms(self):
        assert parse_retry_after("7") == 7.0
        assert parse_retry_after(None) is None
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=1_500_000_000.0) > 0


class TestRetryPolicy:
    async def test_exhaustion_after_max_retries(self):
        clock = FakeClock()
        policy = RetryPolicy(max_retries=3, base_seconds=0.5, cap_seconds=15.0)
        calls = 0

        async def always_503() -> httpx.Response:
            nonlocal calls
            calls += 1
            raise classify_response(_response(503))

        with pytest.raises(RetryableError):
            await policy.run(always_503, label="t", sleep=clock.sleep, rng=random.Random(7))
        assert calls == 4          # initial + 3 retries
        assert len(clock.sleeps) == 3

    async def test_full_jitter_bounds(self):
        clock = FakeClock()
        policy = RetryPolicy(max_retries=3, base_seconds=0.5, cap_seconds=15.0)

        async def always_503() -> httpx.Response:
            raise classify_response(_response(503))

        with pytest.raises(RetryableError):
            await policy.run(always_503, label="t", sleep=clock.sleep, rng=random.Random(7))
        for attempt, slept in enumerate(clock.sleeps):
            assert 0.0 <= slept <= min(15.0, 0.5 * (2**attempt))

    async def test_retry_after_is_honored_exactly(self):
        clock = FakeClock()
        policy = RetryPolicy(max_retries=3, base_seconds=0.5, cap_seconds=15.0)
        script = [classify_response(_response(429, {"Retry-After": "4"})), None]

        async def fetch() -> httpx.Response:
            step = script.pop(0)
            if step is not None:
                raise step
            return _response(200)

        result = await policy.run(fetch, label="t", sleep=clock.sleep)
        assert result.status_code == 200
        assert clock.sleeps == [4.0]   # not jittered — the server named its price

    async def test_terminal_never_retries(self):
        clock = FakeClock()
        policy = RetryPolicy(max_retries=4)
        calls = 0

        async def not_found() -> httpx.Response:
            nonlocal calls
            calls += 1
            raise classify_response(_response(404))

        with pytest.raises(TerminalError):
            await policy.run(not_found, label="t", sleep=clock.sleep)
        assert calls == 1
        assert clock.sleeps == []

    async def test_circuit_open_fails_fast(self):
        clock = FakeClock()
        policy = RetryPolicy(max_retries=4)

        async def blocked() -> httpx.Response:
            raise CircuitOpenError("www.maxpreps.com", cooldown_remaining=12.0)

        with pytest.raises(CircuitOpenError):
            await policy.run(blocked, label="t", sleep=clock.sleep)
        assert clock.sleeps == []   # no retries: the cache layer serves LKG instead

    async def test_transport_errors_are_retryable(self):
        clock = FakeClock()
        policy = RetryPolicy(max_retries=3)
        attempts = 0

        async def flaky() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://x/"))
            return _response(200)

        result = await policy.run(flaky, label="t", sleep=clock.sleep)
        assert result.status_code == 200
        assert attempts == 3


class TestTokenBucket:
    async def test_burst_drains_without_waiting(self):
        clock = FakeClock()
        bucket = TokenBucket(2.0, burst=2, clock=clock)
        await bucket.acquire()
        await bucket.acquire()
        assert bucket._tokens < 1.0  # burst spent, no wall-clock wait needed

    async def test_refill_rate(self):
        clock = FakeClock()
        bucket = TokenBucket(2.0, burst=1, clock=clock)
        await bucket.acquire()
        clock.now += 0.5             # 0.5s at 2 rps = exactly one token back
        await bucket.acquire()       # returns immediately, no real sleep
        assert bucket._tokens < 1.0

    def test_rejects_nonpositive_rate(self):
        with pytest.raises(ValueError):
            TokenBucket(0.0)


class TestCircuitBreaker:
    def test_open_after_threshold_then_probe_then_close(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=clock)
        for _ in range(3):
            breaker.before_request("h")
            breaker.record_failure("h")
        assert breaker.state is BreakerState.OPEN
        with pytest.raises(CircuitOpenError):
            breaker.before_request("h")
        clock.now += 31
        breaker.before_request("h")            # the single half-open probe
        assert breaker.state is BreakerState.HALF_OPEN
        with pytest.raises(CircuitOpenError):
            breaker.before_request("h")        # concurrent second probe rejected
        breaker.record_success()
        assert breaker.state is BreakerState.CLOSED

    def test_half_open_failure_reopens(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
        breaker.before_request("h")
        breaker.record_failure("h")
        clock.now += 11
        breaker.before_request("h")
        breaker.record_failure("h")
        assert breaker.state is BreakerState.OPEN

    def test_cooldown_remaining_reported(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
        breaker.before_request("h")
        breaker.record_failure("h")
        clock.now += 10
        with pytest.raises(CircuitOpenError) as excinfo:
            breaker.before_request("h")
        assert 19 <= excinfo.value.cooldown_remaining <= 20


class _RobotsTransport(httpx.AsyncBaseTransport):
    def __init__(self, status: int, body: str = "") -> None:
        self.status, self.body = status, body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, text=self.body, request=request)


class TestRobotsGate:
    async def test_disallow_rule_fails_closed(self):
        client = httpx.AsyncClient(
            transport=_RobotsTransport(200, "User-agent: *\nDisallow: /private/\n")
        )
        gate = RobotsGate(user_agent="test-bot")
        await gate.check("https://example.com/public/page", client)
        with pytest.raises(RobotsDisallowedError):
            await gate.check("https://example.com/private/page", client)
        await client.aclose()

    async def test_robots_5xx_blocks_everything(self):
        client = httpx.AsyncClient(transport=_RobotsTransport(503))
        gate = RobotsGate(user_agent="test-bot")
        with pytest.raises(RobotsDisallowedError):
            await gate.check("https://example.com/anything", client)
        assert gate.snapshot()["example.com"] == "disallow_all"
        await client.aclose()

    async def test_robots_404_allows_all(self):
        client = httpx.AsyncClient(transport=_RobotsTransport(404))
        gate = RobotsGate(user_agent="test-bot")
        await gate.check("https://example.com/anything", client)
        assert gate.snapshot()["example.com"] == "allow_all"
        await client.aclose()
