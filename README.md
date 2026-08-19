# maxpreps-broadcast

Resilient MaxPreps data for live school broadcasts. Built for the situation where it's Friday night, the scorebug is on air, and "the site is slow" is not an acceptable answer.

```bash
pip install -e .
maxpreps init                 # pick your school once
maxpreps sports season fall   # cover the whole fall slate, not just football
maxpreps schedule --byes      # season with week numbers and bye rows
maxpreps live                 # current scorebug state
maxpreps export               # live/schedule/roster per sport + live.mgjson (+csv/xml)
maxpreps serve --watch        # local API + continuous atomic file rewrites for AE
maxpreps doctor               # run this the morning of a broadcast
```

## Multiple sports

A station covers a season, not a sport. Pick up to **six** active sports and every
command fans out across them; `--sport` narrows to one.

```bash
maxpreps sports season winter          # the winter preset, capped at six
maxpreps sports list --all             # the whole catalogue, active ones marked
maxpreps sports set football "Girls Volleyball" boys-water-polo
maxpreps sports add girls-flag-football
maxpreps schedule --sport girls-soccer
```

Exports are suffixed per sport into one flat directory, so a Drive-synced
production machine sees them side by side and After Effects paths stay stable:

```
broadcast-data/
  sports.json                  # what exported, and the filenames — the rotation list
  live.football.json           schedule.football.json        roster.football.json
  live.girls-volleyball.json   schedule.girls-volleyball.json ...
```

One sport failing never costs you the others: it is reported and skipped, and the
rest still export. Out-of-season sports simply have no contests published yet.

Selection is plain data, so a UI can drive it: `GET /sports` returns the catalogue
grouped by season (plus presets, the cap, and what is active), and
`PUT /sports/active` replaces the selection — rejecting unknown names or an
over-long list without changing state.

## What it is

A typed Python client for MaxPreps' server-rendered data (no private API keys, robots-respecting, heavily cached), a broadcast-shaped export layer for After Effects, and a small local service with SSE score updates. Python API in sync and async flavors:

```python
from maxpreps_broadcast.sync import get_scoretracker, get_team_schedule

resp = get_scoretracker()           # live game, else last final, else next game
print(resp.data.home_score, resp.source_tier, resp.cache_state, resp.data_age_seconds)
```

Every response is an envelope carrying `source_tier` (`json_api` / `hydration` / `html`), `cache_state` (`fresh` / `stale` / `last_known_good`), `data_age_seconds`, and structured parse warnings — so a graphics operator always knows whether they're looking at live data or yesterday's snapshot.

## Resilience model

Three fetch tiers (Next.js data route → `__NEXT_DATA__` hydration → HTML tables), stale-while-revalidate caching (memory → SQLite → last-known-good snapshots), retries with jitter, per-host circuit breakers, a token-bucket rate limit, and conditional GETs. Network dies mid-game → the last known good state is served, flagged, and the files on disk stay complete (all writes are atomic temp+rename). `--offline` never touches the network at all.

## After Effects

`broadcast-data/live.json` is flat (one nesting level), pre-formatted (`"FRI AUG 21 · 7:00 PM"`, `"NW 21 — IRV 14"`, `"1ST & 10"`), char-budgeted, and ships WCAG-picked contrast text plus colors in hex/0-255/0-1 forms. `live.mgjson` imports directly as an AE data footage item, with score history as hold-interpolated streams in watch mode. See `docs/AFTER_EFFECTS.md`.

## Docs

- `docs/ENDPOINTS.md` — reconnaissance findings, data provenance, confidence levels, honest limitations
- `docs/AFTER_EFFECTS.md` — wiring exports into AE (JSON, mgJSON, templates)
- `docs/RUNBOOK.md` — broadcast-day checklist and mid-game failure playbook

## Development

```bash
make install   # editable + dev deps
make test      # 239 tests
make lint      # ruff (clean)
make typecheck # mypy --strict (clean)
```

Schema drift from MaxPreps is expected eventually: parsers run lenient by default (warn + preserve unknowns in `raw_extra`), and `--strict` turns drift into hard errors for CI. `maxpreps doctor` checks live shapes against expectations.

## Respect

Public data only, honest User-Agent, robots.txt enforced fail-closed, 1 req/s default with backoff. This is a fan/booster tool for school broadcasts, not a scraping firehose. Positional key lists derived from the MIT-licensed `chrischall/maxpreps-mcp` project (attributed in `parsers/keys.py`).
