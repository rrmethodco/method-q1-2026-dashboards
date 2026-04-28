# Session Log — 2026-04-28 (overnight build)

_Ross stepped away ~9:00 PM ET with the brief: "complete this dashboard, include all toast revenue and labor data for all locations, all guest surveys from resy. investigate api and data collection from MarginEdge. Within the dashboard create a weekly Snapshot report exportable via PDF that highlights KPIs and prescribes action items."_

## What landed tonight

| # | Title | Branch / PR | Status |
|---|---|---|---|
| 1 | feat(guest): Guest Experience tab — Resy surveys + Google Reviews | [#8](https://github.com/rrmethodco/method-q1-2026-dashboards/pull/8) | merged |
| 2 | fix(guest): correct lookup hints for vessel, rosemary_rose, anthology | [#9](https://github.com/rrmethodco/method-q1-2026-dashboards/pull/9) | merged |
| 3 | feat(snapshot): Weekly Snapshot section + PDF export + Resy OS scraper + toast-sync race fix + memos | `feat/weekly-snapshot` | open PR (see below) |

Commits on `main` from the session:
- `87d2187` chore(data): nightly guest sync 2026-04-28T20:58Z (correct Google Places mappings)
- `9cb130e` fix(guest): correct lookup hints for vessel, rosemary_rose, anthology (#9)
- `1dd6189` feat(guest): Guest Experience tab — Resy surveys + Google Reviews (#8)

## Data audit findings

Coverage as of session start (post-PR #8, before tonight):

| Source | Coverage |
|---|---|
| Toast revenue | 11/11 outlets |
| **Toast labor** | **0/11 outlets** ← gap |
| Resy seed surveys | 6/11 outlets (5,646 surveys) |
| Google Reviews | 11/11 outlets ✓ |

**Root cause of the labor gap**: the latest manual toast-sync run at 21:03Z on 2026-04-28 successfully pulled labor for all outlets (~50k+ time entries across the portfolio), generated a 672,774-line commit, and then the `git push` was rejected because guest-sync had committed in between. Toast-sync workflow had no rebase-and-retry — the labor commit was lost.

**Fix in this session's PR**: `toast-sync.yml` now does up-to-3 push attempts with `git pull --rebase origin main` between failures. The next nightly cron (03:15 ET) will land the labor data.

## Weekly Snapshot — design + behavior

New 6th nav section: **Weekly Snapshot**. One-page operator-ready report with:

- **8 KPI cards** in two rows: Net Sales, Guests, Orders, Avg Check / Labor %, Surveys (Resy), NPS, Google ★
- Each card shows WoW delta and (where applicable) STLY delta
- **Wins / Concerns** auto-surfaced from period numbers (revenue swings, labor% out-of-band, NPS extremes, Google rating < 4.0)
- **Action Items for Operators** — prescriptive engine with these rules:
  - Net sales WoW ≤ -10% → "Investigate covers, weather, service disruption — flag to GM"
  - YoY ≤ -15% → "Loop in Dragonfly Strategies"
  - WoW ≥ +15% → "Replicate the win — document driver"
  - Labor % > 32% → "Tighten Tue-Thu coverage"
  - Labor % < 22% → "Confirm guest service / ticket times"
  - OT % of hours > 8% → "Audit schedule for back-to-back closes"
  - NPS < 30 (with n≥5) → "Pull comments, review with FOH leadership"
  - Food avg < 80 → "Schedule kitchen tasting + recipe audit"
  - Service avg < 85 → "Pre-shift on table touches"
  - Google ★ < 4.0 → "GM response-rate push for negative reviews"
  - Owner reply rate < 50% → "Clear backlog this week"
- **Period comparison table** — Net Sales, Orders, Guests, Labor $/Hours/% (when available), Surveys, NPS — with this-week / prior-week / WoW% / STLY / YoY% columns
- **Week picker** in the header — last 12 completed Mon-Sun weeks, defaults to most recent
- **"Export PDF" button** triggers `window.print()` — `@media print` stylesheet hides nav/controls/footer, sets letter-portrait margins, forces page breaks between sections, preserves Method palette via `print-color-adjust: exact`

Verified via Playwright on `lsbr` (8 KPIs, 2 concerns, 2 action items render cleanly with brand palette). Tested rewinding to 3/9–3/15 — surveys/NPS hydrate (22 surveys, NPS +68).

## MarginEdge investigation

Memo at [docs/MARGIN_EDGE_INTEGRATION.md](MARGIN_EDGE_INTEGRATION.md). TL;DR:

- **MarginEdge HAS a Public REST API** (read-only, included in subscription)
- Exposes: invoices (with line items), products, vendors, vendor items, categories — sufficient for **weekly COGS % of net sales by outlet**
- NOT in API: inventory, recipes, theoretical food cost (UI/CSV only)
- **Per-restaurant scoping** — one auth grant per outlet (fits existing per-outlet JSON architecture)
- Auth model + rate limits not publicly documented; the dev portal SPA is behind Cloudflare
- **Recommended next step**: email Method's MarginEdge CSM and ask for the per-location auth flow + sample creds for one ROOST outlet as a pilot, plus rate limits, plus whether nightly SFTP delivery is available for non-accounting endpoints

Once you have credentials, this slots in cleanly: new `marginedge_sync.py` ETL alongside `toast_sync.py` and `resy_os_scraper.py`, writing `purchases` / `cogs_weekly` blocks into `data/<outlet>.json`.

## Resy OS scraper — built, awaiting auth bootstrap

Memo at [docs/RESY_OS_SCRAPER.md](RESY_OS_SCRAPER.md). The consumer Resy API (`/3/auth/password`) returns HTTP 419 against operator credentials, confirming what we suspected — operator accounts can't auth via the consumer endpoint.

Built tonight (Playwright-based, ready for auth bootstrap):

- `tools/refresh_resy_storage.py` — interactive helper (opens Chromium, you log in manually, exports `resy-storage-state.json`)
- `toast-etl/resy_os_scraper.py` — headless replay scraper with discovery mode, healthcheck for expired sessions, and append-merge into existing `guest` blocks
- `.github/workflows/guest-sync.yml` — adds the OS scraper alongside the existing Resy step; both gated on whether their respective secrets are set

**Bootstrap procedure (your one-time, ~2 minutes):**

```bash
pip install playwright
python -m playwright install chromium
python tools/refresh_resy_storage.py    # log in, press Enter
gh secret set RESY_OS_STORAGE_STATE_JSON < resy-storage-state.json
gh secret set RESY_OS_VENUES --body "lsbr=det/le-supreme;lowland=chs/lowland;hiroki_phl=pha/hiroki;hiroki_det=det/hiroki-san;kampers=det/kampers;mulherins=pha/wm-mulherins-sons;quoin=wld/the-quoin;rosemary_rose=chs/rosemary-rose;vessel=bal/vessel-md;anthology=det/anthology;little_wing=bal/little-wing"
```

Then trigger discovery mode to see what the scraper finds:

```bash
gh workflow run guest-sync.yml -f probe=true
```

The scraper logs every JSON XHR it captures; review the discover output and lock down the right URL pattern in `CANDIDATE_URL_PATTERNS` if needed.

**Refresh cadence**: every ~21 days, or whenever the nightly run's healthcheck reports 0 surveys for >2 venues. Set a calendar reminder.

**Maintenance budget**: ~2 hrs/quarter expected (XHR routes change occasionally). Memo has the full rationale.

## Discrepancies surfaced + fixed

Brought to surface during the Google Places lookup, all confirmed with Ross:

| Outlet | Was | Should be |
|---|---|---|
| Lowland | "Philadelphia" (per CLAUDE.md) | **Charleston, SC** — 36 George St (slug `chs/lowland`) |
| Vessel | "Detroit" (per script hints) | **Baltimore** — private event venue at ROOST Baltimore, 2460 Terrapin Wy (slug `bal/vessel-md`) |
| Little Wing | (not in CLAUDE.md) | **Baltimore** — ground-floor coffee shop at ROOST Baltimore, same building as Vessel |
| Rosemary & Rose | "Philadelphia" (per script hints) | **Charleston, SC** — 529 King St (slug `chs/rosemary-rose`) |

CLAUDE.md updated tonight (the global one at `~/.claude/CLAUDE.md`).

## What's still open

| Item | Where it stands |
|---|---|
| Toast labor data on `main` | Will land on next 03:15 ET nightly cron (race fix in PR will prevent recurrence) |
| Resy OS scraper auth | Waiting on Ross's `tools/refresh_resy_storage.py` bootstrap |
| MarginEdge | Waiting on Ross's email to ME CSM for credentials |
| `orderMeta` null-deref pre-existing bug | Still not addressed (HANDOFF §7) |
| Inline `order_details` in HTML | Still bake-stepped; runtime loader has been live ~24hr — safe to retire |
| Off-brand standalone dashboards | 6 still using Inter font / Tailwind blues |
| `index.html` is the Huddle pitch deck | Not a portfolio page — still worth deciding on |

## Files of interest (added/changed tonight)

| Path | Purpose |
|---|---|
| `Method_Co_FB_Performance_Dashboard.html` | + Weekly Snapshot section (~370 lines: nav, dispatcher, render fn, print CSS) |
| `toast-etl/resy_os_scraper.py` | New Playwright-based Resy OS scraper |
| `tools/refresh_resy_storage.py` | Interactive auth-state refresher |
| `.github/workflows/guest-sync.yml` | + Resy OS scraper steps (gated on secret) |
| `.github/workflows/toast-sync.yml` | + push rebase-and-retry |
| `docs/MARGIN_EDGE_INTEGRATION.md` | API integration memo |
| `docs/RESY_OS_SCRAPER.md` | Scraper architecture memo |
| `docs/SESSION_2026-04-28-overnight.md` | This file |

## Tomorrow morning, in priority order

1. **Merge the open PR** → labor data + Snapshot live on production
2. **Bootstrap Resy OS scraper auth** — 2 min, see procedure above
3. **Email MarginEdge CSM** — request per-location auth flow for one ROOST outlet pilot
4. Open the Snapshot in Chrome at the live URL, hit "Export PDF" once to verify the print stylesheet rendered properly on the staging branch
