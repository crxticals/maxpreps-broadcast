"""Observability: structlog JSON logging with correlation ids, plus in-process metrics.

Counters cover requests, cache hits/misses, retries, breaker trips and parse
warnings; latency is kept as a bounded reservoir per label so ``/metrics`` and
``maxpreps stats`` can report p50/p95 without unbounded memory.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any

import structlog

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id() -> str:
    """Mint a correlation id and bind it to the current context."""
    rid = uuid.uuid4().hex[:12]
    _request_id.set(rid)
    return rid


def current_request_id() -> str:
    return _request_id.get()


def _add_request_id(
    _logger: structlog.typing.WrappedLogger,
    _name: str,
    event_dict: structlog.typing.EventDict,
) -> structlog.typing.EventDict:
    event_dict.setdefault("request_id", _request_id.get())
    return event_dict


def configure_logging(*, json_output: bool = True, level: str = "info") -> None:
    """Configure structlog once, idempotently."""
    import logging

    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    renderer: structlog.typing.Processor
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), level.upper(), 20)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


class Metrics:
    """Thread-safe counters and latency reservoirs."""

    _RESERVOIR = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._latency: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self._RESERVOIR))
        self.started_at = time.time()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        if labels:
            rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = name + "{" + rendered + "}"
        else:
            key = name
        with self._lock:
            self._counters[key] += value

    def observe_latency(self, label: str, seconds: float) -> None:
        with self._lock:
            self._latency[label].append(seconds)

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
        return ordered[idx]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            latency = {k: list(v) for k, v in self._latency.items()}
        lat_summary = {
            label: {
                "count": len(vals),
                "p50_ms": round(self._percentile(vals, 50) * 1000, 2),
                "p95_ms": round(self._percentile(vals, 95) * 1000, 2),
            }
            for label, vals in latency.items()
        }
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "counters": counters,
            "latency": lat_summary,
        }

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        lines: list[str] = []
        for key, value in sorted(snap["counters"].items()):
            base = key.split("{", 1)[0]
            lines.append(f"# TYPE maxpreps_{base} counter")
            lines.append(f"maxpreps_{key} {value}")
        for label, stats in sorted(snap["latency"].items()):
            lines.append(f'maxpreps_latency_p50_ms{{op="{label}"}} {stats["p50_ms"]}')
            lines.append(f'maxpreps_latency_p95_ms{{op="{label}"}} {stats["p95_ms"]}')
        lines.append(f"maxpreps_uptime_seconds {snap['uptime_seconds']}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
