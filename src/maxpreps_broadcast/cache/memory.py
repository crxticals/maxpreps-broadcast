"""In-memory LRU + TTL cache (tier 0, in front of the disk cache)."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class MemoryEntry:
    value: Any
    stored_at: float  # wall-clock epoch seconds (matches disk tier)
    ttl: float
    meta: dict[str, Any]

    def age(self, now: float) -> float:
        return max(0.0, now - self.stored_at)

    def is_fresh(self, now: float) -> bool:
        return self.age(now) <= self.ttl


class MemoryCache:
    def __init__(self, maxsize: int = 256, *, clock: Callable[[], float] = time.time) -> None:
        self._data: OrderedDict[str, MemoryEntry] = OrderedDict()
        self.maxsize = maxsize
        self._clock = clock

    def get(self, key: str) -> MemoryEntry | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        self._data.move_to_end(key)
        return entry

    def set(self, key: str, value: Any, *, ttl: float, stored_at: float | None = None,
            meta: dict[str, Any] | None = None) -> None:
        self._data[key] = MemoryEntry(
            value=value,
            stored_at=stored_at if stored_at is not None else self._clock(),
            ttl=ttl,
            meta=meta or {},
        )
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
