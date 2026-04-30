# Methodology Audit — Forecast Engine

## Executive summary
Method's "forecast" is a one-way pass-through of helixo-2's `daily_forecasts.ai_suggested_revenue`. Method computes nothing, validates nothing, and quality-gates nothing. The pipeline (1) silently leaks a stale local-port forecast for `vessel` (483 days of `forecast_engine_v1` data still serving the KPI cards), (2) drops AI confidence on the floor — captured but never displayed or used to gate stale rows, (3) relies on fuzzy substring name-matching to route 5 of 11 outlets to helixo-2 location rows that the documented UUID map never claimed. Operators can't tell the difference between a real AI forecast, a stale port from October 2025, and a name-collision routing accident.

## Data flow
1. `13:00 UTC` — `.github/workflows/forecast-sync.yml` cron triggers `forecast_engine.py`.
2. `forecast_engine.py:163-177` pages all `daily_forecasts` rows from helixo-2 Supabase (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`).
3. `:272-293` filters out `ai_suggested_revenue IS NULL` rows, then routes each row to a Method outlet via `resolve_uuid_to_outlet`: hardcoded UUID map (6 outlets) or fuzzy name-substring match against `KNOWN_OUTLETS`.
4. Surviving rows are grouped by outlet and written to `data/<outlet>.json` under `forecast.daily` (`:319-327`).
5. The dashboard loader at `Method_Co_FB_Performance_Dashboard.html:5426-5429` copies `payload.forecast` into the runtime `DATA.outlets[<id>]`.
6. `periodCompareSums` (`:862-880`) and `tripleKpiDelta` (`:885-905`) sum forecast rows in the active date window and render the "vs Forecast" delta on every KPI card.

## Critical findings

### 🔴 1. Stale `vessel` forecast is live in production
`data/vessel.json` carries 483 days (2025-03-29 → 2026-08-28) under `source: "forecast_engine_v1 (helixo-2 port)"` with `_meta.model: "weighted_ensemble"` — the local port that PR #62 (commit 17c9949) was supposed to retire. The current sync at `forecast_engine.py:304` iterates **only `by_outlet.keys()`** (outlets that received ≥1 helixo-2 row); vessel is never iterated, so the empty-list branch at `:308-318` never fires for it. Today's run (commit 5a1ca09, 2026-04-30T19:46Z) touched 10 outlets — vessel is conspicuously absent from the diff. Operators looking at the Vessel dashboard see "vs Forecast" deltas computed against a deprecated local algorithm that was never re-blessed.

### 🔴 2. Fuzzy name-matching routes forecasts to outlets that aren't on the UUID map
The documented mapping is 6 UUIDs → 6 outlets (`forecast_engine.py:66-73`). Empirically (`data/*.json`) all of `hiroki_phl` (35d), `little_wing` (28d), `mulherins` (42d), `quoin` (28d) carry full helixo-2 forecasts. They got there via `name_to_outlet` (`:102-114`) — substring match against `KNOWN_OUTLETS`. There is no proof which helixo-2 location each fuzzy match is pulling from; the matcher is bidirectional substring (`if norm(outlet) in norm(name) or vice-versa`) so e.g. "Hiroki" alone routes to `hiroki_phl` but a Detroit-named record could collide. `--print-config` would show the routing but isn't run nightly. **Risk: silent cross-property data contamination.**

### 🟠 3. `ai_confidence` is captured but never used
`forecast_engine.py:292` writes `ai_confidence` to every row (range 0.45–0.95 across 305 rows; 129 rows at 0.95 and 40 at 0.80 — clustered, smells templated). `Method_Co_FB_Performance_Dashboard.html` has zero references to `confidence` — no display, no gating, no styling. A 0.45-confidence forecast renders identically to a 0.95-confidence forecast. Operators get no signal that the AI is uncertain.

### 🟠 4. No `as_of` freshness check on read
The dashboard merges `payload.forecast` whenever `daily.length > 0` (`:5426`). It never checks `forecast.as_of` against `today`, never warns when the file is days stale. If forecast-sync.yml fails for a week, the cards keep cheerfully comparing to a week-old prediction.

### 🟠 5. `is_override = true` rows are pulled and treated as AI
The probe at `forecast_engine.py:218` selects `is_override`, but the sync query (`:166-170`) does not. Any row where helixo-2's manager edited the AI suggestion **but the AI value was filled in first** flows straight through as "AI forecast" — there's no `is_override=false` filter. The header comment promises "AI prediction, not human override" but the code doesn't enforce it.

### 🟡 6. Manager-zeroed days survive
`anthology.json` has 1 row with zero/null `net_sales` despite the `IS NULL` filter at `:273` — the filter only catches `None`, not `0.0`. A helixo-2 zero-revenue day (closure, AI suppressed) becomes a comparator that drives every KPI delta to `-100%`.

### 🟡 7. No backfill bound
The pull is unbounded — every row in `daily_forecasts` is fetched and written every night. There's no `business_date >= today - N` filter. Today's run rewrote 62k lines of JSON. Cheap now; not at scale.

## Unmapped outlets — empirical reality
| Outlet | UUID-mapped? | `forecast.daily` days | Source string |
|---|---|---|---|
| anthology, hiroki_det, kampers, lowland, lsbr, rosemary_rose | yes | 49–56 | `helixo2_daily_forecasts.ai_suggested_revenue` |
| hiroki_phl, little_wing, mulherins, quoin | **no — fuzzy name match** | 28–42 | `helixo2_daily_forecasts.ai_suggested_revenue` |
| vessel | **no — no match at all** | 483 | `forecast_engine_v1 (helixo-2 port)` ← **stale** |

## Hidden helixo-2 assumptions
- That `daily_forecasts.location_id` UUIDs Method uses match the ones in helixo-2's `locations` (we don't own that table).
- That `ai_confidence` is calibrated identically across outlets and over time (the 0.95-heavy distribution suggests defaults, not calibrated probabilities).
- That helixo-2 writes a row for every business_date — gaps render as zero contribution to "vs Forecast" sums (the `filter` helper drops missing dates silently).
- That "Vessel" and "Little Wing" exist as separate helixo-2 locations (they may share the ROOST Baltimore ME entity — see MEMORY).

## Recommendations
1. **Strip vessel's stale forecast block now**, then add a guard at `forecast_engine.py:304`: iterate `KNOWN_OUTLETS`, not `by_outlet.keys()`, so unmapped outlets get an explicit empty-state write every night.
2. **Replace fuzzy `name_to_outlet` with explicit UUID mappings.** Run `--print-config`, capture every UUID in use, lock the mapping, error-out (don't fuzzy-match) on unknown UUIDs.
3. **Filter the sync query**: add `is_override.eq.false` and `ai_suggested_revenue.gt.0` to the PostgREST call at `:166-170`.
4. **Surface `ai_confidence`** on KPI cards (e.g. faded chip when avg confidence < 0.70 in the window) and stamp it in the tri-comparison header.
5. **Stamp `forecast.as_of` on the dashboard** with a "stale > 36h" warning banner.
6. **Bound the pull** to `business_date >= today - 14` and `<= today + 90` to keep diffs small and predictable.
7. **Add a CI assertion**: every outlet's `forecast.source` must equal `helixo2_daily_forecasts.ai_suggested_revenue` after sync — fail the workflow if any file shows the legacy `forecast_engine_v1` string.
