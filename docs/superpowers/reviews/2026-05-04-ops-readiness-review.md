# Phase A.1 — Ops-Readiness Review

**Reviewer:** Ops-Readiness Agent
**Date:** 2026-05-04
**Scope:** Deployment, observability, runbook quality, rollback, failure modes
**PRs reviewed:** #90 (Sprint 1 — validation foundation), #91 (Sprint 2+3 — Edge Function + agents)
**Status:** NOT YET DEPLOYED — Ross runs deploy commands tomorrow morning

---

## TL;DR (deploy-ready status)

Conditionally deploy-ready with two actions required before running `supabase db push`. The core architecture is sound and the happy path works. The single biggest pre-deploy gap: **the `upload-validation` composite action silently swallows failed uploads** — if GH Actions secrets are empty or the bucket doesn't exist yet, every workflow says "success" while the agent worker reads nothing. The second gap: **`app.settings.service_role_key` must be manually set via Supabase SQL Editor before `db push`**, or the pg_cron schedule will be created but will 401 on every tick. Neither is a code change; both are 5-minute pre-flight tasks. Proceed with those two fixes confirmed and this is a clean deploy.

---

## Pre-deploy checklist (Ross's morning)

Run these **before** touching the deploy commands.

- [ ] **Supabase CLI installed and authenticated**
  `npx supabase --version` returns a version string (1.x or later).
  `npx supabase projects list` lists the `method-kpi` project. If it 401s: run `npx supabase login` first.

- [ ] **4 Supabase Edge Function secrets set**
  Go to: `https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/settings/functions`
  Verify these 4 are present (exact names matter):
  - `ANTHROPIC_API_KEY`
  - `SLACK_BOT_TOKEN`
  - `GITHUB_PAT`
  - `SLACK_DASHBOARD_ALERTS_CHANNEL` (value: `C0B1N51L9TN`)

  If any are missing: Settings → Edge Functions → + Add secret.

- [ ] **`app.settings.service_role_key` Postgres setting** *(REQUIRED before `db push`)*
  Run this in Supabase SQL Editor (`https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/sql/new`):
  ```sql
  alter database postgres set "app.settings.service_role_key" = '<your-service-role-key>';
  select length(current_setting('app.settings.service_role_key'));
  ```
  Expected: returns the key length (typically 256+). If 0 or error: the pg_cron schedule will be created but will 401 on every invocation — silent failure.

- [ ] **2 GitHub Actions secrets set**
  Go to: `https://github.com/rrmethodco/method-q1-2026-dashboards/settings/secrets/actions`
  Verify these are present:
  - `SUPABASE_URL` (value: `https://mmwislzsgnjxjxssynwm.supabase.co`)
  - `SUPABASE_SERVICE_ROLE_KEY`

- [ ] **Slack bot installed and invited to channel**
  The bot associated with `SLACK_BOT_TOKEN` must be a member of channel `C0B1N51L9TN`.
  Verify: in Slack, open the channel → Members → confirm the bot is listed.

---

## Deploy commands (verified order + failure modes)

Run these **in order**. Each step must succeed before proceeding.

### Step 1: Link the project
```bash
npx supabase link --project-ref mmwislzsgnjxjxssynwm
```
- **What it does:** Associates the local `supabase/` directory with the remote project.
- **Failure: CLI not installed** → `npx: command not found`. Fix: `npm install -g supabase` or ensure node/npm is in PATH.
- **Failure: not logged in** → "User not found" or 401. Fix: `npx supabase login` first.
- **Failure: wrong directory** → "supabase/config.toml not found". Fix: run from the repo root (`objective-booth-ea13dd/`).
- **Verify:** `npx supabase projects list` shows `mmwislzsgnjxjxssynwm` as current.

### Step 2: Deploy the Edge Function
```bash
npx supabase functions deploy agent-worker --no-verify-jwt
```
- **What it does:** Bundles `supabase/functions/agent-worker/` (Deno + TypeScript) and deploys to the remote project. `--no-verify-jwt` matches `supabase/config.toml` (`verify_jwt = false`) since pg_cron invokes it with the service role key, not a user JWT.
- **Failure: Deno bundle error** → TypeScript compile failure. Fix: run `deno check supabase/functions/agent-worker/index.ts` locally first to see which file has the error.
- **Failure: function deploy times out** → retry. This is a known flaky condition on first deploy of a larger bundle (`@slack/web-api` is ~800KB). Retry 1-2x before escalating.
- **Failure: secrets not set** → Function deploys fine but will 500 on first invocation. This is NOT caught here; it surfaces in post-deploy verification.
- **Verify:** `https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/functions` shows `agent-worker` with a green "Deployed" badge.

### Step 3: Push migrations
```bash
npx supabase db push
```
- **What it does:** Applies `supabase/migrations/20260504000000_validation_bucket.sql` (creates 3 storage buckets) and `supabase/migrations/20260504000001_agent_cron.sql` (enables `pg_cron` + `pg_net`, creates the 5-min schedule).
- **Failure: migration already applied** → Supabase tracks applied migrations; it will skip already-applied ones. If you see "no migrations to apply" that means they already ran — proceed.
- **Failure: `pg_cron` extension not available** → Supabase Pro tier or above. If on Free tier, this will error. Check: `https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/database/extensions`.
- **Failure: `app.settings.service_role_key` not set** → The migration applies successfully (it just schedules the cron), but every cron tick will call the Edge Function with an empty `Authorization: Bearer ` header → 401 on each invocation. Silent failure. **Pre-deploy checklist item above prevents this.**
- **Failure: bucket already exists** → The `ON CONFLICT (id) DO NOTHING` clauses handle this; idempotent.
- **Verify:** See post-deploy verification below.

---

## Post-deploy verification

Run these immediately after `db push` succeeds.

### Edge Function deployed
```bash
curl -s -X POST \
  "https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker" \
  -H "Authorization: Bearer <SUPABASE_SERVICE_ROLE_KEY>" \
  -H "Content-Type: application/json"
```
**Expected response shape:**
```json
{
  "status": "ok",
  "ran_at": "2026-05-04T...",
  "agents_invoked": ["drift_detector: 0 audits, 0 alerts", "anomaly_detector: ..."],
  "errors": []
}
```
A `"status": "ok"` with an empty `"errors": []` is success. On first run, drift detector will log `seeded_initial_schema` for each source (no validation files in the bucket yet — all sources will show `continue` in the loop). That's expected.

If `"errors": ["drift_detector: ANTHROPIC_API_KEY not set"]` — a secret is missing. Go set it in the Supabase Functions settings.

### Storage buckets exist
Dashboard URL: `https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets`
Expect three buckets: `validation` (private), `banner` (public), `audit` (private).

### pg_cron scheduled
In Supabase SQL Editor:
```sql
select jobid, jobname, schedule, command from cron.job;
```
Expect one row: `jobname = 'agent-worker-tick'`, `schedule = '*/5 * * * *'`.

To confirm it's actually firing (wait ~5 min):
```sql
select job_run_details.status, start_time, end_time, return_message
from cron.job_run_details
join cron.job using (jobid)
where jobname = 'agent-worker-tick'
order by start_time desc limit 5;
```
Expect `status = 'succeeded'`. If `status = 'failed'` with an HTTP 401 message: the `app.settings.service_role_key` was not set.

### Validation upload working
After the next scheduled sync runs (first one is Toast Sync at 12:00 UTC):
```sql
-- Or manually trigger toast-sync via GH Actions workflow_dispatch
```
Then check the validation bucket:
`https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets/validation`
Expect a folder for `toast_order` containing a `toast_order_<timestamp>.json` file.

To trigger immediately without waiting for nightly: go to GitHub Actions → Toast Sync → Run workflow → default inputs.

### Drift detector seeded initial schema
After validation files exist in the bucket and the agent-worker has fired:
```sql
select * from cron.job_run_details order by start_time desc limit 1;
```
Then manually invoke the function and check the response body for:
`"drift_detector: 7 audits, 0 alerts"` — one `seeded_initial_schema` audit per source.

Verify the audit bucket: `https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets/audit` → `agent_decisions.jsonl` should exist with `seeded_initial_schema` entries.

### Slack alert test (synthetic)
To verify Slack without waiting for a real failure, temporarily add `"rows_invalid": 1` to a validation file in the bucket and trigger the function:
```bash
curl -s -X POST \
  "https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker" \
  -H "Authorization: Bearer <SUPABASE_SERVICE_ROLE_KEY>"
```
Expect a message in channel `C0B1N51L9TN`. Alternatively, test the Slack token directly:
```bash
curl -s -X POST https://slack.com/api/auth.test \
  -H "Authorization: Bearer <SLACK_BOT_TOKEN>"
```
Expect `"ok": true`.

---

## Failure modes + observability

### Edge Function crashes silently
Supabase Function logs: `https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/functions/agent-worker/logs`
Filter by "Error" level. Any unhandled exception in `index.ts` will appear here with full stack trace. Each agent is wrapped in a try/catch that appends to `result.errors[]` rather than crashing the function — so even partial failures produce a 500 response with the error list in the body.

### pg_cron not running
```sql
select job_run_details.status, start_time, return_message
from cron.job_run_details
join cron.job using (jobid)
where jobname = 'agent-worker-tick'
order by start_time desc limit 5;
```
If no rows: pg_cron was never scheduled (migration didn't apply). If `status = 'failed'`: check `return_message` for the HTTP status code. 401 = service_role_key not set. 500 = function error.

### Storage upload silently no-ops
**This is the most dangerous silent failure.** The `upload-validation` composite action (`/.github/actions/upload-validation/action.yml`) uses `curl -sf` with `|| echo "failed to upload"`. If `SUPABASE_SERVICE_ROLE_KEY` is empty or wrong:
- `curl` gets a 401/403 and exits nonzero
- The `|| echo` catches it and the loop continues
- The step exits 0 — the workflow shows green
- The validation bucket gets no new files
- The agent worker sees no new data; the drift detector seeds nothing; alerts never fire

**How to detect:** The echo output `"failed to upload toast_order/..."` is visible in the GH Actions step log. To find it: GitHub → Actions → any sync run → "Upload validation files to Supabase" step → expand log.

**How to verify it's working:** The validation bucket should contain files after any sync run. If the bucket is empty after 3+ sync runs, this silent-fail is active.

### Anomaly detector shadow mode flip
Shadow mode ends automatically on `2026-05-18T00:00:00Z`. After that date, anomalies trigger Slack alerts.

To flip early or extend:
1. Supabase Dashboard → Functions → agent-worker → Environment variables
2. Set `ANOMALY_SHADOW_UNTIL` to any ISO-8601 datetime
   - Extend: `2026-06-01T00:00:00Z`
   - Enable immediately: `2026-01-01T00:00:00Z` (past date)

No redeploy required — env var changes take effect on the next invocation.

### GITHUB_PAT and retry/repair
The `retry_repair` agent uses `GITHUB_PAT` (a Supabase secret, NOT a GH Actions secret). This secret must be present in Supabase Function secrets, not just in GH Actions. If missing, `retry_repair` will log `"retry_repair: GITHUB_PAT secret not set"` in the response errors but not crash the function.

---

## Rollback procedures

### Disable the agent worker without destroying data
```sql
select cron.unschedule('agent-worker-tick');
```
This stops pg_cron from invoking the function. The function itself remains deployed. Storage buckets, audit log, and validation files are untouched. To re-enable: run the cron schedule SQL from the migration again.

### Disable just the GH Actions upload step
If the upload step is causing failures (not the silent no-op case, but an actual failing step):
1. Edit `.github/actions/upload-validation/action.yml`
2. Add `if: false` to the upload step, or add a `|| true` to the curl line
3. Push to main — the next sync run will skip uploads

### Roll back a bad migration
Supabase migrations are not auto-reversible. Manual cleanup:

**To remove storage buckets (if they're causing problems):**
```sql
delete from storage.objects where bucket_id in ('validation', 'banner', 'audit');
delete from storage.buckets where id in ('validation', 'banner', 'audit');
```

**To remove the cron job:**
```sql
select cron.unschedule('agent-worker-tick');
```

**To remove the migration tracking entry** (so you can re-apply it):
```sql
delete from supabase_migrations.schema_migrations
where version in ('20260504000000', '20260504000001');
```

**Note:** If the buckets already contained files you need, download them first via the Supabase Storage dashboard before deleting.

### Roll back the Edge Function
Deploy the previous version. If there's no previous version (first deploy), simply unschedule the cron and the system returns to its pre-Phase-A.1 state — syncs still run, validation files are still written to disk and committed to the repo, the dashboard renders without the trust panel active.

---

## Critical gaps that will bite us

**BLOCK if not resolved:**

### 1. upload-validation action silently swallows upload failures
`/.github/actions/upload-validation/action.yml` line 36: `|| echo "  failed to upload $source/$name"`. The `curl -sf` exit code is caught by `||` and the loop continues. The step always exits 0. If `SUPABASE_SERVICE_ROLE_KEY` is wrong or the bucket doesn't exist:
- Every validation upload fails silently
- The agent worker has no input data
- Drift detector never seeds, anomalies never fire, Slack never alerts
- The system appears to be running (green checks everywhere) but is completely non-functional

**Fix (5 minutes):** Add an empty-key guard to the action:
```bash
if [ -z "$SUPABASE_KEY" ]; then
  echo "ERROR: SUPABASE_KEY is empty — skipping upload (check SUPABASE_SERVICE_ROLE_KEY secret)"
  exit 1
fi
```
Or change `|| echo "failed..."` to `|| { echo "failed $source/$name"; UPLOAD_FAILED=1; }` with a final `[ -z "$UPLOAD_FAILED" ]` check.

This is not a BLOCK on deploy, but it means **the first sign of a problem will be "agent worker does nothing" rather than "this workflow step failed."** Recommend fixing before go-live.

### 2. app.settings.service_role_key must be set manually before db push
The migration SQL comment says to do this but it's not enforced. If skipped: the cron job is scheduled, fires every 5 min, and every invocation sends `Authorization: Bearer ` (empty) → 401 → the function is never invoked. The cron job will show `status = 'failed'` in `cron.job_run_details`. Detectable but silent until you look.

**Resolution:** Covered in the pre-deploy checklist above.

---

## High-impact ops concerns

### 1. Retry/repair in-memory state resets on cold start
`retry_repair.ts` line 19: `const recentRetries = new Map<string, number[]>()`. This map is module-level — it survives within a warm instance but is wiped on cold start. Edge Functions go cold frequently (Supabase free/pro tear down idle instances). Result: the 3-retry-per-30-min budget resets on each cold start. A persistently-failing workflow could be retried more than 3 times if cold starts coincide with failure periods. Acceptable per spec comment ("worst case: an extra retry") but worth knowing.

**Mitigation path:** Move retry history to a Postgres table in Phase A.2.

### 2. Audit log has a read-modify-write race
`lib/audit.ts`: download existing JSONL → append new lines → re-upload. If two pg_cron ticks overlap (e.g., one invocation runs long and the next tick fires while it's still appending), the second write clobbers the first's additions. Under normal 5-min cadence this is unlikely (typical invocation completes in <10s) but a slow Anthropic API call in drift_detector could push runtime to 30-60s on a bad day.

**Mitigation path:** Switch to a Postgres table with `INSERT` for Phase A.2.

### 3. Drift detector calls Sonnet on EVERY tick with persistent warnings
`drift_detector.ts` line 81: the LLM is skipped only if `added.length === 0 && removed.length === 0 && summary.rows_warned === 0`. If any source has `rows_warned > 0` in its latest validation summary — a persistent condition once validation finds real issues — Sonnet is called on every 5-min tick for that source.

Worst case: 7 sources × 288 ticks/day = 2,016 Sonnet calls/day ≈ **$10/day ≈ $300/month**. Realistic (2 sources with persistent warnings): ~$90/month. Well within budget but worth monitoring from day 1.

The spec's risk section says "drift detector runs daily not per-sync." **That is not what was implemented.** The drift detector runs on every 5-min tick (it reads the latest validation file, which only changes at sync time, but calls Sonnet whenever warnings persist). This is a cost delta from the spec's assumption.

---

## Documentation/observability gaps

### 1. No README for the agent-worker
There is no `supabase/functions/agent-worker/README.md` or top-level project README explaining what the agent worker is, what it does, and how to operate it. The spec and plan docs live on the `claude/spec-trustworthy-reporting` branch, not on `main`. A future operator (or future Ross at 6am when something is on fire) has no single entry point.

**Minimum viable runbook:** A `supabase/functions/agent-worker/README.md` with: what it is, how to check it's running, how to disable it, where the logs are, the 5 env vars it needs.

### 2. No top-level README
The repo root has no README.md at all. The `docs/superpowers/` tree is only visible to someone who already knows it exists.

### 3. Index.ts has agent execution order comments but no "why" comments
The index.ts comment block documents what each agent does (good). It does not explain why the order matters (drift before anomaly before retry before alerts). If someone reorders agents while debugging, alert storms or missed alerts could result.

### 4. No documented runbook for common operator scenarios

| Scenario | Gap |
|---|---|
| Drift detector auto-applies a schema change that turns out to be wrong | No documented reversal procedure. Recovery: manually upload the prior `_schemas/<source>.json` to the validation bucket via the Supabase Storage dashboard. |
| Retry agent dispatches a workflow that fails for a code reason (infinite retry loop) | The 3-retry-per-30-min budget in-memory guard is the only protection, and it resets on cold start. No documented procedure for "manually blocking retries for a specific workflow." Procedure: `select cron.unschedule('agent-worker-tick');` to halt all agent activity temporarily. |
| Anthropic API key is rotated | Update in Supabase Functions secrets. No redeploy required. Drift detector will start failing with `classifier_error` audit entries until the new key is set. |
| Slack workspace migrates or channel is renamed | The channel ID `C0B1N51L9TN` is env-var controlled (`SLACK_DASHBOARD_ALERTS_CHANNEL`). Update in Supabase Functions secrets. No code change needed — this was correctly implemented. |
| Method adds a new outlet | Two code changes: add to `OUTLETS` array in `anomaly_detector.ts` and `banner_writer.ts`. These are hardcoded, not config-driven. Requires redeploy. A `config/outlets.json` read from Storage in Phase A.2 would fix this. |

---

## Cost estimates

| Component | Calculation | Daily | Monthly |
|---|---|---|---|
| Anthropic (drift, best case: no warnings) | 0 calls | $0 | $0 |
| Anthropic (drift, realistic: 2 sources with warnings) | 2 × 288 ticks × $0.005/call | ~$2.88 | ~$87 |
| Anthropic (drift, worst case: all 7 sources warn) | 7 × 288 × $0.005 | ~$10 | ~$300 |
| Supabase Edge Function invocations | 288/day vs 500K/month free tier | — | Well within free tier |
| Supabase Storage | 7 sources × 1 file/day × 90-day retention | ~630 files, ~5MB | Negligible |
| GitHub PAT API quota | 6 workflows × 3 runs × 288 ticks = 5,184 req/day | — | Within 5K/hr free limit |

**Key cost note:** The spec anticipated a ~$1,500/month scenario based on "6 syncs × 1 cron tick" which is incorrect. The drift detector runs on every cron tick (every 5 min), not once per sync. However, the actual per-call cost ($0.005 vs the spec's $0.03) means the realistic total is $87–300/month, not $1,500. At the low end this is immaterial; at the high end (all sources consistently warning) it's worth a monthly check.

**Recommendation:** Set a Supabase/Anthropic budget alert at $15/day. If triggered, check whether any source has had persistent `rows_warned > 0` for multiple days — that's the signal to fix the underlying data issue rather than let the LLM call it repeatedly.

---

## Recommendations for Phase A.2 hardening

1. **Fix the upload-validation silent fail** before the first real incident: add an empty-key guard and fail the step (not just echo) on upload errors.

2. **Move retry history to Postgres**: replace the in-memory `recentRetries` map in `retry_repair.ts` with a `agent_retry_log` table. One-migration change; eliminates the cold-start budget reset.

3. **Move audit writes to Postgres**: replace the read-modify-write Storage pattern in `audit.ts` with a Postgres `INSERT INTO agent_decisions` table. Eliminates the race condition and makes audit queries trivial.

4. **Config-drive the outlets list**: move `OUTLETS` from hardcoded arrays in `anomaly_detector.ts` and `banner_writer.ts` to `config/outlets.json` (already in the repo pattern). Read at invocation time. Eliminates the need for a redeploy when Method adds a property.

5. **Add a drift-detector cooldown**: skip the Sonnet call if the source was last classified within the past 30 minutes and the validation summary hasn't changed (compare `ran_at` timestamps). This caps LLM calls to ~6/day regardless of how long warnings persist.

6. **Write a one-page agent-worker README** (`supabase/functions/agent-worker/README.md`). Five sections: what it is, prerequisites, how to deploy, how to check it's running, how to disable it.

7. **Upgrade model reference**: `anthropic.ts` hardcodes `claude-sonnet-4-5`. Current production model is `claude-sonnet-4-6`. Non-blocking but worth updating before A.2.
