# Phase A.1 — Correctness Review

_Reviewer: Correctness agent · 2026-05-04 · PRs #90 + #91_

---

## TL;DR

All 55 tests pass. Zero hard schema errors against production data — every schema accepts real rows without crashing. One confirmed logic bug: `resy_os_scraper.py` passes `outlets_touched=[]` to the runner, which makes the banner writer permanently blind to Resy survey failures for every outlet. A second high-impact design flaw is the drift detector's `rows_warned > 0` fast-path condition, which triggers an LLM call every 5 minutes for `resy_survey` (68–100% warned rows in production) instead of only when schema keys actually changed. No `BLOCK DEPLOY` — neither issue silently corrupts data — but both should be fixed before the shadow window closes.

---

## Per-schema real-data validation results

| Schema | Source file | Clean | Warned | Hard errors | Status |
|---|---|---|---|---|---|
| ToastOrder | `toast_sync.py` | N/A (aggregated pre-storage) | — | — | Schema correct; tested via e2e |
| ToastTimeEntry | `toast_sync.py` | N/A (aggregated pre-storage) | — | — | Schema correct; tested via e2e |
| ResySurvey | guest block in `data/*.json` | ~32% | ~68% | 0 | ✅ (warnings expected — known post-2026-04-17 drift) |
| MarginEdgeInvoice | `cogs.invoices` in `data/*.json` | 1154 | 0 | 0 | ✅ |
| TripleseatEvent | `events.events` in `data/*.json` | 23,265 | 21 | 0 | ✅ (warnings = fb_exceeds_grand_total, legitimate) |
| Helixo2Forecast | `forecast.daily` in `data/*.json` | 512 | 0 | 0 | ✅ |
| SageBudgetLine | `budget.daily` in `data/*.json` | 3,576 | 0 | 0 | ✅ |

All field names match between schemas and actual sync output. No hard schema errors in any production data file. Docstrings present on every class.

---

## 🟠 High-impact correctness concerns (logic gaps, edge cases)

### 1. `resy_os_scraper.py:463` — `outlets_touched=[]` makes banner blind to Resy failures

**File:line:** `toast-etl/resy_os_scraper.py:461-464`

`transform_to_guest_block()` calls `run_validation(..., outlets_touched=[])`. The validation summary file is written with `outlets_touched: []`. In `banner_writer.ts:34`, the banner skips any source where `!summary.outlets_touched.includes(outlet)`. Result: all 8 Resy-enabled outlets have permanently empty Resy validation state in their banners, regardless of error count.

This is not intentional. The resy scraper runs once per venue in a loop (line 843 `for oid, slug in targets.items():`), so `outlets_touched=[oid]` is the correct fix. The current code passes `[]` because `transform_to_guest_block` was written as a pure transform function and the caller didn't thread the outlet id through.

**Reproduction:** run `resy_os_scraper.py` on any venue, then check `data/_validation/resy_survey_*.json` — `outlets_touched` will be `[]`.

**Impact:** Resy survey errors (e.g. all-null score rows that could signal a Resy schema break) never surface in the per-outlet banner. The drift detector (which reads from Supabase storage, not `outlets_touched`) is unaffected.

**Fix:** In `resy_os_scraper.py`, thread `outlet_id` into `transform_to_guest_block` and pass it as `outlets_touched=[oid]`.

---

### 2. `drift_detector.ts:81` — LLM called every 5 min for `resy_survey` due to `rows_warned` fast-path condition

**File:line:** `supabase/functions/agent-worker/agents/drift_detector.ts:81`

The stable fast-path is `if (added.length === 0 && removed.length === 0 && summary.rows_warned === 0) continue`. In production, `resy_survey` has 68–100% warned rows (all-null-score buckets, known since 2026-04-17 drift). Every 5-minute pg_cron invocation hits this source, finds `rows_warned > 0`, and calls `classifyDrift()` with the LLM — even though no schema keys have changed.

This is wasteful (unnecessary LLM spend every 5 minutes) and may exhaust Anthropic rate limits under load. The `rows_warned` signal is useful for the anomaly detector, not the key-set drift detector.

**Fix:** Change the fast-path to `if (added.length === 0 && removed.length === 0) continue` and handle `rows_warned > 0` with a separate counter-based escalation (e.g. warn only after N consecutive runs with high warned ratio).

---

### 3. `anomaly_detector.ts:68-74` — `od[-1]` assumed to be yesterday without date validation

**File:line:** `supabase/functions/agent-worker/agents/anomaly_detector.ts:68-69`

The detector uses `od[od.length - 1]` as "yesterday's value" without checking whether its `date` field is actually recent. If a sync hasn't run for several days, `od[-1]` could be stale by 48+ hours. The Z-score computation would then report the stale value as an anomaly or mask a real anomaly.

In practice, `vessel` (last daily = 2025-10-04) is correctly skipped by the `od.length < 60` guard. No currently active outlet has this problem. But if any outlet accumulates a data gap, the detector would silently mislabel dates.

**Fix:** Add a staleness check: `if (Date.now() - new Date(yesterday.date + "T12:00:00Z").getTime() > 48 * 3600 * 1000) continue`.

---

### 4. `retry_repair.ts:19` + `alert_dispatcher.ts:15` — In-memory dedup/retry Maps wiped on cold start

Both `recentRetries` (retry_repair) and `recentAlerts` (alert_dispatcher) are module-level `Map` objects. Supabase Edge Functions cold-start on every invocation unless the runtime happens to reuse a warm instance. Cold starts reset both maps, meaning:

- The 3-retry budget is "3 per warm invocation sequence," not "3 per 30 min." A failing workflow could get 3 retries per cold start if cold starts are frequent.
- Dedup in alert_dispatcher is per-cold-start; a breaking alert could fire on every pg_cron tick if the function cold-starts each time.

The retry_repair code acknowledges this at line 16-18 ("acceptable for Phase A.1"). In practice, Supabase Edge Functions do cold-start frequently (often every invocation for low-traffic functions). This is a real risk, not theoretical — during a real outage, the alert channel could get flooded.

**Fix (Phase B):** Move retry tracking to a Postgres table (as the code already notes). For the alert dedup, a single-row Supabase table keyed by `(kind, source, text_hash)` with `last_fired_at` suffices.

---

## 🟡 Medium/observation

**Audit log race condition is real but low-impact.** `audit.ts` does READ→APPEND→WRITE non-atomically. With pg_cron every 5 min and each run taking ~10-30 seconds, simultaneous invocations are plausible (Supabase's pg_cron doesn't prevent overlap by default). The worst case is one run's lines being dropped. Given audit is logging, not business-critical data, this matches the Phase A.1 risk acceptance. Phase B Postgres table would eliminate it.

**`_validation_index` write is atomic.** The `.tmp` → `rename` pattern in `runner.py:116-119` is correct. A kill mid-write leaves the `.tmp` file orphaned, not a partial `.json` file. The original file is untouched until `tmp.replace(outlet_path)` succeeds. No half-written file risk.

**`Exception` breadth in runner.py:53 is appropriate.** Pydantic V2 raises `ValidationError` for field-level failures, but can raise `TypeError` or `PydanticUserError` for malformed input. Catching broadly and logging `str(e)[:500]` plus `row_redacted` is the right call — the full error message is captured with field path and value (verified in testing).

**Empty `rows` produces a valid summary file.** Tested — runner writes a correct `rows_in=0` summary even with empty input. No crash or divide-by-zero.

**`outlets_touched=[]` + `update_outlet_index=True` skips cleanly.** The for-loop in `runner.py:101` simply doesn't iterate; no crash.

---

## ✅ Things done well

- **Zero hard schema errors in production.** All 7 schemas parse every real data row. Schema field names match sync output exactly (verified for `Helixo2Forecast`, `SageBudgetLine`, `TripleseatEvent`).
- **MarginEdge line-item sum check is well-designed.** Correctly uses `abs(total)` for credits, skips zero-total rows, only fires when `extendeds` list is non-empty.
- **ResySurvey drift tolerance.** Making all 5 score buckets `Optional` with a business-rule warning (not a hard fail) was the right call — rows still ingest and the drift is annotated.
- **Atomic writes throughout.** All sync scripts (marginedge, resy, toast, tripleseat, forecast, budget) use `.tmp` → `rename`. The runner does the same.
- **`pytest.raises(ValidationError)` used consistently.** No `pytest.raises(Exception)` slippage.
- **Docstrings on every class.** All 13 schema classes have docstrings.
- **Agent orchestration fault isolation.** Each agent in `index.ts` is wrapped in its own try/catch, so a drift_detector throw does not prevent anomaly_detector and retry_repair from running.
- **Shadow mode correctly respected.** Anomaly detector honors `ANOMALY_SHADOW_UNTIL` env var and gates Slack events on `!anomaly.shadowed`.

---

## Test coverage gaps

The following edge cases should have tests but don't:

1. **`resy_os_scraper` outlets_touched propagation** — no test verifying that a per-venue scrape produces a validation summary with the correct outlet in `outlets_touched`. Would catch the bug at item 1 above.
2. **Runner: update_outlet_index=False skips index write** — the flag is tested only with the default (`True`). A test with `False` would confirm the inverse.
3. **MarginEdgeInvoice: mixed None/non-None extended values** — the partial-sum mismatch path fires correctly but no unit test covers it. In practice this doesn't occur in current data but is a valid edge case.
4. **Anomaly detector: stale `od[-1]` date** — no test for the case where the last daily row is more than 48h old. The guard at line 56 (`od.length < 60`) catches vessel today but not a future stale-but-large outlet.
5. **e2e test does not exercise `update_outlet_index=False`** — `test_full_pipeline` uses only the default; the False path has no e2e coverage.
