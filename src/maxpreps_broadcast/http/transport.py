"""Transport: httpx with HTTP/2, keep-alive pooling and per-host client reuse,
wrapped in the full politeness/resilience stack:

    robots gate → token bucket → concurrency semaphore → breaker → retries

Conditional requests: callers can pass stored ``ETag`` / ``Last-Modified``
validators; a 304 comes back as ``not_modified=True`` with no body.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from maxpreps_broadcast.errors import RetryableError
from maxpreps_broadcast.http.breaker import BreakerRegistry
from maxpreps_broadcast.http.ratelimit import TokenBucket
from maxpreps_broadcast.http.retry import RetryPolicy, classify_response
from maxpreps_broadcast.http.robots import RobotsGate
from maxpreps_broadcast.obs import METRICS, get_logger

log = get_logger(__name__)


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: dict[str, str]
    not_modified: bool = False
    elapsed_seconds: float = 0.0

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Transport:
    def __init__(
        self,
        *,
        user_agent: str,
        requests_per_second: float = 1.0,
        max_concurrency: int = 4,
        timeout_seconds: float = 10.0,
        max_retries: int = 4,
        breaker_failure_threshold: int = 5,
        breaker_cooldown_seconds: float = 30.0,
        respect_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._bucket = TokenBucket(requests_per_second, burst=max(2, int(requests_per_second * 2)))
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = timeout_seconds
        self.retry_policy = RetryPolicy(max_retries=max_retries)
        self.breakers = BreakerRegistry(
            failure_threshold=breaker_failure_threshold, cooldown_seconds=breaker_cooldown_seconds
        )
        self.robots = RobotsGate(user_agent=user_agent)
        self._respect_robots = respect_robots

    def client_for(self, host: str) -> httpx.AsyncClient:
        if host not in self._clients:
            self._clients[host] = httpx.AsyncClient(
                http2=True,
                timeout=self._timeout,
                headers={"User-Agent": self.user_agent, "Accept": "application/json, text/html;q=0.8"},
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                follow_redirects=True,
            )
        return self._clients[host]

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        label: str = "get",
    ) -> FetchResult:
        host = urlsplit(url).netloc
        client = self.client_for(host)
        if self._respect_robots:
            await self.robots.check(url, client)
        breaker = self.breakers.for_host(host)

        async def attempt() -> FetchResult:
            breaker.before_request(host)
            await self._bucket.acquire()
            headers: dict[str, str] = {}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            started = time.monotonic()
            try:
                async with self._semaphore:
                    resp = await client.get(url, headers=headers)
            except httpx.HTTPError:
                breaker.record_failure(host)
                raise
            elapsed = time.monotonic() - started
            METRICS.inc("requests_total", host=host)
            METRICS.observe_latency(label, elapsed)
            if resp.status_code == 304:
                breaker.record_success()
                return FetchResult(
                    url=url, status=304, content=b"", headers=dict(resp.headers),
                    not_modified=True, elapsed_seconds=elapsed,
                )
            error = classify_response(resp)
            if error is not None:
                if isinstance(error, RetryableError):
                    breaker.record_failure(host)
                raise error
            breaker.record_success()
            return FetchResult(
                url=url,
                status=resp.status_code,
                content=resp.content,
                headers=dict(resp.headers),
                elapsed_seconds=elapsed,
            )

        return await self.retry_policy.run(attempt, label=label)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> Transport:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
