# Trustworthy Reporting Engine — Design

**Status:** Draft, awaiting Ross's review
**Date:** 2026-05-04
**Owner:** Ross Richardson (rr@methodco.com)
**Scope:** Project A of the agentic-reporting initiative. Project B (SaaS productize) is out of scope for this spec.

---

## Problem statement

The Method Co dashboard pipeline produces silent data-quality and reliability failures that the user discovers only by spotting wrong numbers in the rendered output. Examples observed in the 2026-04-28 → 2026-05-04 window:

| Class | Example |
|---|---|
| Silent schema drift | Resy survey shape changed on/around 2026-04-17. The scraper kept appearing to succeed; `recommend` came back null on every survey; NPS card flatlined showing "—". No alert. |
| Silent timezone bug | Toast labor hour buckets ran in UTC instead of ET for ~6 weeks. Mulherin's labor "peak" displayed at hour 22 (10pm) instead of hour 18 (dinner rush). No alert. |
| Sync workflow silently cancelled | MarginEdge sync cancelled for 3 consecutive days due to a GitHub Actions concurrency-queue overflow. COGS data 4 days stale on the dashboard. No alert. |
| Cross-workflow rebase race | Toast 2h backfill failed on push due to conflicting commits from other syncs landing during its window. Backfill data lost; required manual re-trigger. |
| Stale data on a dead path | Vessel served a 483-day-old forecast from a deprecated local-port algorithm because the production sync silently skipped vessel (no helixo-2 UUID match). |

Across these incidents the common failure mode is: **the pipeline writes data without a contract for "is this data correct," the dashboard renders whatever was written, and operators are the validation layer.**

## Goals

1. **Wrong financial numbers do not display.** When validation fails on a P&L-line metric, the affected card hides the number and surfaces the reason.
2. **Soft-signal staleness is always visible.** When NPS, reviews, or other non-financial metrics are stale or partially valid, the metric still displays with an inline freshness/confidence stamp.
3. **Transient infrastructure failures self-heal without operator action.** Push races, rate limits, expected schema renames, and timeout-class errors are retried by an agent loop with bounded retry budgets.
4. **Real-time Slack alerts on hard-fails only.** No alert noise for transient infra; only events the operator must act on. Slack channel: `C0B1N51L9TN` (Method Co `#dashboard-alerts` or equivalent — channel ID rather than name to survive renames).
5. **At-a-glance trust signal on every outlet view.** A small persistent panel shows "all sources current" or lists what's stale/failed/annotated with timestamps.
6. **Every agent decision is recorded** in an append-only audit log so operators can inspect *why* a number is what it is — and so future agents (A.3 investigation) have a training corpus.

## Non-goals (this phase)

- LLM-generated weekly snapshot copy (A.2)
- Operator-facing investigation chat (A.3)
- Forecast methodology overhaul (A.4)
- Multi-tenant SaaS productization (Project B)
- Migration off GitHub Actions for the syncs themselves
- Backfilling historical "what was the value of X on day Y *if* validation had been in place" — agents apply going-forward only
- **Daily email digest** — deferred per Ross 2026-05-04. Slack alone for Phase A.1. Re-evaluate after the channel has been live for 2 weeks; add email if Slack fatigue or off-hours coverage gap emerges.

## Failure semantics (locked decision)

Hybrid policy. Per metric class:

| Class | Examples | On validation failure |
|---|---|---|
| **Financial** | Net Sales, COGS $, Labor $, Prime Cost %, Budget variance | **Hard-fail.** Card hides number, displays reason ("COGS sync failed 3d ago — number hidden"). Hard-fails fire Slack push + go into the daily email. |
| **Soft signal** | NPS, review counts, Resy ratings, dwell time, ai_confidence | **Annotate.** Card displays last-known with inline stamp ("$30.28 · stale 2d · last sync 2026-05-02"). No real-time alert; included in daily email summary. |
| **Transient infra** | Push race, HTTP 429, HTTP 5xx, Resy session expiry, Playwright timeout | **Auto-heal.** Agent retries with backoff. Only escalates to alert after exhausting the retry budget (typically 3 attempts over 30 min). |

Classification table is config-as-code, owned per-metric in a single file (`config/metric_classes.yml`).

## Alerting (locked decision)

Tiered. Email digest deferred per Ross 2026-05-04 — Slack-only for Phase A.1.

| Tier | Channel | When |
|---|---|---|
| 1 | Dashboard banner (per-outlet, top of page) | Always — current trust state of that outlet's sources |
| 2 | Slack push to channel ID `C0B1N51L9TN` | Real-time on hard-fail or unrecoverable auto-heal |
| 3 (deferred) | Email digest to rr@methodco.com | Phase A.1.5 if needed — re-evaluate after 2 weeks of Slack-only operation |

Slack channel ID is config-as-code (env var `SLACK_DASHBOARD_ALERTS_CHANNEL`) so it can be re-pointed without code change.

## Architecture (Phase A.1) — locked: Approach 3 (Hybrid)

The existing GitHub Actions sync workflows continue to handle data ingestion. Pydantic validation gates are added inline to each sync script. A new dedicated agent worker, deployed as a **Supabase Edge Function** (Method already has Supabase per Ross 2026-05-04), polls validation status + recent commits and runs the agent loops (drift detector, anomaly detector, retry/repair, alert dispatcher).

Why Edge Function over VPS:
- Method already pays for Supabase — zero new infra spend
- Deploys via Supabase CLI / GitHub Actions — same auth surface Method already operates
- Cron triggers built-in (`pg_cron` + Edge Function invocation) — no separate scheduler
- Limitations vs VPS that we're explicitly accepting: per-invocation runtime cap (~150s), no Playwright (so the schema-drift LLM step uses Anthropic API only, not headless-browser scraping), bounded memory per call. None of these limit Phase A.1 — drift detector samples small row counts, anomaly detector is pure math, retry/repair invokes GitHub Actions via API.

### Why this over alternatives

- **Approach 1 (GH Actions only):** No dedicated worker means agent loops run on the GH Actions cron, capped at the schedule cadence and limited in state retention. Schema-drift detection that needs to compare against historical shapes becomes awkward. Anomaly detection on rolling windows is doable but slow.
- **Approach 2 (full workflow engine):** Inngest/Trigger.dev/Temporal is the right end-state when SaaS and multi-tenancy land (Project B), but it requires migrating working sync workflows. Migration risk + $50–200/mo cost not justified by Phase A.1 alone.
- **Approach 3 (chosen):** Surgical. Existing syncs unchanged. New agent worker is a single, self-contained Python service. Migrates cleanly to Inngest/Temporal in Project B by repackaging the same agent code as workflow handlers.

## Phase A.1 components

### 1. Per-source Pydantic schemas

One Pydantic V2 model per data feed, in `toast-etl/schemas/`. Models:

- `ToastOrder` (matches Toast `/ordersBulk` row)
- `ToastTimeEntry` (matches Toast `/labor/v1/timeEntries` row)
- `ResySurvey` (matches `transform_resy_survey_row` output)
- `MarginEdgeInvoice` (matches MarginEdge invoice row + line items)
- `TripleseatEvent` (matches Tripleseat event row)
- `Helixo2Forecast` (matches `daily_forecasts` row)
- `SageBudgetLine` (matches the Sage Intacct budget row)

Each model defines required fields, types, value bounds (e.g. `net_sales >= 0`, `recommend in 0..10 or None`), and a `validate_business_rules()` method for cross-field invariants (e.g. `paid_date >= opened_date`, `total_hours == regular_hours + overtime_hours`).

Each sync script's transform layer pipes raw rows through the model. Failures don't crash; they go into a `_validation_errors.json` sibling file with row offset, error class, sample row (PII-redacted).

### 2. Validation status output

Each sync run writes a `data/_validation/<source>_<timestamp>.json` file:

```json
{
  "source": "resy",
  "ran_at": "2026-05-04T14:00:00Z",
  "rows_in": 2150,
  "rows_valid": 2105,
  "rows_invalid": 45,
  "rows_skipped": 0,
  "schema_version": "resy_v3",
  "errors": [
    {"row_offset": 1843, "field": "recommend", "code": "all_score_buckets_null",
     "sample_keys": ["date_completed", "overall_score", "responses", "..."]}
  ],
  "outlets_touched": ["lsbr", "mulherins", "..."]
}
```

The agent worker watches this directory; the dashboard reads the latest file per source for its trust panel.

### 3. Schema-drift detector agent

Runs daily at 14:30 UTC (after all morning syncs). For each source:

1. Load last-known schema from `data/_schemas/<source>.json`
2. Inspect a sample of rows from the most recent sync run
3. Compute diff (added fields, removed fields, type changes, value distribution shifts)
4. Classify:
   - **Additive non-breaking** (new optional field): auto-update stored schema, commit via PR auto-merged
   - **Breaking** (required field removed, type changed, all-null on a populated field): open PR with sample shape attached, do NOT auto-merge, alert on the breaking change
   - **Stable** (no diff): no action

Uses Claude API (Sonnet) for the classification step — the LLM reads the diff + sample rows + the existing parser code and writes a structured classification + recommended parser patch. Classification schema is forced via Pydantic.

### 4. Anomaly detector agent

Runs after each sync completes (event-driven via the validation status file watcher). For each (outlet × metric × DOW) tuple:

1. Compute rolling 8-week mean + std on the metric (excluding outliers from prior runs)
2. Today's value beyond ±3σ → flag
3. Annotate the dashboard card via the trust panel
4. Slack push if metric is in the financial class
5. Append to daily email digest

Stored anomaly history in `data/_anomalies/<outlet>.json` so operators can see "this is the 4th Saturday in a row that COGS spiked."

### 5. Self-healing retry/repair agent

Continuously polls (every 5 min) for sync runs in `failed`/`cancelled` state. Classifies the failure:

| Failure pattern | Action |
|---|---|
| `git push rejected (non-fast-forward)` | `git pull --rebase --strategy-option=theirs` for `_validation/*` only, hand-merge the rest if needed, retry push (max 3 attempts) |
| `HTTP 429` from Toast/Resy/MarginEdge | Re-trigger workflow with `--rate-limit-multiplier=2`, backoff 15 min |
| `Resy session expired` | Trigger storage state refresh runbook, alert operator (this one needs human intervention to re-auth) |
| `Workflow cancelled` (concurrency queue overflow) | Re-dispatch immediately (we already separated forecast + marginedge per PR #85; remaining cancellations are real concurrency violations) |
| `Pydantic validation failure rate > 50%` on a source | Halt subsequent syncs of that source, alert hard, do NOT mask with annotations |

Action history logged to `data/_audit/<source>_<date>.jsonl`.

### 6. Alert dispatcher

Single component that consumes events from the other agents and routes per the alerting policy. Implements:

- **Slack** `chat.postMessage` POST to channel ID `C0B1N51L9TN` (configurable via `SLACK_DASHBOARD_ALERTS_CHANNEL` env var). Bot token stored as Supabase secret + GitHub Actions secret (`SLACK_BOT_TOKEN`).
- **Banner state writer** — writes `data/_banner/<outlet>.json` consumed by the dashboard.
- **Email digest** — deferred (see Alerting section). When added (Phase A.1.5), this component gains a `send_email()` method; no architectural change needed.

Deduplication: identical event within 60 min is suppressed.

### 7. Dashboard validation panel

New UI element on every outlet view (top-right, collapsed by default). Shows for each source:

- ✓ green check + last-sync timestamp + "all current"
- 🟡 yellow + "stale Xd" with the staleness count
- 🔴 red + "validation failed Xh ago — see card reasons"

Click the panel → expands to show per-source detail + the latest `_validation/<source>_<timestamp>.json` summary.

### 8. Audit log

Append-only `data/_audit/agent_decisions.jsonl`. Every agent decision is one line:

```json
{"ts": "2026-05-04T14:00:00Z", "agent": "drift_detector", "source": "resy", "decision": "additive_non_breaking", "details": {...}, "action_taken": "schema_v3 → schema_v4 (added: question.weight)"}
```

Becomes the training corpus for A.3 (investigation agent).

## Data contracts

### Sync output contract

Every sync writes:

1. `data/<outlet>.json` (existing) — the data payload
2. `data/_validation/<source>_<run_timestamp>.json` (new) — validation status from this run
3. Optional: `data/_validation_errors/<source>_<run_timestamp>.json` (new) — sample of invalid rows (PII-redacted)

### Agent worker → dashboard contract

The agent worker writes:

1. `data/_banner/<outlet>.json` — current banner state (read by dashboard)
2. `data/_audit/agent_decisions.jsonl` — append-only log

The dashboard reads these but never writes them.

### Agent worker outputs (operator-facing)

1. Slack messages to channel ID `C0B1N51L9TN` (configurable via `SLACK_DASHBOARD_ALERTS_CHANNEL`)
2. Email digest — deferred per Ross 2026-05-04 (not implemented in Phase A.1)

## Operational requirements

| Concern | Requirement |
|---|---|
| Validation latency | A sync's validation completes inline before commit. No async gap. |
| Agent worker availability | Best-effort. If the worker is down, syncs still run and write validation files; agent loops resume on next worker poll. The dashboard still renders without the trust panel updating (banner shows "agent worker last seen Xh ago"). |
| Audit log retention | 90 days minimum, indefinite preferred. Stored in repo so git history preserves it. |
| Validation file retention | Last 30 days, auto-pruned by the agent worker |
| PII | Validation error samples are redacted before write — no `user.email`, no `user.full_name`, no `reservation.contact_phone` |
| Secrets | `SLACK_BOT_TOKEN` + `ANTHROPIC_API_KEY` + `SUPABASE_SERVICE_ROLE_KEY` stored as GitHub Actions secrets and Supabase Edge Function env vars; never written to repo |

## Open decisions — RESOLVED 2026-05-04

| # | Decision | Locked answer |
|---|---|---|
| 1 | Worker host | **Supabase Edge Function** (Method already has Supabase) |
| 2 | Slack channel | **Channel ID `C0B1N51L9TN`** (configurable via env) |
| 3 | Email service | **Deferred** — Slack-only for Phase A.1; revisit after 2 weeks |
| 4 | Anomaly threshold | **±3σ in shadow mode for first 2 weeks**, then enable Slack alerts |

## Risks

- **Anomaly detector false-positive flood in week 1.** First runs will compare against a thin baseline and flag many "anomalies" that are just normal variation. Mitigation: silent shadow mode for first 2 weeks (logs to audit only, no Slack), then enable alerts.
- **Pydantic validation breaks existing data.** Models must be permissive enough to pass current good data — if the model is stricter than reality, every sync hard-fails. Mitigation: build models from current data, not from idealized specs; require all current rows to validate before merging the model.
- **Agent worker goes down silently.** A "no recent agent decisions" signal is itself a critical alert. Mitigation: the dashboard banner shows "agent worker last seen Xh ago" prominently when stale.
- **Cost creep.** Claude API costs for the drift detector agent could grow if it runs on every sync run. Mitigation: drift detector runs daily not per-sync; uses Sonnet not Opus; bounded sample size (50 rows max per source per run).

## Phases A.2–A.4 (sketches, not committed)

### A.2 Reporting agents

Replace the rule-based snapshot generator with focused agents per domain (Sales, Labor, COGS, Guest). Each agent reads validated data, writes its section of the snapshot, cites raw data for every claim. GM email author composes the daily email summary. Estimated 2 sprints.

### A.3 Investigation agent + chat

Operator-facing chat. Agent reads validated data + audit log + raw source data, answers "why" questions with cited drill-down. UI: small chat sidebar on each outlet view. Estimated 2–3 sprints.

### A.4 Forecast agent

Replace helixo-2 black box with ensemble:

- helixo-2 baseline (kept as one signal)
- Naive same-DOW-prior-week (sanity floor)
- Tripleseat overlay (deterministic for booked events)
- Mews occupancy regressor (for hotel restaurants)
- Weather feature (for patios + rooftops)
- Resy forward-book lookup (for FSR covers)

Per-outlet model selection. Calibrated confidence intervals. Continuous learning from the Forecast Accuracy ledger (the tab shipped in PR #75 becomes the training feedback loop). Estimated 3–4 sprints.

---

**End of spec. Awaiting Ross review.**
