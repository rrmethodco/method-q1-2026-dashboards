# Guest-experience scrapers — local setup

Two daily scrapers feed the Guest Experience tab of `Method_Co_FB_Performance_Dashboard.html`:

| Source | Path | Schedule | Where it runs |
|---|---|---|---|
| **Resy survey CSVs** | `tools/resy_csv_sync.py` | 06:00 local, daily | Mac mini (launchd) |
| **Google reviews (full)** | `tools/google_gbp_reviews_sync.py` | 06:30 local, daily | Mac mini (launchd) |
| Google Places sample (legacy) | `toast-etl/google_reviews_sync.py` | 12:15 UTC, daily | GitHub Actions |
| Resy survey API (legacy, broken) | `toast-etl/resy_os_scraper.py` | 12:15 UTC, daily | GitHub Actions — disable after Resy local sync proven |

The legacy GitHub Actions Resy path stopped returning numeric ratings around 2026-04-17 (recommend / food / service / atmos / sentiment all came back null even though guests were still answering). Resy OS's **CSV export still includes everything**, which is why we moved Resy to a local launchd job that drives the portal directly.

The Google legacy path returns only the 5 most-helpful reviews per venue (Places API limit). The Business Profile API returns full history but requires venue-by-venue OAuth ownership claim.

---

## Resy local sync

### One-time setup (~5 min)

```bash
cd "/Users/rossrichardson/CLAUDE PROJECTS/method-q1-2026-dashboards"

# 1. Reseed the storage state if you don't already have one locally
python3 tools/refresh_resy_storage.py
# → opens Chromium, log in to os.resy.com, press Enter when on portal

# 2. Run the installer (creates dirs, templates plist, loads launchd agent)
bash tools/install-resy-sync.sh

# 3. Run once manually to verify
launchctl start com.methodco.resy-sync
tail -F ~/Library/Logs/method-resy-sync.log
```

### Daily operation

The launchd agent fires every day at 06:00 local time. It:

1. `git pull --rebase origin main` (absorbs nightly autocommits)
2. Runs `tools/resy_csv_sync.py`:
   - Headless Chromium opens, replays the stored Resy session
   - For each venue, navigates to `analytics/Surveys`, clicks **Export**
   - Saves the CSV to `~/Documents/Method/resy-csvs/<YYYY-MM-DD>/<outlet>.csv`
   - Parses each CSV → merges rows into `data/<outlet>.json` under `guest.surveys`
3. If `data/*.json` changed, commits + pushes (rebase-and-retry against concurrent syncs)

CSV files are kept indefinitely in date-stamped folders. Audit any day's raw export at `~/Documents/Method/resy-csvs/YYYY-MM-DD/`.

### Maintenance

| Failure | Symptom | Fix |
|---|---|---|
| Storage state expired (~21 day cadence) | log shows `0 surveys captured` for every venue | `python3 tools/refresh_resy_storage.py && cp resy-storage-state.json ~/.config/method-dashboards/ && rm resy-storage-state.json` |
| Export selector drift | log shows `no working Export selector` | edit `EXPORT_SELECTORS` in `tools/resy_csv_sync.py` |
| CSV column drift | log shows `0 parseable rows (header mismatch?)` | inspect the saved CSV headers, update `col_match` calls in `parse_resy_csv` |
| Mac asleep at 06:00 | nothing in log for that day | macOS launchd runs missed jobs on next wake — usually self-heals; or run `launchctl start com.methodco.resy-sync` manually |

### Disable

```bash
launchctl unload ~/Library/LaunchAgents/com.methodco.resy-sync.plist
```

---

## Google Business Profile reviews

### One-time setup (~30 min, mostly Google Cloud Console)

**1. Create / pick a Google Cloud project**
- https://console.cloud.google.com
- New Project → "method-dashboards" (or reuse one)

**2. Enable APIs**
Open https://console.cloud.google.com/apis/library and enable:
- **My Business Account Management API** (account/location list)
- **My Business Business Information API** (location details)
- **My Business Notification API** *(optional — for new-review push)*

**3. Apply for Reviews API access**
The reviews endpoint lives on the legacy `mybusiness.googleapis.com/v4/...` host which is gated behind an application form:
- https://developers.google.com/my-business/content/prereqs#request-access
- Fill out: project ID, intended use ("internal F&B operations dashboard, ~11 owned restaurants/hotels, daily review pulls"), contact email
- Approval is typically 3–7 business days

**4. Configure OAuth consent screen**
- APIs & Services → OAuth consent screen
- User type: **External**
- App name: "Method Dashboards"
- Scopes: add `https://www.googleapis.com/auth/business.manage`
- Test users: add `rr@methodco.com` (and any other operator who will run `--auth`)

**5. Create OAuth credentials**
- APIs & Services → Credentials → Create → OAuth client ID
- Application type: **Desktop app**
- Download the JSON → rename to `gbp-client.json` → place at `~/.config/method-dashboards/gbp-client.json`

**6. Run the auth flow**
```bash
cd "/Users/rossrichardson/CLAUDE PROJECTS/method-q1-2026-dashboards"
pip3 install google-auth-oauthlib requests
python3 tools/google_gbp_reviews_sync.py --auth
# → opens browser, sign in with the methodco.com account that has GBP
#   manager access on the venue listings, click "Allow"
# → token stored at ~/.config/method-dashboards/gbp-token.json
```

**7. Map outlet IDs to GBP location resource names**
```bash
python3 tools/google_gbp_reviews_sync.py --list-accounts
# Prints all accounts/locations the operator can access. Example:
#   Account: accounts/12345  (Method Co)
#     accounts/12345/locations/67890   Le Suprême — 1265 Washington Blvd, Detroit
#     accounts/12345/locations/67891   Bar Rotunda — 1265 Washington Blvd, Detroit
#     ...
```
Then write `~/.config/method-dashboards/gbp-locations.json`:
```json
{
  "lsbr":           "accounts/12345/locations/67890",
  "lowland":        "accounts/12345/locations/12345",
  "mulherins":      "accounts/12345/locations/...",
  "kampers":        "accounts/12345/locations/...",
  "hiroki_det":     "accounts/12345/locations/...",
  "hiroki_phl":     "accounts/12345/locations/...",
  "quoin":          "accounts/12345/locations/...",
  "rosemary_rose":  "accounts/12345/locations/...",
  "vessel":         "accounts/12345/locations/...",
  "anthology":      "accounts/12345/locations/...",
  "little_wing":    "accounts/12345/locations/..."
}
```

Locations Method doesn't have manager access to: leave out of the JSON; those venues keep using the 5-sample Places API path.

**8. Test**
```bash
python3 tools/google_gbp_reviews_sync.py --outlet lsbr
# → +N reviews → total N
```

**9. Schedule** *(after the first manual run lands cleanly — same launchd pattern as Resy, separate plist file)*
TODO once Google Cloud access is granted; deferred until OAuth + Reviews API access are confirmed working.

### Daily operation (post-setup)

Same shape as Resy: pulls full review history per venue, dedups by `review_id`, writes to `data/<outlet>.json` under `guest.google.reviews[]`. The dashboard renderer prefers `.reviews` when present, falls back to the 5-sample `.samples` for unmapped venues.

### Maintenance

| Failure | Symptom | Fix |
|---|---|---|
| Refresh token revoked | `invalid_grant` on next run | re-run `python3 tools/google_gbp_reviews_sync.py --auth` |
| Reviews API access denied | `403` for every location | apply for v4 access (step 3 above), check approval status |
| New venue claimed | shows under `--list-accounts` but not in dashboard | add entry to `gbp-locations.json` |

---

## Storage state files

Both scrapers write tokens/sessions to `~/.config/method-dashboards/`:

| File | What | Refresh cadence |
|---|---|---|
| `resy-storage-state.json` | Playwright cookies + localStorage from `os.resy.com` login | ~21 days |
| `resy-venues.txt` | Outlet→Resy slug mapping | When venues launch/sunset |
| `gbp-client.json` | Google OAuth client credentials | Never (created in GCP console) |
| `gbp-token.json` | Google OAuth refresh token | Indefinite unless revoked |
| `gbp-locations.json` | Outlet→GBP location resource name map | When venues launch / GBP ownership changes |

None of these are committed to the repo. Treat all of them as secrets — the Resy storage state in particular grants full access to your Resy OS portal until it expires.
