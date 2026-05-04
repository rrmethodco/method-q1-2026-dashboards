# Phase A.1 — Security Review

> **BLOCK DEPLOY** — one Critical finding (unauthenticated Edge Function endpoint) must be resolved before the Supabase deploy runs.

## TL;DR

The highest-severity issue is `verify_jwt = false` in `supabase/config.toml`: the `agent-worker` endpoint is fully public, enabling anyone to hammer it and drain Anthropic API credits or trigger cascading GitHub workflow dispatches. A second notable issue is that `app.settings.service_role_key` stored as a database-level setting is readable by any role that can call `current_setting()` without a `missing_ok` guard — including the `authenticated` role. Remaining findings are medium: floating semver deps on esm.sh, a handful of missing PII field names in the redactor, and GitHub error bodies being surfaced in the function's HTTP response. Nothing in the code leaks secrets to logs or to the public `banner` bucket.

---

## 🔴 Critical findings (must fix before deploy)

### C-1 — Edge Function has no authentication (`verify_jwt = false`)

**File:** `supabase/config.toml:6`

```toml
[functions.agent-worker]
verify_jwt = false
```

**Threat model:** The endpoint `https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker` is publicly reachable with no token required. Any attacker who finds the URL (it is embedded in the migration file, which is in a public GitHub repo) can:
- Call it in a tight loop, exhausting the Anthropic API quota (each invocation calls `claude-sonnet-4-5` for every source with schema drift).
- Trigger up to 3×6 = 18 GitHub workflow dispatches per window, cloning arbitrary sync runs.
- Force the audit log into an uncontrolled-write loop against Supabase Storage.

**Fix:** Change to `verify_jwt = true` and have the pg_cron job send the service-role JWT it already constructs in `agent_cron.sql`. The service-role key passed in the `Authorization: Bearer` header satisfies Supabase's JWT verification. No other callers exist today.

**Effort:** 15 minutes (one-line config change; cron already sends the bearer token).

---

## 🟠 High findings (fix soon)

### H-1 — `app.settings.service_role_key` visible to `authenticated` role

**File:** `supabase/migrations/20260504000001_agent_cron.sql:4` (operator runbook step)

The runbook instructs: `alter database postgres set "app.settings.service_role_key" = '<KEY>'`. A database-level `SET` makes the value available to any session via `SELECT current_setting('app.settings.service_role_key')`. In Supabase, the `authenticated` role (used by every logged-in app user) can execute arbitrary SQL via the `/rest/v1/rpc/` layer if any function is marked `SECURITY INVOKER` or if the user lands a raw Postgres connection.

**Fix:** Store it as a transaction-level or function-local setting instead, or better, invoke the Edge Function via a Supabase Vault secret rather than a database-level string. At minimum, wrap the cron body in a `security definer` function owned by `postgres` and revoke `EXECUTE` from `authenticated`/`anon`.

**Effort:** 2–4 hours (requires pg_cron + Vault wiring or a wrapper function).

### H-2 — GitHub API error bodies surfaced in HTTP response

**File:** `supabase/functions/agent-worker/lib/github.ts:36,54`

```ts
throw new Error(`gh ${r.status}: ${await r.text()}`);
throw new Error(`dispatch ${r.status}: ${await r.text()}`);
```

These error messages propagate via `catch (e) { result.errors.push(\`retry_repair: ${String(e)}\`) }` in `index.ts:88` and are returned in the HTTP response body (JSON, status 500). GitHub's 4xx error bodies occasionally echo back request details including the workflow filename and, in some auth-failure scenarios, hints about the token. Combined with `verify_jwt = false` (C-1), any caller who triggers a misconfiguration can read these.

**Fix:** Log the full error server-side; return a sanitized message (e.g., `"gh_api_error"`) in the response body. Fix is trivial once C-1 is addressed and verify_jwt is on.

**Effort:** 30 minutes.

---

## 🟡 Medium / observations

### M-1 — Floating semver dependencies on esm.sh CDN

**File:** `supabase/functions/agent-worker/deno.json:7-9`

```json
"@anthropic-ai/sdk": "npm:@anthropic-ai/sdk@^0.30",
"@slack/web-api": "npm:@slack/web-api@^7",
"@supabase/supabase-js": "https://esm.sh/@supabase/supabase-js@2"
```

`^0.30` and `^7` allow minor/patch upgrades to pull in silently on next cold-start. The esm.sh URL (`@2`) is a floating major. A supply-chain compromise of any of these packages would auto-land in production without a redeploy.

**Fix:** Pin to exact versions (`0.30.x`, `7.x.x`, `2.x.x`) or use a Deno lock file (`deno.lock`). This is standard practice for production Deno functions.

**Effort:** 1 hour (update imports + run `deno cache --lock=deno.lock`).

### M-2 — PII redactor missing several field names present in source data

**File:** `toast-etl/validation/pii_redact.py:8-24`

The `PII_FIELDS` dict does not cover:
- `author` — Google review author names are present in `data/lsbr.json` (`guest.google.samples[*].author`), confirmed as real first-name strings (e.g., `"Brandon"`).
- `customer_name` — common Toast/Resy export field name variant not in the list.
- `server` — staff member names appear in Resy survey export (`guest.comments[*].server`).
- `display_name`, `reviewer_name` — common variants from survey/Google APIs.

If a validation error fires on a row that contains `author` or `server`, the `row_redacted` field in `_validation_errors/*.json` will preserve the staff/guest name.

**Fix:** Add `"author": "name"`, `"customer_name": "name"`, `"server": "name"`, `"display_name": "name"`, `"reviewer_name": "name"` to `PII_FIELDS`.

**Effort:** 15 minutes + update test fixtures in `test_pii_redact.py`.

### M-3 — `errors_sample` in validation summary includes `message: str(e)[:500]`

**File:** `toast-etl/validation/runner.py:56`

Pydantic validation error messages can include the field value that failed (e.g., `"value is not a valid email address (type=value_error.email); given: 'john.doe@example.com'"`). This truncated message lands in `_validation/<source>_<ts>.json`, which is uploaded to Supabase Storage (private bucket) and subsequently read by the drift detector. The drift detector only extracts `row_keys`, not `message`, so the LLM never sees it — but the raw summary file in Storage retains it. Medium risk: the `validation` bucket is private, but the service-role key that writes to it is the same one used for all operations.

**Fix:** Strip or hash the field *value* portion from Pydantic error messages before storing, keeping only the field path and error type.

**Effort:** 1–2 hours.

### M-4 — Hardcoded Slack channel ID in source

**File:** `supabase/functions/agent-worker/agents/alert_dispatcher.ts:19`

`"C0B1N51L9TN"` is a real Slack channel ID committed to the repo. Not a secret per se, but Slack channel IDs in public repos enable targeted phishing and channel-enumeration probes. Low severity, but worth moving to the env-var default only (no hardcoded fallback).

**Effort:** 5 minutes.

---

## ✅ Things done right

- **Secrets never logged.** `Deno.env.get()` is called only to initialize clients; no `console.log` of key values anywhere in `agent-worker/`.
- **`banner` bucket content is safe for public exposure.** `banner_writer.ts` writes only `{ outlet, worst_class, message, updated_at }` — no raw row data, no error messages, no PII.
- **`validation` and `audit` buckets are private.** Correct bucket config in `20260504000000_validation_bucket.sql`.
- **PII redaction is recursive and applied before storage.** `runner.py` calls `redact_pii(row)` on `row_redacted` and the redactor recurses into nested dicts/lists.
- **Drift detector sends only field-name keys to Anthropic, not values.** `drift_detector.ts:88` maps sample rows to `{ key: "..." }` before the LLM call — data values never leave the Supabase environment.
- **Retry/repair dispatch uses a fixed workflow filename allowlist.** `dispatchWorkflow` is only called with values from the `WORKFLOWS` const array; no user-controlled or external input reaches the dispatch call.
- **GH Actions action.yml correctly uses env vars for the service-role key.** GH Actions auto-redacts values of secrets (`${{ secrets.* }}`) in logs; the curl command does not echo the key.
- **No SQL injection surface in migrations.** Both migration files are fully static with no dynamic string interpolation.

---

## Recommendations for Phase A.2/B

1. **Rotate service-role key out of `pg_cron` invocation.** Use Supabase Vault to store the key and reference it via `vault.decrypted_secrets`, preventing it from being visible via `current_setting()`.
2. **Add a rate-limit wrapper (Edge Function middleware) or Supabase API gateway rule** — even with `verify_jwt = true`, the pg_cron caller already has the service-role JWT, so a stolen JWT would still allow abuse. A max-calls-per-minute rule on the function adds defense-in-depth.
3. **Lock the Deno import map to a `deno.lock` file** as part of the CI build step to guarantee reproducible deploys.
4. **Extend PII redaction coverage** to all sources (Resy, Google Reviews, Tripleseat contact fields) with a test matrix against the actual export headers before those syncs are enabled.
5. **Introduce an audit bucket read-access policy** — currently there is no RLS policy preventing the `authenticated` role from downloading `audit/agent_decisions.jsonl`. Add `storage.objects` RLS or a bucket policy that restricts reads to `service_role` only.
