# Resy OS Scraper — Architecture Memo

_Prepared 2026-04-28 for Ross Richardson (EVP F&A, Method Co.)_

## 1. Why a scraper, not the API

The consumer Resy API (`api.resy.com /3/auth/password`) returns **HTTP 419 Unauthorized** when given Resy OS operator credentials. Operator accounts live in a separate identity store with MFA / device-binding / CSRF gates the consumer endpoint refuses.

`toast-etl/resy_sync.py` (the existing API path) cannot work for our use case. The replacement is a **Playwright-based scraper** that runs against the actual `os.resy.com` SPA using stored auth state.

## 2. Auth flow (what we know)

- Resy was acquired by American Express (2019) but `os.resy.com` does not use AmEx corporate SSO.
- The OS billing portal (`billing.resyos.com`) is Salesforce-hosted — a separate identity surface.
- The main `os.resy.com` app uses bespoke Resy-issued session tokens in headers (`Authorization: ResyAPI api_key=...`, `X-Resy-Auth-Token`, `X-Resy-Universal-Auth`) plus cookies.
- HTTP 419 on the consumer endpoint is consistent with operator accounts requiring CSRF tokens / operator scope / device binding the consumer flow doesn't grant.

## 3. Replay-from-storageState is feasible

Resy OS is a SPA driven by header tokens + cookies, not TLS-fingerprint or hardware-bound auth. Playwright `storageState()` captures cookies + localStorage + IndexedDB and replays cleanly across headless runs.

**Risks to test before committing**:

- Cloudflare/Datadog bot scoring may flag a vanilla headless Chromium → mitigate with `playwright-extra` + stealth plugin or by mirroring the seed session's UA / viewport / timezone.
- Some SPAs rotate refresh cookies every N hours → verify by replaying a 7-day-old `storageState.json` once before locking in the architecture.

## 4. Recommended architecture

**Hybrid extraction**: Playwright drives the real browser context (handles auth), but inside the authenticated context the script captures the SPA's own `fetch()` calls (network listener) and persists those JSON responses. Don't parse DOM unless JSON is obfuscated. This is the most stable pattern — auth is browser-managed, data is JSON.

**Stack**:

- Playwright Python (`playwright`, `playwright-stealth`)
- Headless Chromium with seeded `storageState.json`
- Output: append-merge into `data/<outlet>.json` under `guest.surveys`, `guest.ratings`, `guest.comments` (same schema the dashboard already consumes)
- Healthcheck: fail loudly if 0 surveys returned for >2 venues (silent breakage is the real risk)

**CI**:

- GitHub Actions nightly cron (~03:30 ET)
- `RESY_OS_STORAGE_STATE_JSON` stored as encrypted secret
- Auth refresh cadence: **every 21 days** (conservative; cookies on `os.resy.com` likely expire 14–30 days)
- Bootstrap runbook: log in locally, run `tools/refresh-resy-storage.py`, paste the JSON into `gh secret set` (one-command, ~60 seconds)

**Maintenance budget**: assume 1–2 breakages per quarter (renamed XHR routes, new CSRF headers, occasional Cloudflare changes). Industry baseline for operator-side restaurant-SaaS scrapers is 4–8 hrs/month maintenance. Plan for ~2 hrs/quarter on this one specifically.

## 5. What will break it

- Resy renames the surveys / ratings XHR endpoint
- Resy adds device-fingerprint binding to operator sessions
- Resy deploys Cloudflare Turnstile / arkose to operator login
- Resy rotates session-cookie lifetime below 21 days
- Resy ToS changes to explicitly prohibit scraping (would force a re-evaluation; SevenRooms ToS already does this for their app)

If Resy ever ships a real partner API for ratings, **retire this scraper** rather than maintain in parallel.

## 6. Existing OSS

**No public scrapers target `os.resy.com`.** All 8 GitHub repos under topic `resy` target consumer reservation booking on `resy.com` (Alkaar/resy-booking-bot, evanpurkhiser/resyctl, lgrees/resy-cli). Operator-side scrapers aren't published — narrow audience and Resy ToS likely discourages it. We're building this in-house.

## 7. Bootstrap (Ross's one-time action)

1. Install Playwright locally: `pip install playwright && playwright install chromium`
2. Run `python tools/refresh-resy-storage.py` (provided in this PR's scaffold)
3. The script opens a real Chromium window → log into Resy OS manually → press Enter in the terminal
4. Script writes `resy-storage-state.json` to disk
5. Set as GitHub Secret: `gh secret set RESY_OS_STORAGE_STATE_JSON < resy-storage-state.json`
6. Repeat every ~21 days when the nightly run starts failing the healthcheck

## Sources

- [Resy OS login portal](https://os.resy.com/portal/login)
- [Resy OS billing portal (Salesforce)](https://billing.resyos.com/login)
- [Reversing Resy's API — Seanwiryadi (Medium)](https://medium.com/@seanwiryadi16/the-secret-menu-of-tech-my-encounter-with-an-undisclosed-resy-endpoint-ce46059dcdf0)
- [Alkaar/resy-booking-bot — auth header reference](https://github.com/Alkaar/resy-booking-bot/issues/141)
- [Playwright authentication docs](https://playwright.dev/docs/auth)
- [SevenRooms ToS (anti-scraping clause)](https://sevenrooms.com/terms-of-service/)
- [Resy Surveys helpdesk](https://helpdesk.resy.com/resy-surveys-S1qQMwX8u)
- [American Express acquires Resy (2019)](https://blog.resy.com/newsroom/american-express-aims-to-expand-digital-dining-access-experiences-with-acquisition-of-resy-reservation-platform/)
