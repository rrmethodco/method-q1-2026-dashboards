# Phase A.1 — Integration Review

**Reviewer role:** Cross-system contracts + data flow
**Date:** 2026-05-04
**Reviewed:** PR #90 (Sprint 1) + PR #91 (Sprint 2/3) merged to `main`

---

## TL;DR (3-5 lines)

The core data flow — sync writes validation JSON → upload action POSTs to Supabase Storage → drift detector lists by source prefix → banner writer uploads per-outlet → dashboard fetches — is structurally sound and path-consistent. Three issues require attention before relying on this in production: (1) the anomaly detector silently no-ops for any outlet with multiple revenue centers (lsbr, hiroki\_det, quoin — three of eleven outlets), because it hardcodes `order_details.main` which doesn't exist for those outlets; (2) the Slack deduplication in `alert_dispatcher.ts` and the retry budget in `retry_repair.ts` both use in-memory `Map` state that evaporates on every 5-minute Edge Function cold start, meaning the dedup never fires and a broken workflow will be re-dispatched on every cycle until it heals or the cron is disabled; (3) the validation-pruner workflow is a structural no-op because `data/_validation/` is gitignored — files only live in Supabase Storage which has no pruning mechanism.

---

## End-to-end data flow trace

Toast Orders — one complete cycle:

| Step | Component | Handoff | Verdict |
|---|---|---|---|
| 1 | `toast-sync.yml` cron fires at 12:00 UTC | triggers `sync_outlet()` | ✓ |
| 2 | `sync_outlet()` calls `run_validation()` before `transform_orders()` | validation runs on raw combined rows | ✓ |
| 3 | `runner.py` writes `data/_validation/toast_order_<ts>.json` | timestamp format: `20260504T120000Z` (no underscores — safe for `${name%_*}` parse) | ✓ |
| 4 | `runner.py` injects `_validation_index.toast_order` into `data/lsbr.json` in place | runs before `write_atomic()` call; `merge_payloads()` starts from `dict(existing)` so index survives | ✓ |
| 5 | `merge_payloads()` writes merged payload via `write_atomic()` | `_validation_index` key is preserved (fresh payload never sets it) | ✓ |
| 6 | `git add data/*.json` commits outlet file with embedded index | `data/_validation/` is gitignored — validation files not committed | ✓ |
| 7 | Upload composite action: `for f in data/_validation/*.json; source="${name%_*}"` | correctly extracts `toast_order` from `toast_order_20260504T120000Z.json` for all source names tested | ✓ |
| 8 | `curl POST $SUPABASE_URL/storage/v1/object/validation/$source/$name` | path: `validation/toast_order/toast_order_<ts>.json` | ✓ |
| 9 | `drift_detector.ts`: `from("validation").list("toast_order", { sortBy: name desc })` | matches upload path structure — `list("toast_order")` returns objects at prefix `toast_order/` | ✓ |
| 10 | `drift_detector.ts`: `download("${source}/${latestFile}")` | `toast_order/toast_order_<ts>.json` — matches upload path | ✓ |
| 11 | `drift_detector.ts` reads/writes `_schemas/toast_order.json` inside `validation` bucket | separate prefix from sources — no collision possible | ✓ |
| 12 | `banner_writer.ts`: `from("banner").upload("${outlet}.json", ...)` | top-level, no subdir | ✓ |
| 13 | Dashboard fetches `/storage/v1/object/public/banner/lsbr.json` | matches top-level upload | ✓ |
| 14 | `refreshValidationPanel()` merges `_validation_index` (git-embedded) + banner (Storage) | clean precedence: agent's `worst_class` overrides only if more severe | ✓ |

---

## BLOCK DEPLOY — Critical contract mismatches

None that fully prevent the system from running. The issues below range from silent data-loss to operational alert flood.

---

## High-impact integration concerns

**1. Anomaly detector silently skips multi-RC outlets (`order_details?.main?.daily`)**

`anomaly_detector.ts` line 55 hardcodes `order_details?.main?.daily`. In the actual outlet payloads, only single-concept outlets (lowland, mulherins, kampers, etc.) use `main` as their revenue-center key. Multi-RC outlets — `lsbr` (`bar_rotunda`, `le_supreme`), `hiroki_det` (`hiroki_san`, `sakazuki`, `aladdin_sane`), `quoin` (`quoin_restaurant`, `quoin_rooftop`, `simmer_down`) — have no `main` key. For those three outlets the `od` variable is `undefined`, the `od.length < 60` guard trips, and the detector produces zero audits and zero alerts, silently. These are among the highest-revenue outlets.

Fix: replace the hardcoded key with a fallback that unions daily rows across all RC keys, or uses the first RC key, or uses a `combined` key when present.

**2. Slack dedup and retry budget use in-memory state — evaporate on cold start**

`alert_dispatcher.ts` uses `const recentAlerts = new Map<string, number>()` for deduplication (60-min window). `retry_repair.ts` uses `const recentRetries = new Map<string, number[]>()` for retry budget (3 per 30 min). Supabase Edge Functions are stateless: every invocation (every 5 minutes) is a fresh cold start. These Maps are never populated from a prior run. Consequence: every breaking drift event that persists will fire a Slack message every 5 minutes. A cancelled workflow will be re-dispatched every 5 minutes until it succeeds or the cron is stopped manually. The spec explicitly notes this risk in a comment ("Worst case: an extra retry") but the Slack dedup has no such acknowledgement and the risk is unacceptably high for a Slack channel.

Fix: persist dedup/retry state to a Postgres table (Supabase already present) — one row per key with `last_fired_at`. This is the "switch to a Postgres table in Phase B" path mentioned in the retry comment; it should be Phase A before alerts go live.

**3. Validation pruner is a structural no-op; Storage files grow unbounded**

`validation-pruner.yml` runs `retention.py --data-dir ../data --keep-days 30` on a fresh GH Actions checkout, then does `git add data/_validation/ || true`. Because `data/_validation/` is gitignored (correct per `.gitignore`), the runner's workspace will never contain validation files and the pruner deletes nothing. There is no corresponding pruning mechanism in Supabase Storage. The `validation` bucket will accumulate one file per source per sync run indefinitely. At 7 sources × daily = 2,555 files/year minimum.

Fix: add a Supabase Storage list+delete loop in the Edge Function or a separate scheduled function, or change `retention.py` to delete from Storage via Supabase client.

---

## Medium / observation

**4. `GITHUB_PAT` secret undocumented in spec**

`lib/github.ts` reads `Deno.env.get("GITHUB_PAT")` and throws if absent. The spec's secrets inventory lists only `SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`, and `SUPABASE_SERVICE_ROLE_KEY`. If `GITHUB_PAT` is not added to the Supabase Edge Function secrets, `runRetryRepair()` throws on every invocation, which will surface as `errors: ["retry_repair: Error: GITHUB_PAT secret not set"]` in the response but won't prevent drift detection or banner writing (errors are caught per-agent).

**5. Audit path: spec says `data/_audit/` (git); implementation uses Supabase Storage `audit` bucket**

Spec section 8 specifies `data/_audit/agent_decisions.jsonl` in the git repo. Implementation writes to a Supabase Storage `audit` bucket at `agent_decisions.jsonl`. The audit bucket is private and not accessible without the service role key. Nothing currently reads from `data/_audit/` in the codebase. The divergence is acceptable (Storage is better for append-only logs), but it means the spec's statement "stored in repo so git history preserves it" is not met — 90-day retention is now Storage-SLA-dependent.

**6. `pg_cron` uses `net.http_get` (GET) for Edge Function invocation**

The migration fires a GET request. `Deno.serve` in `index.ts` accepts any HTTP method (no method check). This works, but Supabase Edge Function docs typically show POST for cron triggers. If Supabase ever adds method enforcement, this will silently stop triggering. Low risk, worth a note.

**7. Hardcoded project ref `mmwislzsgnjxjxssynwm` in two places**

Dashboard JS line 1089 and migration `20260504000001_agent_cron.sql` both hardcode the Supabase project ref. Two places to update on project migration. No single source of truth. Document in runbook.

**8. Upload action fails silently on missing secrets**

If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` are unset (empty string), `curl -sf` fails with exit code 22 (HTTP error) or a connection error. The `|| echo "failed to upload"` swallows the error and the step completes green. The sync job succeeds. The validation file never reaches Storage. The banner shows stale data with no alert. A `[[ -z "$SUPABASE_URL" ]] && exit 0` guard at the top of the action script would at least make the failure legible in GH Actions logs.

**9. Forecast and MarginEdge uploads race with the `validation` bucket**

`forecast-sync.yml` (group: `forecast-sync`) and `marginedge-sync.yml` (group: `marginedge-sync`) run on independent concurrency groups from `data-sync`. They can run concurrently and upload to the same bucket paths as the `data-sync` group workflows. Because sources are namespaced by prefix (`helixo2_forecast/`, `marginedge_invoice/`), this is safe in steady state. The only collision risk is two runs of the same source within the same UTC second — `x-upsert: true` means the second write wins; the summary from the first run is lost. Probability is low; impact is one missed validation summary.

---

## Things integrated cleanly

- **Storage path structure is consistent end-to-end.** Upload POSTs to `validation/$source/$filename`; drift detector lists `from("validation").list(source)`; downloads `${source}/${filename}`. No mismatch.
- **Banner path is consistent.** Writer uploads `${outlet}.json` (top-level); dashboard fetches `/storage/v1/object/public/banner/<outlet>.json`. Exact match.
- **Filename timestamp format has no underscores.** `isoformat(timespec="seconds")` → colon+hyphen strip → `20260504T120000Z`. The `${name%_*}` bash parse correctly strips from the last underscore for all source names (`toast_order`, `toast_time_entry`, `marginedge_invoice`, `tripleseat_event`, `helixo2_forecast`, `sage_budget`, `resy_survey`).
- **Per-outlet validation in both forecast and budget.** Both `forecast_engine.py` and `budget_sync.py` iterate per-outlet and call `run_validation()` with `outlets_touched=[outlet]` — correct, not a bulk call.
- **`_validation_index` survives `merge_payloads()`.** `runner.py` writes it into the on-disk outlet file before `write_atomic()` is called. `merge_payloads` initializes `merged = dict(existing)` preserving all keys not explicitly overwritten by the fresh payload.
- **Concurrency no-op on validation-pruner.** Runs at 05:00 UTC, well clear of the 12:00–13:30 UTC sync window.
- **`_schemas/` prefix in `validation` bucket cannot collide with a real source.** Source names are all alphanumeric with underscores; `_schemas` starts with underscore which no source uses.
- **Dashboard banner fetch is gracefully tolerant.** `catch (e) { /* tolerate missing banner */ }` means Sprint 1 deployments (no agent worker yet) continue to render `_validation_index` data without breaking.
- **Drift detector reads stored schema from `_schemas/<source>.json` via the same `validation` bucket.** Path does not overlap with source upload paths. Seeding on first run is clean.

---

## Recommendations

| Priority | Action |
|---|---|
| P0 | Fix `anomaly_detector.ts` to not assume `order_details.main` — union across RC keys or fall back to first key. Three of eleven outlets are dark. |
| P0 | Persist Slack dedup state and retry budget to Postgres before enabling Slack alerts. In-memory state evaporates every 5 minutes. |
| P1 | Add Supabase Storage pruning for the `validation` bucket — list files older than 30 days per source prefix and delete. |
| P1 | Add `GITHUB_PAT` to the Supabase Edge Function secrets runbook and project `.env.example`. |
| P2 | Add a `[[ -z "$SUPABASE_URL" ]] && { echo "SUPABASE_URL unset — skipping upload"; exit 0; }` guard to the upload action to make missing-secret failures visible in logs. |
| P2 | Reconcile spec's `data/_audit/` (git) vs actual `audit` bucket (Storage). Update spec or add note that 90-day retention is Storage-SLA-dependent. |
| P3 | Extract `mmwislzsgnjxjxssynwm` project ref to a single config location (env var or `supabase/config.toml`) and reference it from both the dashboard HTML and the cron migration. |
