# Statistical Critique — Forecast vs Actuals

**Auditor:** Statistical Critic
**Source:** `data/<outlet>.json` (helixo-2 forecast vs Toast actuals)
**Period:** overlap windows ending 2026-04-30 (vessel goes back to 2025-03-29 — see Finding 1)
**As-of date:** 2026-04-30

---

## TL;DR (5 lines)

1. **Pooled WAPE 34.8% vs naive same-DOW-prior-week 35.2% — helixo-2 is statistically tied with a one-line lookup.** (n=246 day-outlet pairs)
2. **3 of 8 outlets with overlap LOSE to the naive baseline** (hiroki_phl, mulherins, rosemary_rose). Two more (anthology, vessel) only "win" because the naive baseline is also catastrophic on tiny n.
3. **`ai_confidence` is partially calibrated** at the pooled level (0.85+ bin = 31% WAPE vs <0.7 bin = 52%) but **inverted within hiroki_phl** (high-confidence days are the worst — 0.85+ bin WAPE = 81%).
4. **Mulherins shows lag-7 residual autocorrelation = 0.72** — the model is missing a textbook weekly seasonality pattern in a flagship outlet. This is fixable in any classical model.
5. **3 outlets (hiroki_det, lsbr, quoin) get forecasts with zero actuals to evaluate against.** We are publishing predictions we cannot back-test.

---

## Per-outlet WAPE table

| Outlet | n | WAPE rev | WAPE cov | Bias (MPE rev) | Naive WAPE rev | Beats naive? |
|---|---:|---:|---:|---:|---:|:---:|
| anthology | 15 | 63.6% | 86.9% | +195.8% | 64.4% (n=6) | yes (barely) |
| hiroki_phl | 27 | 78.7% | 59.6% | +142.6% | 50.9% | **NO** |
| kampers | 39 | 50.3% | 55.8% | +11.3% | 61.7% | yes |
| little_wing | 28 | 36.1% | 26.2% | +3.1% | 100.7% | yes |
| lowland | 39 | **20.0%** | 24.5% | +2.9% | 23.4% | yes |
| mulherins | 39 | 35.4% | 34.5% | +10.7% | **20.9%** | **NO** |
| rosemary_rose | 39 | 38.9% | 35.0% | -3.1% | 37.6% | **NO** |
| vessel | 54* | 79.0% | 64.0% | +146.7% | 99.1% | yes (both bad) |
| hiroki_det | 0 | — | — | — | — | no actuals |
| lsbr | 0 | — | — | — | — | no actuals |
| quoin | 0 | — | — | — | — | no actuals |
| **POOLED** | **246** | **34.8%** | — | — | **35.2%** | **tied** |

*Vessel "overlap" runs 2025-03-29 → 2025-10-05 — these are in-sample, not forward forecasts. See Finding 1.

**Bias**: Positive MPE = systematic over-forecast. Five of eight outlets over-forecast. Anthology, hiroki_phl, and vessel over-forecast revenue by **>140% on average per day** — the model is hallucinating sales that aren't there.

---

## ai_confidence calibration

**Pooled across all 8 outlets:**

| Bin | n days | WAPE rev |
|---|---:|---:|
| 0.50 – 0.70 | 17 | 52.0% |
| 0.70 – 0.85 | 55 | 48.6% |
| 0.85 – 1.00 | 154 | 31.3% |

Directionally calibrated at the pool level. **But the per-outlet picture breaks the story:**

- **hiroki_phl** — high-confidence (0.85+) bin WAPE = **81%**, mid bin = 103%, low bin = 61%. **Confidence is inversely correlated with accuracy** at this outlet. Days flagged 0.95 confidence include the four worst APE days (1172%, 1043%, 558%, 319%). The model is most certain when it is most wrong.
- **kampers** — 35 of 39 days are stamped 0.85+ confidence with WAPE 50%. The score is essentially constant and tells the operator nothing.
- **lowland, little_wing** — 100% of days at 0.85+. No variance, no calibration signal.

**Verdict:** the confidence score is not a usable input to operational decisions. It either has no variance or, in the worst case (hiroki_phl), it points the wrong way.

---

## Worst-error days (top 10, APE > 50%)

| Date | DOW | Outlet | Forecast | Actual | APE | Conf | Hypothesis |
|---|---|---|---:|---:|---:|---:|---|
| 2026-04-29 | Wed | anthology | $25,000 | $1,050 | 2,281% | 0.68 | event center — model assumed booked event that didn't materialize |
| 2025-04-26 | Sat | vessel | $222 | $10 | 2,120% | n/a | private event venue with sparse activity — see Finding 1 |
| 2026-04-10 | Fri | hiroki_phl | $4,354 | $342 | 1,172% | **0.95** | very high "confidence" yet 12x error; model anchored on Friday seasonality without checking covers |
| 2026-04-02 | Thu | hiroki_phl | $1,018 | $89 | 1,043% | **0.95** | same pattern — high conf, near-zero actual, possible closure |
| 2025-10-05 | Sun | vessel | $153 | $16 | 829% | n/a | sparse outlet |
| 2026-04-06 | Mon | hiroki_phl | $2,218 | $337 | 558% | **0.95** | same pattern |
| 2026-04-03 | Fri | anthology | $22,050 | $4,914 | 349% | 0.80 | scheduled event likely cancelled or partial |
| 2026-04-16 | Thu | hiroki_phl | $2,300 | $548 | 319% | 0.75 | recurring Thursday over-forecast |
| 2026-04-07 | Tue | kampers | $1,750 | $453 | 286% | 0.93 | Detroit Tuesday — possible weather/event miss |
| 2026-04-21 | Tue | anthology | $6,300 | $1,875 | 236% | 0.73 | weekday no-event slot |

**Pattern:** worst-day flags by DOW: Sun=23, Fri=19, Thu=17, Sat=14, Tue=12, Mon=9, Wed=4 (n=98 of 280 = **35% of days have APE > 50%**). The model is worst on weekend and Thursday — i.e., the days that drive the P&L. There is no evidence in the data that holidays, weather, or local events are inputs (no exogenous columns visible).

---

## Residual autocorrelation findings

| Outlet | lag-1 | lag-7 | Read |
|---|---:|---:|---|
| anthology | -0.31 | -0.28 | choppy alternation (event vs no-event) |
| hiroki_phl | -0.15 | -0.17 | mean-reverting noise |
| kampers | 0.01 | 0.08 | clean |
| little_wing | 0.03 | -0.13 | clean |
| lowland | 0.03 | **+0.29** | mild weekly leak |
| **mulherins** | 0.05 | **+0.72** | **strong weekly pattern uncaptured** |
| rosemary_rose | 0.00 | +0.17 | mild weekly leak |
| vessel | 0.12 | 0.05 | sparse data |

**Mulherins lag-7 = 0.72** is the headline. A residual autocorrelation that high at lag-7 means a simple weekly seasonal naive (or even adding day-of-week dummies to a regression) would absorb most of the remaining error. Helixo-2 is leaving structured weekly signal on the floor at one of the most important outlets in the portfolio.

---

## Critical findings (severity-ranked)

1. **🔴 Helixo-2 fails to beat a one-line naive baseline at the pooled level (34.8% vs 35.2%) and loses outright at 3 of 8 outlets.** Impact: paying for an opaque AI engine that performs no better than `actual = same DOW last week`. Fix: stand up the naive baseline as the production benchmark; require helixo-2 to beat it by ≥3pp WAPE per outlet before it is used for staffing/PAR decisions.

2. **🔴 Vessel forecast period (2025-03-29 → 2026-08-28) overlaps actuals from 2025-03 through 2025-10 — these are in-sample fits, not forward forecasts.** Impact: any reported vessel WAPE is structurally biased optimistic, and we still see 79% error on in-sample. Fix: only score forward forecasts (forecast `as_of` date < actual date). Trim historical "forecasts" from the dashboard.

3. **🔴 hiroki_phl: ai_confidence is anti-calibrated.** 19 of 27 days are 0.85+ confidence and the WAPE in that bin is 81%, vs 61% in the low-confidence bin. Impact: any operator using the confidence score to decide "trust today's forecast" is making worse decisions than coin flip. Fix: do not surface ai_confidence in the dashboard until calibration is verified per outlet.

4. **🟠 Mulherins lag-7 residual autocorrelation = 0.72.** Impact: model is missing weekly seasonality at our flagship Philly restaurant. Fix: add a DOW-fixed-effect ensemble layer on top of helixo-2 (or replace it with a SARIMA(0,1,1)(0,1,1,7) — these are textbook).

5. **🟠 3 outlets (hiroki_det, lsbr, quoin) have forecasts published with zero actuals to validate.** Impact: ~140 forecast-days are flying blind in the dashboard. Fix: gate forecast publication on the existence of ≥30 days of actuals, OR clearly badge "unvalidated" in the UI.

6. **🟠 Systematic over-forecast bias.** Anthology MPE +196%, hiroki_phl +143%, vessel +147%, kampers +11%, mulherins +11%. Operators staffing to forecast will be over-staffed and over-prepped. Fix: bias-correct forecasts post-hoc using rolling 14-day MPE per outlet.

7. **🟡 35% of overlap days have APE > 50%.** Impact: forecast is unusable for daily operations a third of the time. Fix: same as #1 — require WAPE < 25% per outlet as a publication gate.

8. **🟡 ai_confidence has near-zero variance for many outlets (kampers, lowland, little_wing).** Impact: no operational signal. Fix: ask helixo-2 vendor to expose per-day prediction intervals (e.g., quantile forecasts), not a scalar confidence.

---

## Recommendations

### Metrics this dashboard SHOULD compute (and why)

| Metric | Formula | Why it belongs in the UI |
|---|---|---|
| **WAPE (revenue + covers)** | Σ\|a-f\| / Σa | Single number, scale-free, weights big days appropriately. The default. |
| **MPE (bias)** | mean((f-a)/a) | Tells operators if forecast is systematically high or low — drives prep & staffing decisions. |
| **MASE** | MAE / MAE_naive_DOW | Direct head-to-head with the naive baseline. <1.0 = AI adds value, ≥1.0 = it doesn't. **This is the metric Ross should put on the front page.** |
| **sMAPE** | mean(2\|a-f\|/(\|a\|+\|f\|)) | Bounded 0–200%, robust to small actuals (vessel/anthology have $10-day covers). |
| **APE distribution (median, p75, p95)** | per-day \|a-f\|/a | A WAPE of 35% with p95 of 900% is unusable. Distribution > average. |
| **Residual autocorrelation lag-7** | corr(e_t, e_{t-7}) | Diagnostic — flags when a simpler model would beat helixo-2. |
| **Beats-naive flag (boolean)** | helixo WAPE vs naive WAPE | The lowest bar. If we don't clear it, we're not forecasting — we're guessing. |

### Confidence-interval calibration test

Helixo-2 emits a scalar `ai_confidence`. To make it useful, ask the vendor for **quantile forecasts** (p10, p50, p90 by day). Then run a coverage test: of the days where actual ∈ [p10, p90], do we get ~80% coverage as advertised? Today's `ai_confidence` field cannot be calibrated because it isn't tied to a probabilistic claim. Until the vendor exposes intervals, hide the score from the operator UI — it currently misleads (see hiroki_phl).

### Action list for Ross

1. Add a "vs naive" column to the forecast accuracy dashboard. **Headline KPI = MASE per outlet.**
2. Pull `ai_confidence` from operator-facing views until per-outlet calibration is verified.
3. Mark hiroki_det / lsbr / quoin forecasts as "unvalidated — no actuals" in the UI.
4. Rebuild vessel forecast pipeline to score only forward predictions (`forecast.as_of < actual.date`).
5. Apply rolling 14-day bias correction (multiply forecast by mean(actual)/mean(forecast) over trailing window) — likely cuts pooled WAPE by 5–10 points instantly at over-forecasting outlets.
6. Stand up SARIMA / ETS as a free in-house benchmark; if it beats helixo-2 at any outlet, that's the production model for that outlet.

---

*All numbers above computed directly from `data/<outlet>.json` files in this worktree as of 2026-04-30. Code: `/tmp/forecast_audit.py` (in working session).*
