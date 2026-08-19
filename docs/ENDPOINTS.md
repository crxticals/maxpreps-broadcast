# ENDPOINTS.md — Reconnaissance findings and data provenance

Captured 2026-08-18 against maxpreps.com (Next.js server-rendered surface). Everything here was verified live unless marked otherwise; confidence levels are stated per item so future drift can be triaged honestly.

## The three tiers

**Tier 1 — `GET /_next/data/{buildId}/{path}.json`** *(confidence: high, live-verified)*
Next.js data routes return the same `pageProps` the page hydrates with, as clean JSON. `buildId` is scraped from the homepage's `__NEXT_DATA__` blob (regex handles a trailing `\n` embedded in the string — observed in the wild), cached 6h, and self-healed once on a 404 (a stale buildId after a MaxPreps deploy is the expected failure). Observed buildId during recon: `1785513693`.

**Tier 2 — page HTML → `__NEXT_DATA__`** *(confidence: high)*
Identical payload, more bytes. Used automatically when tier 1 fails for reasons other than a stale buildId.

**Tier 3 — rendered HTML tables** *(confidence: medium)*
Schedule and roster only. Heuristic bs4 parsing; every selector miss emits an `html_empty` warning rather than silently returning nothing. This is the abandon-ship tier and its output is visibly marked `source_tier: html`.

## Wire formats

Two shapes coexist:

- **"gems" shape** — `data.team` with named fields. The spec-canonical schedule format.
- **"wire" positional shape** — `pageProps.contests` / roster rows as positional arrays, deserialized against ordered key lists: **37 roster / 41 contest / 32 team** keys, vendored in `parsers/keys.py` from the MIT-licensed `chrischall/maxpreps-mcp` project (captured 2026-08-01, buildId above). The team list contains load-bearing duplicate keys — later positions overwrite earlier ones, and the deserializer preserves that order deliberately. **If arity drifts** (row length ≠ key count) the parser warns (`arity_drift`) in lenient mode and raises in strict; re-derive the lists by diffing a fresh `__NEXT_DATA__` capture against the current ones.

Verified wire semantics *(confidence: high, cross-checked against rendered pages)*:

| field | meaning |
|---|---|
| `homeAwayType` | `0` = home, `1` = away — the **sole** venue truth; slot order in the payload lies |
| `contestState` | `1` = scheduled, `4` = final |
| `calcResult` | `2` = win, `3` = loss (used when `resultString` is absent) |
| `isDeleted` | soft-deleted rows are present in payloads and must be filtered |

## Sport URL grammar *(confidence: high, live-verified 2026-08-18 across three schools)*

```
{st}/{city}/{school}/{slug}[/{gender}][/{season}]/{tab}
```

Encoded in `sports.py`; three findings drive that table and none are guessable:

- **Slugs are MaxPreps' own.** Scraped from school nav markup. Several defeat the obvious guess: `track-field` (not `track-and-field`), `water-polo`, `flag-football`, `cross-country`.
- **One gender is implicit per sport, and it is not always the boys.** `basketball` is the boys' team, `basketball/girls` the girls'. **Volleyball inverts it**: `volleyball` is *girls* volleyball (166KB payload, `formattedSportSeasonName: "Volleyball"`) and boys are explicit at `volleyball/boys` (43KB, `"Boys Volleyball"`). Emitting the non-default gender is the canonical form; emitting the default one 308-redirects back and returns byte-identical tier-1 payloads either way.
- **The season segment is load-bearing for some sports and redundant for others.** `soccer/schedule` is a **404** while `soccer/winter/schedule` resolves; `wrestling/winter/schedule` 308-redirects back to `wrestling/schedule`. Tier 1 returns identical bytes with or without it. The client therefore **always emits it** — safe where redundant, required where not.
- **The season *year* is a query param, not a path segment.** `soccer/25-26/schedule` 404s; use `?year=YY-YY`.
- **A 404 does not mean "wrong slug".** It equally means the school does not field that team, or the season is not published yet. `water-polo/girls` 404s at two of the three probe schools and 200s at the third. Out-of-season sports return **200 with zero contests** — an expected state, not an outage.

## Surfaces and confidence

| surface | path shape | confidence |
|---|---|---|
| schedule | `{st}/{city}/{school}/{slug}[/{gender}]/{season}/schedule` (+`?year=YY-YY`) | high — live-verified vs Northwood 26-27 incl. the 9/25 bye |
| roster | `.../roster` | high — real capture in fixtures |
| search | `search?q=` (`initialSchoolResults`) | high — real capture |
| standings | `.../standings` (`standingsData.standingSections`) | medium-high |
| state rankings | `{st}/{sport}/{season}/rankings/{page}` | medium-high — real capture with movement fields |
| league/division rankings | contexts on the *team* rankings tab | **assumed shape** — parsed defensively; on miss, falls back to state rankings with a `rankings_scope_fallback` warning |
| athlete profile | `local/player/stats.aspx?careerid=` | **partially assumed** — known field names probed first, then a conservative table-walk; if nothing is recognized the profile carries identity only plus `athlete_stats_unrecognized` |

## Honest limitations (by design, not omission)

- **No play-by-play, game clock, possession, or down/distance** on the server-rendered surface. These fields exist in the models and are honest `None`s — the scorebug shows period + scores, never invented state. A future client-side scoretracker API integration could fill them.
- **No contest-by-id endpoint.** `get_scoretracker_by_id` searches the hinted/primary team's schedule, then fans out across league members' schedules. Pass `team=` to make it fast.
- **robots.txt was not captured during recon** (fetch tooling limitation in the recon environment). The runtime gate is therefore fail-closed: robots is fetched per host with a 12h TTL, a disallow denies the request, and a robots 5xx denies everything on that host until it recovers. If MaxPreps disallows these paths, the tool stops working — that is the intended behavior.
- **mgJSON key spelling**: the schema key is `sampleSetID` (some earlier notes said "sampleSetting" — that is wrong), and the schema's own `hasExpectedFrequecyB` typo is reproduced faithfully because AE expects it.
- **Sandbox note**: mascot downloads from `image.maxpreps.io` returned 403 in the build sandbox due to its egress allowlist — not a MaxPreps behavior. Logo failures never block a render; the path fields are simply empty.

## buildId lifecycle

Homepage scrape → 6h in-memory TTL → on any tier-1 terminal error, force-refresh once and retry → if still failing, fall through to tier 2. MaxPreps deploys invalidate buildIds at unpredictable times; this self-heal path is exercised in tests.
