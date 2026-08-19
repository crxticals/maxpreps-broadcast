"""Disk cache: a small SQLite key-value store (tier 1).

Stores the raw JSON-serializable payload per normalized request key, with
``stored_at``, per-entry TTL and the HTTP validators (ETag / Last-Modified)
for conditional revalidation.  WAL mode; a single connection guarded by a
lock — every operation is tiny.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    stored_at REAL NOT NULL,
    ttl REAL NOT NULL,
    etag TEXT,
    last_modified TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
"""


@dataclass
class DiskEntry:
    value: Any
    stored_at: float
    ttl: float
    etag: str | None
    last_modified: str | None
    meta: dict[str, Any]

    def age(self, now: float) -> float:
        return max(0.0, now - self.stored_at)

    def is_fresh(self, now: float) -> bool:
        return self.age(now) <= self.ttl


class DiskCache:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, key: str) -> DiskEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, stored_at, ttl, etag, last_modified, meta FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        payload, stored_at, ttl, etag, last_modified, meta = row
        return DiskEntry(
            value=json.loads(payload),
            stored_at=float(stored_at),
            ttl=float(ttl),
            etag=etag,
            last_modified=last_modified,
            meta=json.loads(meta or "{}"),
        )

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float,
        etag: str | None = None,
        last_modified: str | None = None,
        stored_at: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, payload, stored_at, ttl, etag, last_modified, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    json.dumps(value, separators=(",", ":")),
                    stored_at if stored_at is not None else time.time(),
                    ttl,
                    etag,
                    last_modified,
                    json.dumps(meta or {}, separators=(",", ":")),
                ),
            )
            self._conn.commit()

    def touch(self, key: str, *, stored_at: float | None = None) -> None:
        """Refresh ``stored_at`` after a 304 Not Modified revalidation."""
        with self._lock:
            self._conn.execute(
                "UPDATE cache SET stored_at = ? WHERE key = ?",
                (stored_at if stored_at is not None else time.time(), key),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM cache WHERE key LIKE ? ORDER BY key", (prefix + "%",)
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
