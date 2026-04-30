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


def transform_resy_survey_row(raw: dict) -> dict | None:
    """Map a Resy OS survey row → the dashboard's survey schema.

    Resy's row shape (verified via discover, run 25115337275):
      {
        date_completed: ISO timestamp,
        id: int,
        overall_score: 0-100 int,
        reservation: {
          server, party_size, date_seated, service_type, ...
        },
        responses: [
          {question, question_option, question_weight, response, ...},
          ...
        ],
        user: {<PII — DROPPED, never stored>}
      }

    Question text → score-bucket mapping is substring-based (Resy lets
    venues customize the question text). Buckets we map into:
      food, service, atmos, sentiment, recommend.
    """
    rsv = raw.get("reservation") or {}
    date_completed = (raw.get("date_completed") or "")[:10]
    if not date_completed:
        return None
    # Derive dow + hour from reservation if available, else date_completed
    seated = rsv.get("date_seated") or rsv.get("date_arrived") or raw.get("date_completed") or ""
    hour = None
    try:
        if len(seated) >= 13 and seated[10] in ("T", " "):
            hour = int(seated[11:13])
    except ValueError:
        pass
    try:
        from datetime import date as _d
        y, m, d = (int(x) for x in date_completed.split("-"))
        dow = _d(y, m, d).weekday()  # Mon=0
    except Exception:
        dow = None

    # Walk responses and bucket by question text. Resy's `question` field
    # can be either a string (older shape) or a nested dict like
    #   {"id": ..., "text": "...", "category": ..., "weight": ...}.
    # Coerce both shapes to a lowercase prompt string for keyword matching.
    def _question_text(q_field) -> str:
        if isinstance(q_field, dict):
            for k in ("text", "title", "label", "question", "prompt", "name"):
                v = q_field.get(k)
                if isinstance(v, str) and v:
                    return v.lower()
            return ""
        if isinstance(q_field, str):
            return q_field.lower()
        return ""

    food = service = atmos = sentiment = recommend = None
    text_responses: list[dict] = []   # free-text answers — surface in UI
    for r in (raw.get("responses") or []):
        if not isinstance(r, dict):
            continue
        q_raw = r.get("question")
        q = _question_text(q_raw)
        ans = r.get("response")
        if ans is None:
            continue
        # Coerce numeric — handle dict shape ({"score": 9}) too
        score = None
        if isinstance(ans, (int, float)):
            score = float(ans)
        elif isinstance(ans, dict):
            for k in ("score", "value", "rating", "answer"):
                v = ans.get(k)
                if isinstance(v, (int, float)):
                    score = float(v); break
        else:
            try:
                score = float(ans)
            except (TypeError, ValueError):
                pass
        if score is not None:
            if "food" in q or "menu" in q:
                food = score
            elif "service" in q or "staff" in q:
                service = score
            elif "atmos" in q or "ambien" in q or "vibe" in q or "decor" in q:
                atmos = score
            elif "sentiment" in q or "experience" in q or "overall" in q:
                sentiment = score
            elif ("recomm" in q or "likel" in q or "promot" in q or "nps" in q):
                recommend = score
            continue
        # Non-numeric answer → likely a free-text comment. Resy's open-
        # text questions ("Anything else you'd like to share?", "What
        # could we have done better?", etc.) come through with the same
        # row shape but `response` as a string. Capture for the UI.
        # Filter trivial yes/no responses since they're not useful.
        text = None
        if isinstance(ans, str):
            text = ans.strip()
        elif isinstance(ans, dict):
            for k in ("text", "value", "answer", "response"):
                v = ans.get(k)
                if isinstance(v, str) and v.strip():
                    text = v.strip(); break
        if not text or len(text) < 4:
            continue
        if text.lower() in {"yes", "no", "n/a", "na", "none", "nothing"}:
            continue
        # Pull the operator-facing question text (preserve original case
        # so it reads like the operator wrote it).
        q_disp = q
        if isinstance(q_raw, dict):
            for k in ("text", "title", "label", "question", "prompt", "name"):
                v = q_raw.get(k)
                if isinstance(v, str) and v:
                    q_disp = v; break
        elif isinstance(q_raw, str):
            q_disp = q_raw
        text_responses.append({
            "q": (q_disp or "")[:120],
            "a": text[:600],
        })

    return {
        "date":      date_completed,
        "overall":   raw.get("overall_score"),
        "sentiment": sentiment,
        "service":   service,
        "food":      food,
        "atmos":     atmos,
        "recommend": recommend,
        "server":    (rsv.get("server") or "").strip() or None,
        "covers":    rsv.get("party_size"),
        "dow":       dow,
        "hour":      hour,
        "text":      text_responses or None,
    }


def transform_to_guest_block(
    captured: list[dict], existing_guest: dict | None
) -> tuple[dict, dict]:
    """Take a list of {url, json} responses and emit a `guest` block in the
    same shape renderGuestSection() consumes.

    Recognized payload shapes (verified via Resy OS discovery):
      - {data: {surveys: [<resy survey row>, ...]}}   ← survey.resy.com/api/1/venue/surveys
      - {data: [<rating row>, ...]}                   ← *legacy seed shape*
      - {config:..., data: [<rating row>, ...]}       ← api.resy.com/3/analytics/report/core/ratings

    `existing_guest` is the seed block (NPS-Report extractor). We APPEND-
    MERGE on a natural key — the seed historical tail always survives.

    Returns (guest_block, stats) where stats tracks upgrade counts so the
    caller's session-expiry healthcheck can distinguish "rows were
    upgraded in place" (a healthy backfill — for example when the
    free-text capture rolled out 2026-04-30 and existing 1.9k LSBR rows
    refreshed) from "scraper got nothing back" (true session expiry).
    Without stats, the healthcheck would mis-report a successful backfill
    run as expired because survey row count didn't change.
    """
    surveys = list((existing_guest or {}).get("surveys") or [])
    ratings = list((existing_guest or {}).get("ratings") or [])
    comments = list((existing_guest or {}).get("comments") or [])
    google = (existing_guest or {}).get("google")
    stats = {"added": 0, "upgraded": 0}

    # Dedup keys for survey rows — natural-key tuple so a re-scrape
    # doesn't double-count.
    def survey_key(s: dict) -> tuple:
        return (s.get("date"), s.get("server"), s.get("overall"), s.get("covers"))

    # Track existing rows by natural key AND remember their index so we
    # can replace text-less rows in place when a fresh API response
    # carries the free-text answers (added 2026-04-30 — see PR for the
    # `text` field on survey rows). Without this, the 1.9k existing
    # surveys would never receive their newly-captured comment text.
    seen_index = {survey_key(s): i for i, s in enumerate(surveys)}
    rating_fields = {"r1", "r2", "r3", "r4", "r5"}

    def unwrap(node):
        if isinstance(node, dict) and "data" in node and len(node) <= 3:
            return node["data"]
        return node

    def extract_resy_surveys(node) -> list[dict]:
        """Find Resy-shaped survey rows (have 'overall_score' + 'responses')
        and transform each into our schema. Walks the entire payload tree."""
        out: list[dict] = []
        if isinstance(node, dict):
            if "overall_score" in node and "responses" in node and "date_completed" in node:
                row = transform_resy_survey_row(node)
                if row:
                    out.append(row)
            else:
                for v in node.values():
                    out.extend(extract_resy_surveys(v))
        elif isinstance(node, list):
            for v in node:
                out.extend(extract_resy_surveys(v))
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
        # Resy surveys path — traverse + transform
        for row in extract_resy_surveys(body):
            k = survey_key(row)
            if k in seen_index:
                # Existing row — only replace if the new payload adds
                # something the old one lacks (text comments, score
                # bucket coverage). Avoids unnecessary writes while
                # still backfilling pre-text-capture history.
                old = surveys[seen_index[k]]
                old_has_text = bool(old.get("text"))
                new_has_text = bool(row.get("text"))
                if new_has_text and not old_has_text:
                    surveys[seen_index[k]] = row
                    stats["upgraded"] += 1
                continue
            surveys.append(row)
            seen_index[k] = len(surveys) - 1
            stats["added"] += 1
        # Legacy path — seed-shaped rows (NPS-export extractor used these)
        for row in extract_ratings(body):
            ratings.append(row)

    return ({
        "as_of": date.today().isoformat(),
        "source": "resy_os_scraper",
        "surveys": surveys,
        "ratings": ratings,
        "comments": comments,
        **({"google": google} if google else {}),
    }, stats)


def expand_via_limit_bump(page, captured: list[dict]) -> list[dict]:
    """Re-issue each captured survey/ratings URL with a bumped
    pagination limit so we receive the venue's *full* history in one
    response, not just the SPA's first page.

    Resy OS's analytics SPA paginates client-side (~20 rows per fetch)
    so the original `on_response` capture only sees the latest page.
    For free-text comments to be useful as a period-filtered review
    feed, we need every survey row, not just the most recent ones.
    Playwright's `page.request` shares the storage-state cookies, so
    these fetches reuse the same auth without a separate session.

    Strategy:
      • dedup by base URL (scheme+host+path)
      • bump common limit params (`limit`, `per_page`, `pageSize`,
        `take`, `count`) to a large value
      • strip offset/cursor params (`offset`, `page`, `skip`, `start`)
      • if no recognized limit param exists, add `limit=10000`
      • bail gracefully on per-venue failure (HTTP error, JSON parse,
        etc.) — the SPA-captured page-1 results stay in `captured`
        so we still get *something* from the venue.

    Returns expanded responses to be merged with `captured`. The
    transformer dedups via natural key, so any overlap between the
    SPA capture and the bumped fetch is harmless.
    """
    import urllib.parse as _u
    LIMIT_KEYS = {"limit", "per_page", "pagesize", "take", "count"}
    OFFSET_KEYS = {"offset", "page", "skip", "start"}
    BIG_LIMIT = "10000"
    # Headers we must NOT forward — Playwright's request context manages
    # them itself and replaying them breaks the request. Cookies are
    # auto-attached from the page context. Pseudo-headers (`:authority`
    # etc.) are HTTP/2-specific. Content-length is recomputed by
    # Playwright. Sec-Fetch-* are renderer-asserted and may not match.
    DROP_HEADERS = {
        "host", "content-length", "cookie",
        ":authority", ":method", ":path", ":scheme",
        "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest",
        "sec-fetch-user", "connection", "transfer-encoding",
    }

    expanded: list[dict] = []
    seen_bases: set[str] = set()
    for cap in captured:
        url = cap.get("url") or ""
        # Only re-issue GET endpoints. Resy's analytics report (POST)
        # has been showing up as a candidate but isn't a list endpoint
        # we want to bump.
        if (cap.get("method") or "GET").upper() != "GET":
            continue
        # Only expand the survey/ratings list endpoints, not unrelated
        # candidate URLs (auth, config, profile, etc.).
        u_lc = url.lower()
        if not any(t in u_lc for t in ("survey", "rating", "feedback", "review")):
            continue
        parts = _u.urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if base in seen_bases:
            continue
        seen_bases.add(base)
        qs = _u.parse_qs(parts.query, keep_blank_values=True)
        for k in list(qs.keys()):
            if k.lower() in LIMIT_KEYS:
                qs[k] = [BIG_LIMIT]
            if k.lower() in OFFSET_KEYS:
                qs.pop(k)
        if not any(k.lower() in LIMIT_KEYS for k in qs):
            qs["limit"] = [BIG_LIMIT]
        new_url = _u.urlunparse(parts._replace(query=_u.urlencode(qs, doseq=True)))
        # Forward the SPA's request headers, especially Authorization.
        # Without the Bearer JWT, survey.resy.com 401s — cookies alone
        # are not sufficient for that host.
        src_headers = cap.get("request_headers") or {}
        headers = {k: v for k, v in src_headers.items()
                   if k.lower() not in DROP_HEADERS and not k.startswith(":")}
        try:
            resp = page.request.get(new_url, headers=headers, timeout=45_000)
        except Exception as e:
            sys.stderr.write(f"  full-history bump request failed for {base}: {e}\n")
            continue
        if not resp.ok:
            sys.stderr.write(
                f"  full-history bump returned HTTP {resp.status} for {base}\n"
            )
            continue
        try:
            body = resp.json()
        except Exception as e:
            sys.stderr.write(f"  full-history bump JSON parse failed for {base}: {e}\n")
            continue
        expanded.append({"url": new_url, "status": resp.status, "json": body})
    return expanded


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
        # Stash request method + headers so the limit-bump replay can
        # forward the SPA's Authorization header (JWT in localStorage,
        # not in cookies — page.request lacks renderer-side state).
        try:
            req = resp.request
            req_method = (req.method or "GET").upper()
            req_headers = dict(req.headers or {})
        except Exception:
            req_method, req_headers = "GET", {}
        captured.append({
            "url": url, "status": resp.status, "json": body,
            "method": req_method, "request_headers": req_headers,
        })

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
    # Re-issue each captured survey URL with a bumped `limit` to fetch
    # the venue's full history (not just the SPA's first page of ~20).
    # Free-text comments are only useful for period filtering when we
    # have all rows, not just the most recent. See expand_via_limit_bump.
    expanded = expand_via_limit_bump(page, captured)
    if expanded:
        print(f"    full-history fetch: {len(expanded)} response(s) bumped to limit=10000")
    return captured + expanded


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
    healthcheck_total_new = 0
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
                        print(f"        outer_count={row_count} outer_keys={row_shape}")
                    # Surveys live two levels deep (data.surveys); dump the
                    # first row's keys, plus its nested {reservation,
                    # responses} sub-keys so the field map is unambiguous.
                    if isinstance(inner, dict):
                        for promising_key in ("surveys", "comments", "ratings"):
                            sub = inner.get(promising_key)
                            if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                                row0 = sub[0]
                                sub_keys = sorted(row0.keys())[:25]
                                print(f"        {promising_key}_count={len(sub)} {promising_key}_row_keys={sub_keys}")
                                # Drill nested objects (reservation, responses
                                # are dicts; user is also a dict but PII —
                                # only dump the key list, not the values).
                                for nk, nv in row0.items():
                                    if isinstance(nv, dict):
                                        print(f"          {nk}_keys={sorted(nv.keys())[:18]}")
                                    elif isinstance(nv, list) and nv and isinstance(nv[0], dict):
                                        print(f"          {nk}[0]_keys={sorted(nv[0].keys())[:18]}")
                continue

            # Transform + merge
            payload = load_outlet(data_dir, oid)
            existing_guest = payload.get("guest") or {}
            new_guest, mstats = transform_to_guest_block(captured, existing_guest)
            n_surveys = len(new_guest.get("surveys") or [])
            n_existing = len(existing_guest.get("surveys") or [])
            added = mstats["added"]
            upgraded = mstats["upgraded"]
            tail = f" · upgraded {upgraded}" if upgraded else ""
            print(f"  surveys: {n_existing} → {n_surveys} (+{added}){tail}")
            # Healthcheck signal: a venue is "quiet" only if NEITHER
            # new rows came in NOR existing rows were upgraded with
            # text. A pure-upgrade run still proves the session is
            # alive (the API returned data); we just happened to
            # already have those rows.
            if added == 0 and upgraded == 0:
                healthcheck_zero_count += 1
            else:
                healthcheck_total_new += (added + upgraded)
            payload["guest"] = new_guest
            payload["generated_at_resy"] = datetime.now(timezone.utc).isoformat()
            write_outlet(data_dir, oid, payload)

        browser.close()

    if failures:
        sys.stderr.write(f"\n{len(failures)} venue(s) failed: {failures}\n")

    # Session-expiry detection: a dead storage state means EVERY venue
    # gets redirected to login and writes 0 new rows AND no upgrades to
    # existing rows. So the only reliable "session expired" signal is
    # `total activity across the whole portfolio == 0` (where activity
    # = added + upgraded). Per-venue zero count alone is noisy — vessel
    # (private events, no Resy), rosemary_rose (~2 surveys ever), and
    # quoin (low volume) routinely come back empty even with a healthy
    # session. Earlier logic exited 1 at >2 zero-venues, which threw
    # away successful pulls on quiet days. See PR #52 / 2026-04-30
    # incident. Upgrades count as activity to prevent the same false
    # alarm during the text-capture backfill.
    if healthcheck_total_new == 0 and len(targets) > 1:
        sys.stderr.write(
            "\n[healthcheck] 0 new or upgraded surveys across all "
            "venues — storage state likely expired. Run "
            "tools/refresh_resy_storage.py to reseed.\n"
        )
        return 1
    if healthcheck_zero_count > 2:
        sys.stderr.write(
            f"\n[healthcheck] {healthcheck_zero_count} venues quiet, "
            f"but {healthcheck_total_new} new/upgraded rows captured "
            f"elsewhere — session healthy, continuing.\n"
        )
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
