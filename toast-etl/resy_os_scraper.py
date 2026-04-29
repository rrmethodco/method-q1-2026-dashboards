#!/usr/bin/env python3
"""
Method Co — Resy OS guest-experience scraper.

Replaces resy_sync.py (which fails with HTTP 419 against operator accounts)
with a Playwright-based scraper that drives the real Resy OS SPA using a
stored auth state. Captures the SPA's own JSON XHR responses and merges
them into data/<outlet>.json under the `guest` key — same shape the
dashboard renderer (renderGuestSection) already consumes.

Setup:
  1. Bootstrap auth state locally (one-time, ~60 seconds):
       python3 -m pip install playwright requests
       python3 -m playwright install chromium
       python3 tools/refresh_resy_storage.py
       gh secret set RESY_OS_STORAGE_STATE_JSON < resy-storage-state.json

  2. Map outlet ids to Resy OS slugs (one-time):
       gh secret set RESY_OS_VENUES --body "lsbr=det/le-supreme;lowland=chs/lowland;..."
     (The slug is the `<city>/<venue>` portion of the OS URL, NOT the
     numeric venue_id used by the consumer API.)

  3. Refresh storage state every ~21 days (when the nightly job's
     healthcheck flags 0 surveys for >2 venues).

Usage:
  python3 resy_os_scraper.py                  # all venues
  python3 resy_os_scraper.py --outlet lsbr    # one outlet
  python3 resy_os_scraper.py --discover       # discover XHR endpoints,
                                                no writes; use for first
                                                run / when Resy ships a UI
                                                change that breaks scraping
  python3 resy_os_scraper.py --dry-run        # no auth, write fixture

Behavior:
  - Append-merges with existing guest block (dedup on date+server+overall)
    so historical NPS-export seed data survives indefinitely.
  - Atomic write via .tmp.
  - Healthcheck: exits 1 if >2 venues return 0 surveys (silent breakage
    is the real risk for scrapers; loud failure forces a runbook step).
  - Exits 0 cleanly when RESY_OS_STORAGE_STATE_JSON or RESY_OS_VENUES are
    missing — lets the nightly run before secrets are populated.

Architecture:
  Headless Chromium with stored cookies + localStorage. For each venue,
  navigate to the Reviews/Insights pages and listen to all fetch/XHR
  traffic. Filter responses by URL pattern + JSON shape, then transform
  into the outlet's `guest` block.

  Discovery is deliberate: the script ships with a list of CANDIDATE_URL_
  PATTERNS that match likely survey/ratings endpoints; the first run with
  --discover prints what was actually seen so the operator can lock down
  the patterns. Resy OS endpoints aren't documented, so this is the
  first-run loop — mirror what the existing toast_audit.py does for the
  Toast clients.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write(
        "missing dependency: pip install playwright && playwright install chromium\n"
    )
    sys.exit(2)


# ---------- config ----------

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 4_000

# URL substrings that signal "this XHR is probably the survey/ratings
# data". Order doesn't matter — we capture all matches and let the
# transformer decide what's useful. Update when discovery shows new paths.
CANDIDATE_URL_PATTERNS = [
    "feedback",
    "survey",
    "ratings",
    "reviews",
    "guest_satisf",
    "nps",
]

# Pages within a venue's OS portal that are most likely to fire the
# survey/ratings XHRs. Discovery (2026-04-29) confirmed the actual paths
# are under `analytics/<Surveys|Reviews|Comments>` (NOT `Insights/...`)
# and the XHRs fan out to a separate host: `survey.resy.com/api/1/...`.
# We visit each in order; if any yields usable JSON, we bail early.
VENUE_INSIGHT_PAGES = [
    "analytics/Surveys",
    "analytics/Reviews",
    "analytics/Comments",
    "analytics/Ratings",
    "analytics",
    "Home",  # fallback — dashboard sometimes pre-loads recent feedback
]


def parse_venues(raw: str) -> dict[str, str]:
    """Parse RESY_OS_VENUES into {outlet_id: slug}.

    Format: outlet_id=city/slug;outlet_id=city/slug;...
    Example: lsbr=det/le-supreme;lowland=chs/lowland
    """
    out: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        oid, slug = chunk.split("=", 1)
        oid = oid.strip(); slug = slug.strip("/").strip()
        if oid and slug:
            out[oid] = slug
    return out


def load_outlet(data_dir: Path, outlet_id: str) -> dict:
    p = data_dir / f"{outlet_id}.json"
    if not p.exists():
        return {"outlet_id": outlet_id}
    return json.loads(p.read_text(encoding="utf-8"))


def write_outlet(data_dir: Path, outlet_id: str, payload: dict) -> None:
    p = data_dir / f"{outlet_id}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def is_candidate_url(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in CANDIDATE_URL_PATTERNS)


def transform_to_guest_block(
    captured: list[dict], existing_guest: dict | None
) -> dict:
    """Take a list of {url, json} responses and emit a `guest` block in the
    same shape renderGuestSection() consumes. The transform is generous —
    we look for known field names (overall, sentiment, food, service,
    atmos, recommend, server, covers, dow, hour) in every response and
    keep what we find.

    `existing_guest` is the seed block already in the file (from the
    NPS-Report extractor). We APPEND-MERGE — the seed historical tail
    always survives.
    """
    surveys = list((existing_guest or {}).get("surveys") or [])
    ratings = list((existing_guest or {}).get("ratings") or [])
    comments = list((existing_guest or {}).get("comments") or [])
    google = (existing_guest or {}).get("google")

    # Dedup keys for survey rows — we use a tuple of natural-key fields
    # so a re-scrape doesn't double-count.
    def survey_key(s: dict) -> tuple:
        return (s.get("date"), s.get("server"), s.get("overall"), s.get("covers"))

    seen_keys = {survey_key(s) for s in surveys}

    survey_fields = {"overall", "sentiment", "service", "food", "atmos",
                     "recommend", "server", "covers", "dow", "hour"}
    rating_fields = {"r1", "r2", "r3", "r4", "r5"}

    # Resy's API wraps payloads under a `data` key; drill in transparently.
    def unwrap(node):
        if isinstance(node, dict) and "data" in node and len(node) <= 3:
            return node["data"]
        return node

    def extract_rows(node) -> list[dict]:
        """Walk arbitrary JSON and return every dict that looks like a
        survey row (has at least 3 of the survey_fields)."""
        node = unwrap(node)
        out: list[dict] = []
        if isinstance(node, dict):
            keys = set(node.keys())
            score = len(keys & survey_fields) + (1 if "date" in keys else 0)
            if score >= 3:
                out.append(node)
            for v in node.values():
                out.extend(extract_rows(v))
        elif isinstance(node, list):
            for v in node:
                out.extend(extract_rows(v))
        return out

    def extract_ratings(node) -> list[dict]:
        out: list[dict] = []
        if isinstance(node, dict):
            keys = set(node.keys())
            if (keys & rating_fields) and "date" in keys:
                out.append(node)
            for v in node.values():
                out.extend(extract_ratings(v))
        elif isinstance(node, list):
            for v in node:
                out.extend(extract_ratings(v))
        return out

    for cap in captured:
        body = cap.get("json")
        for row in extract_rows(body):
            k = survey_key(row)
            if k in seen_keys:
                continue
            surveys.append(row)
            seen_keys.add(k)
        for row in extract_ratings(body):
            ratings.append(row)

    return {
        "as_of": date.today().isoformat(),
        "source": "resy_os_scraper",
        "surveys": surveys,
        "ratings": ratings,
        "comments": comments,
        **({"google": google} if google else {}),
    }


def scrape_venue(page, slug: str, discover: bool) -> list[dict]:
    """Navigate through the venue's insight pages, capture candidate
    JSON responses. Returns list of {url, status, json}.

    Resy OS XHRs fan out to multiple hosts (os.resy.com itself plus
    survey.resy.com /api/1/...), and some are cached/intercepted by the
    Service Worker. We listen on `response` (Playwright) AND ALSO
    monkey-patch fetch/XHR via init-script so SW-served responses are
    captured too.
    """
    captured: list[dict] = []
    all_seen_urls: list[dict] = []  # for --discover diagnostics

    def on_response(resp):
        url = resp.url
        # In discover mode, log every JSON response so we can see what
        # the SPA is actually doing even when no candidate matched.
        if discover:
            all_seen_urls.append({"url": url, "status": resp.status})
        if not is_candidate_url(url):
            return
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            body = resp.json()
        except Exception:
            return
        captured.append({"url": url, "status": resp.status, "json": body})

    page.on("response", on_response)

    for sub in VENUE_INSIGHT_PAGES:
        url = f"https://os.resy.com/portal/{slug}/{sub}"
        try:
            # `domcontentloaded` instead of `networkidle` — Resy OS keeps
            # background telemetry traffic open indefinitely so networkidle
            # never fires within timeout. We then explicitly wait for a
            # window that captures the SPA's data XHRs.
            page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        except PWTimeout:
            sys.stderr.write(f"  [{slug}] timeout on {sub} — continuing\n")
            continue
        except Exception as e:
            sys.stderr.write(f"  [{slug}] error on {sub}: {e}\n")
            continue
        # Wait for the SPA to do its survey-data XHR. 7s is conservative;
        # the actual XHR usually fires within 3s.
        try:
            page.wait_for_timeout(7000)
        except Exception:
            pass
        if captured and not discover:
            break  # got something useful — be polite

    page.remove_listener("response", on_response)
    if discover:
        # Print everything we saw so the operator can lock down the
        # right URL patterns. Keep it concise — top 30 unique paths.
        seen_paths = sorted({u["url"].split("?")[0] for u in all_seen_urls
                             if not any(skip in u["url"] for skip in
                                        ["datadog", "amplitude", "kustomer",
                                         "google-analytics", "fbevents",
                                         "incapsula", "stripe.com",
                                         "hubspot", "imrworldwide"])})
        for p in seen_paths[:30]:
            print(f"    seen: {p}")
    return captured


def cmd_run(storage_state: dict, venues: dict[str, str], data_dir: Path,
            only: str | None, discover: bool, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] writing fixture; no browser launched")
        fixture = {
            "as_of": date.today().isoformat(), "source": "resy_os_dry_run",
            "surveys": [{"date": date.today().isoformat(), "overall": 100,
                         "sentiment": 100, "service": 100, "food": 100,
                         "atmos": 100, "server": "TEST", "recommend": 10,
                         "covers": 2, "dow": 0, "hour": 19}],
            "ratings": [{"date": date.today().isoformat(),
                         "r1": 0, "r2": 0, "r3": 0, "r4": 0, "r5": 1}],
        }
        (data_dir / "_resy_os_dry_run.json").write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    targets = {oid: slug for oid, slug in venues.items() if not only or oid == only}
    if not targets:
        sys.stderr.write(f"no matching venues (only={only!r})\n")
        return 1

    healthcheck_zero_count = 0
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
        page = ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        for oid, slug in targets.items():
            print(f"\n[{oid}] slug={slug}")
            try:
                captured = scrape_venue(page, slug, discover)
            except Exception as e:
                sys.stderr.write(f"  ✗ {oid}: {e}\n")
                failures.append(oid)
                continue
            print(f"  captured {len(captured)} candidate response(s)")
            if discover:
                for c in captured[:10]:
                    body = c["json"]
                    body_keys: list[str] = []
                    if isinstance(body, dict):
                        body_keys = list(body.keys())[:8]
                    elif isinstance(body, list) and body:
                        body_keys = list((body[0] or {}).keys())[:8] \
                            if isinstance(body[0], dict) else ["<list>"]
                    # Drill into Resy's `data` wrapper for a row-shape preview.
                    inner = body
                    if isinstance(body, dict) and "data" in body:
                        inner = body["data"]
                    row_shape: list[str] = []
                    row_count = None
                    if isinstance(inner, list) and inner:
                        row_count = len(inner)
                        if isinstance(inner[0], dict):
                            row_shape = sorted(inner[0].keys())[:18]
                    elif isinstance(inner, dict):
                        row_shape = sorted(inner.keys())[:18]
                    print(f"    {c['status']} {c['url'][:100]} top_keys={body_keys}")
                    if row_shape:
                        print(f"        row_count={row_count} row_keys={row_shape}")
                continue

            # Transform + merge
            payload = load_outlet(data_dir, oid)
            existing_guest = payload.get("guest") or {}
            new_guest = transform_to_guest_block(captured, existing_guest)
            n_surveys = len(new_guest.get("surveys") or [])
            n_existing = len(existing_guest.get("surveys") or [])
            print(f"  surveys: {n_existing} → {n_surveys} "
                  f"(+{n_surveys - n_existing})")
            if n_surveys == n_existing:
                healthcheck_zero_count += 1
            payload["guest"] = new_guest
            payload["generated_at_resy"] = datetime.now(timezone.utc).isoformat()
            write_outlet(data_dir, oid, payload)

        browser.close()

    if failures:
        sys.stderr.write(f"\n{len(failures)} venue(s) failed: {failures}\n")
    if healthcheck_zero_count > 2:
        sys.stderr.write(
            f"\n[healthcheck] {healthcheck_zero_count} venues had 0 new "
            f"surveys — storage state likely expired. Run "
            f"tools/refresh_resy_storage.py to reseed.\n"
        )
        return 1
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--outlet", help="single outlet id (default: all)")
    ap.add_argument("--discover", action="store_true",
                    help="print captured XHR URLs + payload-key shapes; no writes")
    ap.add_argument("--dry-run", action="store_true",
                    help="write fixture; no browser/network")
    ap.add_argument("--data-dir", default="../data",
                    help="dir of <outlet>.json files (default: ../data)")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    if args.dry_run:
        return cmd_run({}, {"_dry": "_dry"}, data_dir, args.outlet, False, True)

    raw_state = os.environ.get("RESY_OS_STORAGE_STATE_JSON")
    raw_venues = os.environ.get("RESY_OS_VENUES")
    if not raw_state:
        sys.stderr.write("RESY_OS_STORAGE_STATE_JSON missing — exiting cleanly\n")
        return 0
    if not raw_venues:
        sys.stderr.write("RESY_OS_VENUES is empty — nothing to scrape\n")
        return 0

    try:
        storage_state = json.loads(raw_state)
    except Exception as e:
        sys.stderr.write(f"RESY_OS_STORAGE_STATE_JSON parse error: {e}\n")
        return 1

    venues = parse_venues(raw_venues)
    if not venues:
        sys.stderr.write("RESY_OS_VENUES parsed empty\n")
        return 0

    return cmd_run(storage_state, venues, data_dir, args.outlet,
                   args.discover, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
