# Operational Reality Check — Forecast Blind Spots

## TL;DR
Method's "forecast" is a thin pass-through of helixo-2's `daily_forecasts.ai_suggested_revenue` (`toast-etl/forecast_engine.py` lines 153-177). Every operational driver an actual restaurant/hotel operator uses — Tripleseat private events, Resy on-the-books reservations, Mews occupancy, weather, local events, promos, menu/price changes — is invisible to it. Empirically the model is 30-160% off on individual days at LSBR, and at Vessel (private event venue) it forecasts $156-$345/day on dates Tripleseat shows as $14k-$23k weddings already booked. Method owns every input that would close that gap; none of them are wired.

## Drivers the forecast should incorporate but doesn't (ranked by impact)

### 1. Tripleseat private events (HIGH)
Vessel forecasts $271 for 2025-11-15 — Tripleseat shows two booked events totaling **$14,500**. 2025-12-14: forecast $156, booked $23,365. Across LSBR, **52 of 56** forecast-window days have Tripleseat events that helixo-2 never saw; some of the largest Resy-window misses (LSBR 2026-04-26: actual $37k vs forecast $15.9k = **+133%**) line up with 6-event days. Method already pulls this data via `toast-etl/tripleseat_sync.py` and writes it to `data/<outlet>.events.events[]`. **Wiring:** join `events.events[]` (booked_revenue + guest_count keyed by event_start date) onto each forecast row pre-display, and surface a delta. Estimated correction at vessel/anthology: 50-90% of forecast variance.

### 2. Hotel occupancy / ADR linkage (HIGH for hotel restaurants)
Mulherins, Le Suprême, Lowland, Rosemary Rose, Quoin Rooftop, Hiroki-San Detroit and Bar Rotunda are all hotel restaurants whose covers are mechanically driven by in-house guests. **None of the eleven outlet JSONs contain a `mews` or `occupancy` block.** Method runs Mews across all 10 hotels and the data is already used elsewhere in BI Analytics / Lighthouse. **Wiring:** add a `pms.daily` block per outlet (occupancy %, ADR, in-house guests, arrivals) and feed it as a feature into the local override layer. Without it the AI is essentially forecasting blind to the strongest leading indicator the property has.

### 3. Resy on-the-books pace (HIGH)
LSBR has 1,903 Resy surveys + reservations going back a year (`data/lsbr.json` `guest.surveys`) and Quoin has live Resy data via `resy_os_scraper`. Forward bookings 0-60 days out are a near-deterministic floor for cover counts. The forecast file contains zero forward-pace data, only post-meal surveys. **Wiring:** extend `toast-etl/resy_sync.py` (or the helixo OS scraper) to capture upcoming `Booked` reservations grouped by `arrival_date`, store as `guest.pace.daily[]`, and use it as a min-bound on `forecast.daily.guests`.

### 4. Weather (HIGH for rooftop/patio, MED elsewhere)
Quoin Rooftop and Kampers Bar (Detroit rooftop) are weather-binary. A 50°F rainy Saturday is a closure-grade cover event; today's forecast for those outlets is a flat seasonal curve with **0.95 confidence** even on dates 4-8 weeks out. There is no weather signal anywhere in the codebase (grep of `forecast_engine.py` confirms: no "weather", "rain", "temp"). **Wiring:** NWS or OpenWeather API → `weather.daily[]` block keyed by zip → feature into local re-rank. Day-of forecast adjustment is the easy win; 7-day window is the bigger one.

### 5. Local events (MED-HIGH, varies by city)
Detroit Tigers home games are 81 dates literally next door to Book Tower (LSBR/Hiroki-Det/Kampers/Anthology). Conventions, marathons, concerts at The Fillmore — all material. Method has no event feed integration; `forecast_engine.py` doesn't even acknowledge the concept. **Wiring:** PredictHQ or a curated city-event feed → `events.local_calendar.daily[]`. Highest leverage at Detroit (4 outlets share one event surface) and Quoin Wilmington (small market, every event matters).

### 6. Day-of-week / STLY seasonality (MED — assumed but unverifiable)
helixo-2 presumably handles DOW internally, but the forecast values prove gaps: LSBR confidence is `0.93` on every Saturday in March-April, yet Saturdays missed by **+76%, +79%, +121%, +133%, +162%, +86%**. Either DOW seasonality is broken or the model is generic across outlets. **Wiring:** Method should compute its own STLY (same-DOW-last-year + holiday-shift) baseline from `order_details.daily` and cross-check against helixo-2's number. When they diverge >25%, log a flag.

### 7. Tripleseat lead-time / pace curve (MED)
Beyond presence, *when* events were booked tells you about confidence. `events.events[].lead_time_days` already exists (computed in `tripleseat_sync.py` line 563). High-lead-time tentatives are softer than 30-day-out definites. Currently unused.

### 8. Menu / price changes, promo pushes (MED)
Klaviyo campaigns, Resy specials, menu repricing. Method has Klaviyo, Method runs promos. None of this enters the model. **Wiring:** simple operator-entered `interventions.daily[]` block (date, type, expected lift) + `marginedge` price-change events.

### 9. Operating-hours / closure changes (LOW-MED)
Holiday closures, brunch added/removed, event-takeover days. The dashboard reads `config.open_hours_per_week` but the forecast doesn't.

### 10. New-outlet cold start (HIGH where it bites)
Per the prior audit, hiroki_det / lsbr / quoin had 0 actuals overlap. The newer Detroit openings have <12 months of own-history. helixo-2's black box is unlikely to handle this gracefully. **Wiring:** comparable-outlet anchoring (LSBR cold-start anchors to Le Suprême Philly equivalent ramp curve + Mulherins DOW shape).

## Outlet-archetype mismatch
One model demonstrably does not fit all archetypes:
- **Coffee shop (little_wing):** 9 unique forecast values across 28 days — essentially a step function. Coffee shops are weather + occupancy + commuter-driven; this looks like a templated curve, not a fitted model.
- **Private event venue (vessel):** flat $156-$345/day on dates with $14k-$23k Tripleseat bookings. The model is forecasting walk-in coffee-shop volumes for a venue whose entire revenue is pre-booked. Catastrophic model-archetype mismatch.
- **Rooftop bar (quoin):** confidence 0.95 with no weather input — false precision.
- **FSR (mulherins, lsbr, lowland):** the only archetype the AI is even plausibly designed for, and even here LSBR shows ±50-160% daily error on Saturdays.

## Critical findings (severity-ranked)
1. **CRITICAL — Vessel forecast is structurally wrong.** Off by 50-100x on event days. This single outlet's forecast should be replaced today with a Tripleseat-driven booked-revenue projection.
2. **CRITICAL — No Mews/PMS occupancy data anywhere.** Hotel restaurants are forecasting without their #1 input.
3. **HIGH — Confidence values are not informative.** 0.93-0.95 on days with ±100% error means the AI's self-confidence is uncorrelated with actual accuracy and shouldn't be displayed without a recalibration layer.
4. **HIGH — Tripleseat data is collected, partitioned, written to disk, and never joined to the forecast.** This is a dashboard-layer fix that could ship in a day.
5. **MED — No weather feed.** Easy public API, real lift on rooftop/patio.

## Recommendations — phased

### Quick wins (next sprint, ≤2 weeks)
- **Tripleseat overlay:** post-process `forecast.daily` to add `events_booked_revenue` + `events_booked_guests` per date from existing `events.events[]`. Display as "Forecast + Booked Events" in the dashboard. Vessel especially.
- **Mews `pms.daily` block:** new sync module mirroring `budget_sync.py`. Just occupancy + in-house guests + ADR. Don't model anything — just expose it on the cards next to the forecast.
- **Confidence sanity column:** for every forecast day with an actual, compute realized error and store rolling 14-day MAE per outlet. Render the AI confidence and the empirical MAE side by side.

### Medium-term (next quarter)
- **Method override layer:** keep helixo-2's `ai_suggested_revenue` as the prior, but add a Method-controlled local model that ingests `pms.daily`, `events.events`, weather, Resy pace and produces `forecast.method_adjusted`. Dashboard shows both.
- **Resy forward-pace ingest:** extend the OS scraper or build a Resy API path (per memory `resy_auth_limitation.md`, OS-operator-creds path is non-functional; need API route).
- **Weather feed:** zip → daily forecast → outlet, NWS preferred (free, public).

### Strategic (12 months — should we even use a black box?)
- The helixo-2 AI is a vendor-style dependency we can't introspect, can't tune, and can't blame when it's wrong. Method has every input — POS, PMS, Tripleseat, Resy, MarginEdge — already in-house. **Recommendation:** treat helixo-2 as one signal among several, not THE forecast. Build a Method-owned ensemble (STLY baseline + Tripleseat + Mews + weather + helixo-2 prior) and benchmark it monthly against the black box. If Method's ensemble beats helixo-2 by >15% MAE for two consecutive quarters — and it almost certainly will at Vessel and the rooftops — phase helixo-2 out as a primary and keep it as a sanity-check signal.

---

**One-paragraph summary (≤150 words):**
The single most operationally consequential blind spot is Tripleseat private-event ingestion. Method already pulls every booked event into `data/<outlet>.events.events[]` via `toast-etl/tripleseat_sync.py`, but `forecast_engine.py` never joins them — so Vessel (a venue whose revenue is *literally* pre-sold weddings) forecasts $156-$345 per day on dates Tripleseat shows as $14,500-$23,365 in confirmed bookings, and LSBR misses Saturdays by +76% to +162%. This is a dashboard-layer overlay, not a modeling problem: every dollar of correction sits in a JSON file we already write nightly. A two-day fix that surfaces "Forecast + Booked Events" alongside the AI number would close the largest single source of forecast error in the portfolio and would have caught Vessel's structural model-archetype mismatch the day the dashboard launched.
