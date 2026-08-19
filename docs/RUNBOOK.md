# RUNBOOK.md — Broadcast day

## Morning of (T-8h)

```bash
maxpreps doctor
```

Six live checks (buildId, schedule, roster, scoretracker, rankings, search) against expected shapes. All ✓ → proceed. Any ✗ → you have hours, not minutes: read the error, check if maxpreps.com itself is up, re-run. `SchemaDriftError` means MaxPreps shipped a change — run `maxpreps --strict schedule` to see exactly which fields moved, and check `raw_extra` on parsed objects for where the data went.

Then warm every cache and produce first files:

```bash
maxpreps export
maxpreps schedule --byes    # eyeball: week numbers right? bye where expected? kickoff times sane?
maxpreps roster             # eyeball: count right? no ghosts? captains marked?
```

The eyeball step matters: `doctor` verifies shapes, you verify truth.

## Pre-game (T-1h)

```bash
maxpreps serve --watch --interval 5 --out ./broadcast-data
curl -s localhost:8787/healthz | python3 -m json.tool
```

Go/no-go from `/healthz`: `status: ok`, breakers all `closed`, surfaces `fresh: true`, `last_known_good_keys > 0` (that's your parachute). Confirm `broadcast-data/live.json` timestamps advance every interval.

## During the game

Normal operation is silence. The watcher polls, diffs, rewrites atomically, publishes to `/stream`. If something goes wrong upstream, **the last good files remain in place** — a network death mid-Q3 means the bug shows the last known score flagged `stale: true`, not a blank.

| symptom | meaning | action |
|---|---|---|
| `cache_state: stale` in live.json | serving cached while revalidating | none — normal under load |
| `cache_state: last_known_good` | upstream fetch failing, parachute deployed | keep broadcasting; check `/healthz` breakers |
| breaker `open` in /healthz | repeated upstream failures; fast-failing to LKG | wait — it half-opens automatically after cooldown |
| watcher `errors` climbing, files not updating | check `maxpreps stats` and logs | restart `serve --watch`; files stay intact throughout |
| everything down, files frozen | MaxPreps or your uplink is gone | scores from the press box radio; update the bug by hand; the JSON stays valid |

Total network loss ahead of time (venue with no Wi-Fi): warm caches at home, then run everything with `--offline` — it serves caches/LKG and never attempts a request.

## After

`maxpreps export` once for final numbers, then `maxpreps stats` if anything felt slow (latency percentiles per endpoint). Cache and LKG snapshots persist for next week — the first `doctor` run next Friday will be instant.
