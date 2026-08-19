"""/healthz introspection: cache freshness, breaker states, robots status,
and per-surface data ages — the pre-broadcast go/no-go read."""

from __future__ import annotations

import time
from typing import Any

from maxpreps_broadcast.client import MaxPrepsClient


def build_health(client: MaxPrepsClient, *, watch: dict[str, Any] | None = None) -> dict[str, Any]:
    surfaces: dict[str, Any] = {}
    for key in client.disk.keys():  # noqa: SIM118 — DiskCache is not a dict
        if key.startswith("lkg:"):
            continue
        entry = client.disk.get(key)
        if entry is None:
            continue
        age = round(entry.age(time.time()), 1)
        surfaces[key] = {
            "age_seconds": age,
            "fresh": entry.is_fresh(time.time()),
            "tier": entry.meta.get("source_tier", "?"),
        }
    snapshots = client.snapshots.keys()
    status = "ok"
    breakers = client.transport.breakers.snapshot()
    if any(state != "closed" for state in breakers.values()):
        status = "degraded"
    return {
        "status": status,
        "offline": client.settings.offline,
        "breakers": breakers,
        "robots": client.transport.robots.snapshot(),
        "surfaces": surfaces,
        "last_known_good_keys": len(snapshots),
        "watch": watch or {"enabled": False},
    }
