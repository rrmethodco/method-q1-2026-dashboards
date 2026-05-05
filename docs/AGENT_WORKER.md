# Agent Worker — Operator Runbook

> **Status:** Phase A.1 deployed 2026-05-05 (PRs #90/#91/#92/#93/#94 merged). Cross-source reconciler added 2026-05-05 (PR #96).

The agent worker is a Supabase Edge Function (`supabase/functions/agent-worker/`) that runs every 5 minutes via `pg_cron`. It implements **6 agents** that monitor the data validation pipeline:

| Agent | What it does |
|---|---|
| **drift_detector** | Diffs latest sample row keys vs stored schema. LLM-classifies as `stable` / `additive_non_breaking` / `breaking`. Auto-applies additive changes; alerts on breaking. |
| **anomaly_detector** | Per-outlet × metric × DOW rolling ±3σ on net sales + covers. **Shadow mode until 2026-05-18** (logs to audit only). |
| **retry_repair** | Polls cancelled/failed sync workflow runs. Auto-dispatches retries (max 3 per 30-min window). Alerts on exhausted budget. |
| **cross_source_reconciler** | Internal: `order_details.amount/net_sales` ratio sanity bounds [0.90, 1.50]. External: `order_details.net_sales` vs `sales_summary.net_sales` drift threshold ±5%. Catches the +20-30% Net Sales inflation Ross spotted manually 2026-05-05 — that class of bug now triggers a Slack alert automatically. |
| **alert_dispatcher** | Routes events from drift / anomaly / retry / reconciler to Slack channel `C0B1N51L9TN`. 60-min dedup. |
| **banner_writer** | Computes per-outlet `worst_class` (ok/warn/err) from validation summaries. Writes to public `banner` bucket; dashboard fetches. |

Each cron tick produces ~10-25 audit decisions logged to `audit/agent_decisions.jsonl` in Supabase Storage.

---

## Architecture diagram

```
[GH Actions sync workflow]
  ├─ runs sync (Toast/Resy/MarginEdge/etc.)
  ├─ Pydantic runner validates rows
  ├─ Writes data/_validation/<source>_<ts>.json (locally + git commit)
  └─ Composite action curls JSON to Supabase Storage `validation` bucket
                          ↓
                   [validation/ bucket]
                          ↓
[pg_cron: every 5 min]
  └─ HTTP GET https://...supabase.co/functions/v1/agent-worker
                          ↓
[Edge Function: agent-worker]
  ├─ drift_detector            — Anthropic Sonnet classifies diffs
  ├─ anomaly_detector          — pure stats, fetches GH Pages JSON
  ├─ retry_repair              — GitHub API dispatches retries
  ├─ cross_source_reconciler   — amount/net_sales ratio + sales_summary drift
  ├─ alert_dispatcher          — Slack chat.postMessage
  ├─ banner_writer             — writes per-outlet to `banner` bucket
  └─ appendAudit               — writes to `audit/agent_decisions.jsonl`
                          ↓
[Method Co dashboard] (GH Pages) fetches:
  - Outlet payload's _validation_index (committed by runner)
  - Public banner state from Supabase Storage (written by agent)
```

---

## Initial Deploy (Ross's morning checklist)

### Pre-deploy (must be done before commands below)

- [ ] Anthropic API key created → `ANTHROPIC_API_KEY` set as Supabase Edge Function secret
- [ ] GitHub fine-grained PAT created → `GITHUB_PAT` set as Supabase secret
- [ ] Slack app created + bot installed in Method workspace
- [ ] Slack bot invited to channel `C0B1N51L9TN`: `/invite @<botname>`
- [ ] Bot OAuth token (`xoxb-...`) → `SLACK_BOT_TOKEN` set as Supabase secret
- [ ] `SLACK_DASHBOARD_ALERTS_CHANNEL=C0B1N51L9TN` set as Supabase secret
- [ ] Service role key → store in Supabase Vault via SQL editor (Supabase doesn't allow `ALTER DATABASE postgres SET ...` — error 42501; Vault is the canonical replacement):
  ```sql
  select vault.create_secret(
    '<SERVICE_ROLE_KEY>',
    'agent_worker_service_role_key',
    'Used by pg_cron to invoke the agent-worker Edge Function'
  );
  ```
  The pg_cron migration (`20260504000001_agent_cron.sql`) reads from `vault.decrypted_secrets where name = 'agent_worker_service_role_key'`, so the secret name is fixed.
- [ ] Same service role key → set as `SUPABASE_SERVICE_ROLE_KEY` GitHub Actions secret
- [ ] `SUPABASE_URL=https://mmwislzsgnjxjxssynwm.supabase.co` set as GitHub Actions secret

### Deploy commands

```bash
cd /path/to/method-q1-2026-dashboards

# 1. Link Supabase CLI to method-kpi
npx supabase link --project-ref mmwislzsgnjxjxssynwm

# 2. Push migrations (creates Storage buckets + pg_cron schedule)
npx supabase db push
# Expected output:
#   Connecting to remote database...
#   Applying migration 20260504000000_validation_bucket.sql...
#   Applying migration 20260504000001_agent_cron.sql...
#   Local database is up to date.

# 3. Deploy the Edge Function (verify_jwt is ON — see config.toml; pg_cron
#    sends the service-role JWT, so authenticated calls work; public hits
#    are blocked).
npx supabase functions deploy agent-worker
# Expected output:
#   Deployed Function agent-worker on project mmwislzsgnjxjxssynwm
#   Function URL: https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker
```

### Smoke verification (run after deploy)

```bash
# 1. Edge Function reachable + JWT-protected
# (auth header is REQUIRED — verify_jwt is ON to prevent public abuse)
curl -i "https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker" \
  -H "Authorization: Bearer <YOUR_SERVICE_ROLE_KEY>"
# Expected: 200 OK with JSON { status: "ok", agents_invoked: [...] }
#
# Sanity-check the auth gate is working — this should return 401:
curl -i "https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker"
# Expected: 401 Unauthorized (or similar — body says "Missing authorization header")

# 2. Storage buckets exist
# Visit: https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets
# Should see: validation, banner, audit

# 3. pg_cron scheduled
# Visit: https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/sql/new
# Run: select * from cron.job where jobname = 'agent-worker-tick';
# Expected: 1 row with schedule '*/5 * * * *'

# 4. Validation upload working (kick a manual sync)
gh workflow run toast-sync.yml --ref main
# Wait ~5 min for completion, then visit:
# https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets/validation
# Should see: toast_order/ and toast_time_entry/ folders with timestamped files

# 5. Drift detector ran (check audit log)
# Visit: https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets/audit
# Download agent_decisions.jsonl
# Expected: lines with agent="drift_detector" and decision="seeded_initial_schema"

# 6. Slack alert smoke (optional)
# Force a test alert by manually invoking the function with a synthetic event,
# OR wait for an actual hard-fail. Or simpler: post a manual test message:
# curl -X POST https://slack.com/api/chat.postMessage \
#   -H "Authorization: Bearer <SLACK_BOT_TOKEN>" \
#   -H "Content-Type: application/json; charset=utf-8" \
#   -d '{"channel":"C0B1N51L9TN","text":"agent-worker smoke test"}'
```

---

## Operations

### Disable the agent worker (temporarily)

```sql
-- Stops pg_cron from invoking the function. Edge Function stays deployed.
select cron.unschedule('agent-worker-tick');
```

To re-enable, re-apply the migration:
```bash
npx supabase db push
```

### Flip anomaly detector out of shadow mode

Default shadow window: until **2026-05-18**. After that, anomalies fire Slack alerts (subject to dedup).

To override (extend or shorten the shadow window), set `ANOMALY_SHADOW_UNTIL` as an Edge Function secret:
```bash
npx supabase secrets set ANOMALY_SHADOW_UNTIL=2026-06-01T00:00:00Z
# Or to disable shadow mode entirely (alerts fire immediately):
npx supabase secrets set ANOMALY_SHADOW_UNTIL=2020-01-01T00:00:00Z
```

### Rotate the Anthropic API key

```bash
# 1. Create new key at console.anthropic.com
# 2. Update Supabase secret:
npx supabase secrets set ANTHROPIC_API_KEY=<new-key>
# 3. Revoke old key at console.anthropic.com
```

### Add a new outlet to the system

When Method opens a new restaurant, add the outlet ID to:

1. `supabase/functions/agent-worker/agents/anomaly_detector.ts` — `OUTLETS` array
2. `supabase/functions/agent-worker/agents/banner_writer.ts` — `OUTLETS` array
3. `forecast_engine.py` — `SUPA_UUID_TO_OUTLET` (if helixo-2 forecasts the new outlet)
4. Re-deploy: `npx supabase functions deploy agent-worker`

### Slack channel rename / new channel

If you change channels, update the Supabase secret:
```bash
npx supabase secrets set SLACK_DASHBOARD_ALERTS_CHANNEL=<new-channel-id>
```

Channel IDs are stable across renames in Slack — only changes if you migrate the bot to a different channel entirely. Bot must be invited to the new channel.

---

## Failure-mode debugging

### Edge Function silently not running

**Check pg_cron actually fires:**
```sql
select * from cron.job_run_details
where jobname = 'agent-worker-tick'
order by start_time desc limit 10;
```

If no recent rows, the `pg_cron` job is broken. Common causes:
- `app.settings.service_role_key` not set (verify with `select length(current_setting('app.settings.service_role_key'))`)
- `pg_net` extension not enabled (`create extension if not exists pg_net;`)

### Edge Function 500ing

**Check function logs:**
- https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/functions/agent-worker/logs

Common causes:
- Missing secret (`ANTHROPIC_API_KEY` / `GITHUB_PAT` / `SLACK_BOT_TOKEN`) — function returns 500 with the error in the response body
- Anthropic API rate limit — drift detector throws; other agents still run; should auto-recover next cycle
- Slack `chat.postMessage` failing — usually means bot was uninstalled or removed from channel

### Drift detector auto-applied a wrong schema change

The drift detector auto-updates `_schemas/<source>.json` on `additive_non_breaking` classifications. If the LLM was wrong, the dashboard would start accepting corrupted shapes. To roll back:

1. Open https://supabase.com/dashboard/project/mmwislzsgnjxjxssynwm/storage/buckets/validation
2. Navigate to `_schemas/<source>.json`
3. The schema file has a `prior` key with the previous state — manually upload a fixed version

### Retry/repair stuck in retry loop

If a workflow is failing for code reasons (not infrastructure), the agent will retry up to 3 times in 30 min then alert. To stop retries earlier:

```sql
-- Disable the cron temporarily
select cron.unschedule('agent-worker-tick');
-- Fix the underlying workflow code, push to main
-- Re-enable
-- (re-apply migration or paste the cron.schedule(...) call manually)
```

---

## Observability checklist

| Signal | Where to find |
|---|---|
| Audit log (every agent decision) | Supabase Storage → `audit/agent_decisions.jsonl` |
| Validation summaries (per source per run) | Supabase Storage → `validation/<source>/` |
| Stored schemas (drift detector reference) | Supabase Storage → `validation/_schemas/<source>.json` |
| Banner state per outlet | Supabase Storage → `banner/<outlet>.json` |
| Cron run history | `select * from cron.job_run_details order by start_time desc;` |
| Edge Function logs | Supabase dashboard → Functions → agent-worker → Logs |
| Slack alerts | Slack channel `C0B1N51L9TN` |
| GH Actions sync run history | https://github.com/rrmethodco/method-q1-2026-dashboards/actions |
| Dashboard validation panel | Top-right of every outlet view |

---

## Cost estimates

| Component | Estimated monthly cost |
|---|---|
| Anthropic API (drift detector — Sonnet) | ~$30–80/mo (depends on drift event frequency) |
| Supabase Edge Function invocations | $0 (within free tier — 8,640/mo well below 500K cap) |
| Supabase Storage | $0 (a few KB total — well below 1GB free tier) |
| GitHub PAT API calls | $0 (well within 5K/hr free tier) |

**Total: ~$50/mo all-in for Phase A.1 operational layer.**

---

## Related docs

- **Spec:** `docs/superpowers/specs/2026-05-04-trustworthy-reporting-engine-design.md`
- **Plan:** `docs/superpowers/plans/2026-05-04-trustworthy-reporting-engine-phase-a1.md`
- **Reviews (overnight):** `docs/superpowers/reviews/2026-05-04-*-review.md`
- **Forecast audit (separate):** `docs/forecast-audit/`
