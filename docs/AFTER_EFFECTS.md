# AFTER_EFFECTS.md — Wiring exports into AE

Everything the exporter writes is designed around two AE realities: expressions are miserable beyond one nesting level, and a half-written file read mid-render is a corrupted frame. So every view is flat, every value is pre-formatted for a text layer, and every write is atomic (a file on disk is always complete — old or new, never partial).

## live.json (footage → expressions)

Import `broadcast-data/live.json` as footage, then on any text layer:

```
// sourceText expression
footage("live.json").sourceData.score_line        // "NW 21 — IRV 14"
footage("live.json").sourceData.clock_display     // "Q3 " / "FINAL" / kickoff string
footage("live.json").sourceData.home_abbr
```

Useful fields (all strings unless noted): `home_abbr / away_abbr / home_score / away_score / score_line / status / status_upper / period_display / clock_display / down_distance_display / kickoff_display / home_name_upper / away_name_upper / is_live (bool) / stale (bool)`.

Colors ship three ways per side — pick whichever your rig wants:

```
footage("live.json").sourceData.home_primary_hex    // "#022C66"
footage("live.json").sourceData.home_primary_r      // 2 (0-255)
footage("live.json").sourceData.home_primary_r01    // 0.0078 (0.0-1.0, for Fill effects)
footage("live.json").sourceData.home_primary_text   // "#FFFFFF" — WCAG-picked text color
```

For a Fill effect color: `[d.home_primary_r01, d.home_primary_g01, d.home_primary_b01, 1]`.

`home_logo_path` / `away_logo_path` point at cached PNGs (GIFs are converted; AE's GIF import is not worth anyone's evening). Empty string when unavailable — never blocks anything.

Reload behavior: AE re-reads footage JSON on demand. With `maxpreps serve --watch` rewriting the file every few seconds, either use a scripted refresh or re-render per update; the atomic writes guarantee the read is always a complete document.

## live.mgjson (File → Import, data footage)

`live.mgjson` imports as MGJSON data footage. Statics mirror live.json; in watch mode the score history becomes dynamic streams (`home_score_t`, `away_score_t`, `period_t`) with **hold** interpolation — a score is a step function, and linear interpolation would animate 14→21 through a nonsense 17.5. Every file is structurally validated before it's written; an invalid document raises instead of landing on disk.

## schedule.json / roster.json

`schedule.json.games[]` rows are flat: `week / date_display / opponent_upper / home_away / vs_at / result / score_us / score_them / record_before / is_league / is_live`. `roster.json.players[]`: `jersey_padded / lower_third_name ("C. PORTIS") / full_upper / positions / grade / height / weight / is_captain`. Both also exist as CSV/TSV for spreadsheet-style imports and XML for pt-style rigs.

## Char budgets

Configured in `[export.char_budgets]` (config.toml): `score_line 18, home_abbr/away_abbr 4, kickoff_display 22, lower_third_name 18, ...`. Truncation is word-boundary with an ellipsis — set the budget to your template's actual text-box capacity and long school names stop escaping their boxes. Per-opponent fixes: `maxpreps override "Orange Lutheran" --abbr OLU --display "O. Lutheran"`.

## Template mappings (`maxpreps render`)

`templates/{name}.mapping.yaml` maps AE layer names → exported field paths, so re-wiring a template is a YAML edit, not Python:

```yaml
template: scorebug
source: live          # live | schedule | roster
layers:
  "HOME SCORE": home_score
  "SCORE LINE": score_line
  "OPP W3":     games.3.opponent_upper   # dotted paths reach into lists
defaults:
  "DOWN DISTANCE": ""
```

`maxpreps render --template scorebug` writes `scorebug.render.json`: `{"layers": {"HOME SCORE": "21", ...}, "missing_fields": [...]}` — one value per layer, ready for an ExtendScript/UXP ingest loop or a human with a checklist. A shipped `templates/scorebug.mapping.yaml` covers the standard 20-layer bug.

## The meta block

Every view carries `fetched_at / source_tier / cache_state / data_age_seconds / stale / warning_count`. Put `stale` somewhere an operator can see it (even a tiny off-air indicator): it is the difference between "live score" and "score as of when the Wi-Fi died".
