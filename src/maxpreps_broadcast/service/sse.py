"""Server-Sent Events broker for /stream.

One asyncio.Condition; every publish wakes all subscribers with the latest
event. Subscribers that lag simply skip to the newest state (a scorebug wants
"now", not a replay). Heartbeats keep proxies from reaping idle connections.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class LiveBroker:
    def __init__(self, *, heartbeat_seconds: float = 15.0) -> None:
        self._condition = asyncio.Condition()
        self._sequence = 0
        self._latest: dict[str, Any] | None = None
        self.heartbeat_seconds = heartbeat_seconds

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        async with self._condition:
            self._sequence += 1
            self._latest = {"event": event, "data": data, "id": self._sequence}
            self._condition.notify_all()

    @staticmethod
    def _format(message: dict[str, Any]) -> str:
        payload = json.dumps(message["data"], default=str)
        return f"id: {message['id']}\nevent: {message['event']}\ndata: {payload}\n\n"

    async def subscribe(self) -> AsyncIterator[str]:
        last_seen = 0
        # New subscribers immediately get the current state, if there is one.
        # (Read under the lock, yield outside it: a generator paused mid-yield
        # must never hold the condition, or publishers deadlock.)
        async with self._condition:
            initial = self._latest
        if initial is not None:
            last_seen = initial["id"]
            yield self._format(initial)
        while True:
            async with self._condition:
                def newer(seen: int = last_seen) -> bool:
                    return self._latest is not None and self._latest["id"] > seen

                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(newer),
                        timeout=self.heartbeat_seconds,
                    )
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                assert self._latest is not None
                last_seen = self._latest["id"]
                message = self._latest
            yield self._format(message)
