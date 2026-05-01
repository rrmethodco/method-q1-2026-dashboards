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


# Schema-drift diagnostic state. Populated by transform_resy_survey_row
# whenever a survey has a non-empty responses[] but our keyword router
# fails to extract any of the 5 score fields (food/service/atmos/
# sentiment/recommend). Dumped to stderr at end of run so the operator
# sees actionable evidence to fix the parser when Resy changes shape.
# Cap at 8 samples to avoid log spam — the first 8 are enough to read
# the new schema.
_DRIFT_SAMPLES: list[dict] = []


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
    responses = raw.get("responses") or []
    _resp_count = len(responses) if isinstance(responses, list) else 0
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

    # Schema-drift diagnostic — if Resy emitted a non-empty responses[]
    # but our keyword router didn't bucket ANY of the 5 score fields,
    # capture a redacted shape sample. Dashboard's NPS card depends on
    # `recommend` being populated; recent surveys (post 2026-04-16, see
    # docs notes) all have null scores → schema drift suspected.
    bucketed = sum(v is not None for v in (food, service, atmos, sentiment, recommend))
    if _resp_count > 0 and bucketed == 0 and len(_DRIFT_SAMPLES) < 8:
        sample_rows = []
        for r in responses[:3]:  # first 3 entries — enough to read structure
            if not isinstance(r, dict):
                sample_rows.append({"_type": type(r).__name__})
                continue
            sample_rows.append({
                "row_keys": sorted(r.keys()),
                "question_type": type(r.get("question")).__name__,
                "question_preview": (
                    str(r.get("question"))[:120] if not isinstance(r.get("question"), dict)
                    else {k: type(v).__name__ for k, v in r.get("question", {}).items()}
                ),
                "response_type": type(r.get("response")).__name__,
                "response_preview": (
                    None if r.get("response") is None
                    else (str(r.get("response"))[:120] if not isinstance(r.get("response"), dict)
                          else {k: type(v).__name__ for k, v in r.get("response", {}).items()})
                ),
            })
        _DRIFT_SAMPLES.append({
            "date": date_completed,
            "row_keys": sorted(raw.keys()),
            "responses_count": _resp_count,
            "first_3_responses": sample_rows,
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

    # Per-row merge: each Resy API query returns a different SUBSET of
    # the survey's responses[]. The score-question query (question_id=2)
    # returns numeric ratings; the comment-question query (question_id=29)
    # returns text. Same survey row, different fields populated. We must
    # MERGE per-field, never wholesale replace, or the second pass clobbers
    # the first. (PR #80 hit this: text-only fetches blanked out scores on
    # 100% of rows that previously had both.)
    SCORE_FIELDS = ("food", "service", "atmos", "sentiment", "recommend",
                    "overall", "server", "covers", "dow", "hour")
    for cap in captured:
        body = cap.get("json")
        # Resy surveys path — traverse + transform
        for row in extract_resy_surveys(body):
            k = survey_key(row)
            if k in seen_index:
                old = surveys[seen_index[k]]
                changed = False
                # Numeric fields & metadata — keep old if non-null, else
                # take new. Never overwrite a non-null value with null.
                for f in SCORE_FIELDS:
                    new_v = row.get(f)
                    if new_v is None:
                        continue
                    if old.get(f) is None:
                        old[f] = new_v
                        changed = True
                # Text answers — union by (question, answer) tuple.
                old_text = old.get("text") or []
                new_text = row.get("text") or []
                if new_text:
                    seen_pairs = {(t.get("q"), t.get("a")) for t in old_text if isinstance(t, dict)}
                    additions = [t for t in new_text
                                 if isinstance(t, dict)
                                 and (t.get("q"), t.get("a")) not in seen_pairs]
                    if additions:
                        old["text"] = old_text + additions
                        changed = True
                if changed:
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
    # Verified via run #25189914424 diagnostics: Resy's surveys endpoint
    # paginates via `all=true` (not `limit`) within a `start_date` /
    # `end_date` window. With `all=true` LSBR's April returned 120 rows
    # vs. the default 20. So full history = wide date range + all=true
    # + follow `data.next_request` cursor if present.
    DROP_HEADERS = {
        "host", "content-length", "cookie",
        ":authority", ":method", ":path", ":scheme",
        "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest",
        "sec-fetch-user", "connection", "transfer-encoding",
    }
    # Year-chunked fetch. A single wide-date-range request times out at
    # 60s for high-volume venues (LSBR ~1900 rows, Lowland ~1500). Hit
    # one calendar year at a time — each response is bounded and fast,
    # and the natural-key dedup absorbs any overlap. Cover Method's
    # earliest Resy presence (mid-2022) through the current year, plus
    # one in the future to catch edge cases.
    from datetime import datetime as _dt
    _now_year = _dt.utcnow().year
    YEAR_CHUNKS = [(y, f"{y}-01-01T00:00:00", f"{y}-12-31T23:59:59")
                   for y in range(2022, _now_year + 2)]
    MAX_PAGES_PER_YEAR = 50  # safety net for cursor loops within a year
    REQUEST_TIMEOUT_MS = 90_000

    expanded: list[dict] = []
    seen_bases: set[str] = set()
    for cap in captured:
        url = cap.get("url") or ""
        if (cap.get("method") or "GET").upper() != "GET":
            continue
        # Only target the surveys list endpoint — analytics is a
        # different shape and doesn't have rows to bump.
        u_lc = url.lower()
        if "survey.resy.com/api/1/venue/surveys" not in u_lc:
            continue
        parts = _u.urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if base in seen_bases:
            continue
        seen_bases.add(base)
        src_headers = cap.get("request_headers") or {}
        headers = {k: v for k, v in src_headers.items()
                   if k.lower() not in DROP_HEADERS and not k.startswith(":")}
        # Build the per-year base query template. Resy returns DIFFERENT
        # subsets of each survey's responses[] depending on the
        # surveyresponse__question_id filter — id=2 returns numeric
        # ratings (recommend, food, service, atmos, sentiment); id=29
        # returns the free-text comment. Without any question filter, we
        # get only the comment. So we run the year-loop TWICE: once for
        # scores (q=2) and once unfiltered for text (sets the dashboard's
        # NPS card and the Reviews & Comments tiles respectively).
        base_qs_template = _u.parse_qs(parts.query, keep_blank_values=True)
        for k in list(base_qs_template.keys()):
            if k.startswith("surveyresponse__"):
                base_qs_template.pop(k)
        base_qs_template["all"] = ["true"]
        base_qs_template.setdefault("sort", ["-reservation__date_booked"])

        # Two query variants per year: text (no question filter) and
        # scores (question_id=2). The merge in transform_to_guest_block
        # unions fields per row.
        QUERY_VARIANTS = [
            {},  # text-only (no question filter)
            {"surveyresponse__question_id": ["2"],
             "surveyresponse__response__isnull": ["False"]},
        ]

        # Walk one calendar year at a time. Year-chunking caps each
        # response size + wall time so we don't blow past the 90s
        # request timeout on high-volume venues.
        for year, start_iso, end_iso in YEAR_CHUNKS:
         for variant in QUERY_VARIANTS:
            qs = {k: list(v) for k, v in base_qs_template.items()}
            qs["start_date"] = [start_iso]
            qs["end_date"] = [end_iso]
            for vk, vv in variant.items():
                qs[vk] = vv
            year_url = _u.urlunparse(parts._replace(query=_u.urlencode(qs, doseq=True)))
            page_idx = 0
            cur_url = year_url
            while page_idx < MAX_PAGES_PER_YEAR:
                try:
                    resp = page.request.get(cur_url, headers=headers, timeout=REQUEST_TIMEOUT_MS)
                except Exception as e:
                    sys.stderr.write(
                        f"  full-history {year} fetch failed page {page_idx}: {e}\n"
                    )
                    break
                if not resp.ok:
                    sys.stderr.write(
                        f"  full-history {year} HTTP {resp.status} page {page_idx} for {base}\n"
                    )
                    break
                try:
                    body = resp.json()
                except Exception as e:
                    sys.stderr.write(
                        f"  full-history {year} JSON parse failed page {page_idx}: {e}\n"
                    )
                    break
                expanded.append({"url": cur_url, "status": resp.status, "json": body})
                # Stop early if year has no data — saves a lot of
                # round-trips for venues that only opened recently.
                if isinstance(body, dict):
                    d = body.get("data")
                    if isinstance(d, dict):
                        rows = d.get("surveys")
                        if isinstance(rows, list) and not rows:
                            break
                # Follow next_request cursor within the year.
                next_req = None
                if isinstance(body, dict):
                    d = body.get("data")
                    if isinstance(d, dict):
                        next_req = d.get("next_request")
                    if next_req is None:
                        next_req = body.get("next_request")
                if not next_req:
                    break
                if isinstance(next_req, str):
                    if next_req.startswith("http"):
                        cur_url = next_req
                    elif next_req.startswith("/"):
                        cur_url = f"{parts.scheme}://{parts.netloc}{next_req}"
                    elif next_req.startswith("?"):
                        cur_url = f"{parts.scheme}://{parts.netloc}{parts.path}{next_req}"
                    else:
                        break
                elif isinstance(next_req, dict):
                    nxt_u = next_req.get("url") or next_req.get("href")
                    if not nxt_u:
                        break
                    cur_url = nxt_u if nxt_u.startswith("http") else f"{parts.scheme}://{parts.netloc}{nxt_u}"
                    nxt_p = next_req.get("params") or next_req.get("query")
                    if isinstance(nxt_p, dict) and nxt_p:
                        sep = "&" if "?" in cur_url else "?"
                        cur_url = f"{cur_url}{sep}{_u.urlencode(nxt_p, doseq=True)}"
                else:
                    break
                page_idx += 1
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
        # Look for an Export button on the Surveys page and click it —
        # captures whatever endpoint the export fires (likely a CSV
        # download or a /export endpoint). We don't actually need the
        # download blob since on_response will pick up the URL; we
        # just need the SPA to emit the request so we can re-use it.
        if "Surveys" in sub or "Reviews" in sub or "Comments" in sub:
            for sel in [
                'button:has-text("Export")',
                'button:has-text("Download")',
                'a:has-text("Export")',
                '[aria-label*="export" i]',
                '[data-testid*="export" i]',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn and btn.count() > 0 and btn.is_visible(timeout=1000):
                        print(f"  [{slug}] found export-like control: {sel}")
                        # Race a download event with a navigation guard —
                        # if the click triggers a CSV download, we save
                        # it; otherwise the on_response listener catches
                        # the XHR.
                        try:
                            with page.expect_download(timeout=20_000) as dl_info:
                                btn.click(timeout=5000)
                            dl = dl_info.value
                            csv_path = f"/tmp/resy_export_{slug.replace('/', '_')}.csv"
                            dl.save_as(csv_path)
                            print(f"  [{slug}] export saved to {csv_path}")
                        except PWTimeout:
                            # No download fired — the click probably
                            # triggered an XHR which on_response caught.
                            print(f"  [{slug}] export click fired no download (XHR path)")
                        except Exception as e:
                            print(f"  [{slug}] export click failed: {e}")
                        page.wait_for_timeout(5000)  # let any XHR settle
                        break
                except Exception:
                    continue
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
    # Diagnostic: log the actual URLs the SPA fired and the response
    # shapes so we can identify pagination params Resy expects (which
    # aren't `limit`/`offset` since those 500). Without this we're
    # debugging blind.
    for c in captured:
        c_url = c.get("url", "")
        if any(t in c_url.lower() for t in ("survey", "rating", "feedback", "review")):
            body = c.get("json")
            top_keys = list(body.keys())[:8] if isinstance(body, dict) else type(body).__name__
            data_node = body.get("data") if isinstance(body, dict) else None
            data_keys = list(data_node.keys())[:8] if isinstance(data_node, dict) else (
                f"list[{len(data_node)}]" if isinstance(data_node, list) else None
            )
            row_count = None
            if isinstance(data_node, dict):
                for k in ("surveys", "rows", "items", "results"):
                    v = data_node.get(k)
                    if isinstance(v, list):
                        row_count = f"{k}={len(v)}"
                        break
            elif isinstance(data_node, list):
                row_count = f"data={len(data_node)}"
            meta_node = body.get("meta") if isinstance(body, dict) else None
            meta_keys = list(meta_node.keys())[:10] if isinstance(meta_node, dict) else None
            print(f"  [diag] method={c.get('method')} url={c_url}")
            print(f"  [diag]   top_keys={top_keys} data_keys={data_keys} rows={row_count}")
            if meta_keys:
                print(f"  [diag]   meta_keys={meta_keys} meta={meta_node}")
    # Re-issue each captured survey URL with a bumped `limit` to fetch
    # the venue's full history (not just the SPA's first page of ~20).
    # Free-text comments are only useful for period filtering when we
    # have all rows, not just the most recent. See expand_via_limit_bump.
    expanded = expand_via_limit_bump(page, captured)
    if expanded:
        print(f"    full-history fetch: captured {len(expanded)} additional response(s)")
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

    # Schema-drift dump — surfaces evidence when our keyword router
    # silently fails to bucket score fields (e.g. food/service/atmos/
    # sentiment/recommend all null on a row that DID have responses).
    # The Apr 2026 incident: dashboard NPS card flatlined because
    # every survey post 2026-04-16 had a non-empty responses[] but no
    # parseable score in any of them — the parser got nothing back from
    # _question_text. This dump tells you what changed.
    if _DRIFT_SAMPLES:
        sys.stderr.write(
            f"\n[schema-drift] {len(_DRIFT_SAMPLES)} survey row(s) had "
            "responses[] but no parseable score buckets. Sample shape(s):\n"
        )
        for i, sample in enumerate(_DRIFT_SAMPLES, 1):
            sys.stderr.write(
                f"  [{i}] date={sample['date']} responses_count="
                f"{sample['responses_count']} row_keys={sample['row_keys']}\n"
            )
            for j, r in enumerate(sample['first_3_responses'], 1):
                sys.stderr.write(f"      response[{j}]: {json.dumps(r, default=str)}\n")
        sys.stderr.write(
            "  → Update transform_resy_survey_row keyword routes in "
            "resy_os_scraper.py based on the question/response shapes above.\n"
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
