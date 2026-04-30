# Forecast Audit — Executive Summary

**Generated:** 2026-04-30 overnight, by Method Co dashboard team (Ross Richardson, EVP Finance & Accounting).

**Scope:** stress-test the forecast layer that drives the dashboard's "vs Forecast" KPI cards across 11 F&B outlets. Source: helixo-2's proprietary AI engine (Supabase `daily_forecasts.ai_suggested_revenue` / `ai_suggested_covers`), passed through unmodified by `toast-etl/forecast_engine.py`.

---

## TL;DR — three findings, ranked by Ross-impact

### 🔴 P0 · helixo-2 does not beat a naive baseline (statistical critique)

Pooled across 246 day-outlet pairs of overlap, helixo-2's WAPE is **34.8%** vs the same-DOW-prior-week naive baseline at **35.2%** — a statistical tie. Three of eight outlets with overlap (`hiroki_phl`, `mulherins`, `rosemary_rose`) lose outright to naive. `mulherins` shows lag-7 residual autocorrelation of 0.72 — a textbook SARIMA would absorb most of its error. `ai_confidence` is anti-correlated with accuracy at `hiroki_phl` (high-confidence days are the worst-error days).

**Translation for ops:** the AI you're staffing against is no better than "look up what last Tuesday did."

**Fix:** require helixo-2 to beat naive by ≥3pp WAPE before its number drives any staffing or budget decision. Add a `BEATS NAIVE?` column to every "vs Forecast" comparison. (See `docs/forecast-audit/02-statistical-critique.md`.)

### 🔴 P0 · Vessel is serving a 483-day stale forecast from a deprecated local-port algorithm (methodology audit)

`forecast_engine.py:304` iterates only outlets that received ≥1 helixo-2 row. Vessel has no UUID map entry and no fuzzy name match, so it never gets touched by the nightly sync. Four other outlets (`hiroki_phl`, `little_wing`, `mulherins`, `quoin`) currently route via undocumented fuzzy substring matching rather than the locked 6-UUID map — a silent data-correctness risk if helixo-2 renames a location.

**Translation:** operators viewing the Vessel dashboard right now are seeing "vs Forecast" deltas computed against a `weighted_ensemble` model from October 2025.

**Fix:** lock the UUID map, log unmapped outlets as errors (not silent skips), and write an empty-state forecast block for outlets helixo-2 doesn't cover. (See `docs/forecast-audit/01-methodology-audit.md`.)

### 🔴 P0 · The single largest correctable error sits in Tripleseat data we already pull (operational reality)

`forecast_engine.py` is a pure pass-through with **zero** references to weather, occupancy, ADR, Mews, holiday, Resy, reservation, marketing, promo, menu, price, or even DOW logic — it just writes whatever helixo-2 returned.

Concrete cost:
- **Vessel** forecasts $156-$345/day on dates with $14,500-$23,365 in already-booked Tripleseat events (2025-11-15, 2025-12-14, 2026-01-24, etc.). Two orders of magnitude wrong.
- **LSBR** Saturdays miss by +76% / +79% / +121% / +133% / +162% / +86% with `ai_confidence` flat at 0.93-0.95.
- Across 56 LSBR forecast-window days, **52** had Tripleseat events helixo-2 never saw — even though `toast-etl/tripleseat_sync.py` writes them to the same JSON file.
- **No outlet JSON contains a Mews/PMS/occupancy block** — hotel restaurants forecast without their #1 leading indicator.

**Fix:** dashboard-layer overlay that surfaces "AI forecast + Booked Events" alongside the helixo-2 number. Two-day build. Closes the largest single source of forecast error in the portfolio. (See `docs/forecast-audit/03-operational-reality.md`.)

---

## Phased recommendations

| Phase | Effort | Action |
|---|---|---|
| **Phase 1 — already shipped** | done | `Forecast Accuracy` nav tab tracks WAPE, bias, naive-baseline delta, ai_confidence calibration, and worst-error days per outlet. Operators can now SEE the gap. |
| **Phase 2 — this sprint** | days | Lock UUID map. Backfill Vessel with proper empty-state. Add Tripleseat-event overlay to forecast comparisons. |
| **Phase 3 — next quarter** | weeks | Wire Mews ADR/occupancy + Resy forward-book + holiday calendar + weather as features. Run an open-source baseline (Prophet / AutoARIMA) in parallel and ensemble with helixo-2 where it loses. |
| **Phase 4 — strategic** | quarter+ | Decide whether helixo-2's vendor-lock + opacity is acceptable given Phase 1's evidence that it doesn't beat naive. (See `docs/forecast-audit/04-industry-benchmark.md` for industry comparables.) |

---

## How this audit was generated

Four parallel critic agents, each given a single dimension:
1. **Methodology auditor** — read every line of forecast code + outlet JSONs
2. **Statistical critic** — computed WAPE, bias, naive baseline, lag-7 autocorr, ai_confidence calibration empirically
3. **Operational realist** — checked forecast against restaurant ops drivers (events, weather, occupancy, reservations)
4. **Industry benchmark researcher** — placed helixo-2 against 5-out, BlackBox, Restaurant365, Toast Forecasting, etc.

Reports are in this directory; this summary is the consolidated head.
