#!/usr/bin/env python3
"""
Method Co — Resy guest-experience sync.

Replaces the static "* - NPS Report.html" exports with a live pull straight
from Resy. Authenticates against Resy's web API as a venue operator, fetches
the same survey + 1-5 ratings + comment data those reports were generated
from, and merges it into data/<outlet>.json under the `guest` key — same
shape the dashboard renderer (renderGuestSection) already consumes.

Setup (one-time):
  1. GitHub Secrets:
       RESY_EMAIL    — Resy OS account email (must have venue operator access)
       RESY_PASSWORD — Resy OS account password
       RESY_VENUES   — outlet_id=venue_id;outlet_id=venue_id;...
                       e.g. lsbr=12345;lowland=67890;hiroki_phl=11111;
                       Get venue_ids by running:  python3 resy_sync.py --probe
  2. Optional overrides:
       RESY_API_KEY  — public Resy frontend API key (default ships with this
                       file; override only if Resy rotates it)
       RESY_BASE     — API host (default https://api.resy.com)
       DAYS_BACK     — lookback per run (default 400)

Usage:
  python3 resy_sync.py                    # sync all configured venues
  python3 resy_sync.py --outlet lsbr      # one outlet
  python3 resy_sync.py --probe            # auth-only + endpoint discovery
  python3 resy_sync.py --dry-run          # no network, write fixture

Behavior:
  - Append-merges with the existing guest block (dedup on date+server+overall+
    covers natural key) so historical seed data survives indefinitely.
  - Atomic write via .tmp.
  - Exits 0 on missing creds (lets the nightly workflow run before secrets
    are populated), exits 1 only on real fetch errors.

Discovery note:
  The exact Resy OS endpoint paths for surveys/ratings have not been pinned
  here yet — Resy doesn't publish them and they shift periodically. `--probe`
  authenticates with your real creds and walks a list of candidate URLs,
  reporting which respond with usable JSON. Once `--probe` finds the working
  paths for your account, lock them into RESY_FEEDBACK_PATH / RESY_RATINGS_PATH
  env (or edit _ENDPOINT_CANDIDATES below) and the nightly job is good to go.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


# ---------- config ----------

RESY_BASE        = (os.environ.get("RESY_BASE") or "https://api.resy.com").rstrip("/")
# Public Resy frontend API key. Ships in their web bundle; same value Resy OS
# uses. Override via env if Resy rotates it.
RESY_API_KEY     = os.environ.get("RESY_API_KEY") or "VbWk7s3L4KiK5fzlO7GBSdCnG62tqz3o"
DAYS_BACK        = int(os.environ.get("DAYS_BACK", "400"))
REQUEST_TIMEOUT  = 45
USER_AGENT       = "MethodCo-Dashboards/1.0 (resy_sync.py; +https://github.com/rrmethodco)"

# Candidate endpoints to probe — Resy keeps these undocumented. First one that
# returns 200 + parseable JSON for a given venue/date-range wins. Override via
# RESY_FEEDBACK_PATH / RESY_RATINGS_PATH env vars (semicolon-separated lists).
_DEFAULT_FEEDBACK_CANDIDATES = [
    "/3/venue/{venue_id}/feedback?start_date={start}&end_date={end}",
    "/3/owner/feedback?venue_id={venue_id}&start_date={start}&end_date={end}",
    "/2/venue/{venue_id}/feedback?start_date={start}&end_date={end}",
    "/3/venue/{venue_id}/surveys?start_date={start}&end_date={end}",
    "/3/owner/surveys?venue_id={venue_id}&start_date={start}&end_date={end}",
]
_DEFAULT_RATINGS_CANDIDATES = [
    "/3/venue/{venue_id}/ratings?start_date={start}&end_date={end}",
    "/3/owner/ratings?venue_id={venue_id}&start_date={start}&end_date={end}",
    "/2/venue/{venue_id}/ratings?start_date={start}&end_date={end}",
]

def _split_env(name: str, default: list[str]) -> list[str]:
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    return [p.strip() for p in v.split(";") if p.strip()]

FEEDBACK_CANDIDATES = _split_env("RESY_FEEDBACK_PATH", _DEFAULT_FEEDBACK_CANDIDATES)
RATINGS_CANDIDATES  = _split_env("RESY_RATINGS_PATH",  _DEFAULT_RATINGS_CANDIDATES)


# ---------- venue config ----------


def parse_venues() -> dict[str, str]:
    """Parse RESY_VENUES into {outlet_id: venue_id}.

    Format:  outlet_id=venue_id;outlet_id=venue_id;...
    Example: lsbr=12345;lowland=67890;hiroki_phl=11111

    venue_id is Resy's numeric internal ID for the venue (NOT the URL slug).
    Find it via `--probe` output or the Resy OS URL bar.
    """
    raw = (os.environ.get("RESY_VENUES") or "").strip()
    out: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        oid, vid = chunk.split("=", 1)
        oid = oid.strip(); vid = vid.strip()
        if oid and vid:
            out[oid] = vid
    return out


# ---------- auth ----------


def auth_login(email: str, password: str) -> dict[str, Any]:
    """POST /3/auth/password → {auth_token, id, ...}.

    Resy's auth endpoint takes form-encoded creds (not JSON) and the public
    frontend API key in the Authorization header. The returned auth_token
    goes in `X-Resy-Auth-Token` for subsequent calls.
    """
    url = f"{RESY_BASE}/3/auth/password"
    headers = {
        "Authorization": f'ResyAPI api_key="{RESY_API_KEY}"',
        "User-Agent": USER_AGENT,
        "Origin": "https://widgets.resy.com",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = requests.post(url, headers=headers, data={"email": email, "password": password},
                      timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise SystemExit(f"resy auth failed: HTTP {r.status_code} {r.text[:200]}")
    js = r.json()
    token = js.get("token") or js.get("auth_token") or js.get("access_token")
    if not token:
        raise SystemExit(f"resy auth: no token in response keys={list(js.keys())[:8]}")
    js["_token"] = token
    return js


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f'ResyAPI api_key="{RESY_API_KEY}"',
        "X-Resy-Auth-Token": token,
        "User-Agent": USER_AGENT,
        "Origin": "https://widgets.resy.com",
        "Accept": "application/json",
    }


# ---------- fetchers ----------


def _try_get(headers: dict[str, str], path: str, *, venue_id: str, start: str, end: str) -> tuple[int, Any]:
    url = RESY_BASE + path.format(venue_id=venue_id, start=start, end=end)
    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return -1, {"error": str(e), "url": url}
    body: Any
    try:
        body = r.json()
    except Exception:
        body = r.text[:300]
    return r.status_code, body


def fetch_feedback(headers: dict[str, str], venue_id: str, start: str, end: str) -> list[dict] | None:
    """Walk FEEDBACK_CANDIDATES, return first endpoint that responds with a
    list-shaped JSON body (or {results: [...]}). Empty list is still a
    successful answer (just no surveys in range)."""
    for path in FEEDBACK_CANDIDATES:
        sc, body = _try_get(headers, path, venue_id=venue_id, start=start, end=end)
        rows = _coerce_rows(body)
        if sc == 200 and rows is not None:
            return rows
    return None


def fetch_ratings(headers: dict[str, str], venue_id: str, start: str, end: str) -> list[dict] | None:
    for path in RATINGS_CANDIDATES:
        sc, body = _try_get(headers, path, venue_id=venue_id, start=start, end=end)
        rows = _coerce_rows(body)
        if sc == 200 and rows is not None:
            return rows
    return None


def _coerce_rows(body: Any) -> list[dict] | None:
    """Resy responses come back as either a bare list or {results:[...]} /
    {data:[...]} / {feedback:[...]}. Normalize."""
    if isinstance(body, list):
        return body if all(isinstance(x, dict) for x in body) else None
    if isinstance(body, dict):
        for key in ("results", "data", "feedback", "items", "ratings", "surveys"):
            v = body.get(key)
            if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                return v
    return None


# ---------- transform ----------

# Resy survey field aliases — Resy has shipped at least three different
# field-name conventions over the years (camelCase, snake_case, abbreviated).
# Map them to the dashboard's canonical names. Add new aliases as discovered.
_SURVEY_FIELD_ALIASES = {
    "date":      ("date", "diningDate", "dining_date", "reservationDate", "created_at"),
    "overall":   ("overall", "overallScore", "overall_score"),
    "sentiment": ("sentiment", "sentimentScore", "sentiment_score"),
    "service":   ("service", "serviceScore", "service_score"),
    "food":      ("food", "foodScore", "food_score"),
    "atmos":     ("atmos", "atmosphere", "atmosphereScore", "atmos_score"),
    "server":    ("server", "serverName", "server_name", "host"),
    "recommend": ("recommend", "recommendScore", "nps", "npsScore", "nps_score"),
    "covers":    ("covers", "partySize", "party_size", "guests"),
    "dow":       ("dow", "dayOfWeek", "day_of_week"),
    "hour":      ("hour", "hourOfDay", "hour_of_day"),
}

def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def normalize_surveys(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = _pick(r, _SURVEY_FIELD_ALIASES["date"])
        if not isinstance(d, str) or len(d) < 10:
            continue
        rec = {k: _pick(r, aliases) for k, aliases in _SURVEY_FIELD_ALIASES.items()}
        rec["date"] = d[:10]
        # If dow/hour absent, derive from date+timestamp when possible.
        if rec["dow"] is None:
            try:
                # Resy convention used in our seed data: 0=Mon … 6=Sun.
                rec["dow"] = datetime.strptime(rec["date"], "%Y-%m-%d").weekday()
            except Exception:
                pass
        out.append(rec)
    return out


def normalize_ratings(rows: list[dict]) -> list[dict]:
    """Coerce to the {date, r1..r5} shape. Resy ratings endpoints sometimes
    return per-row stars (1-5) instead of pre-bucketed counts — handle both."""
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        d = _pick(r, ("date", "createdAt", "created_at", "diningDate", "dining_date"))
        if not isinstance(d, str) or len(d) < 10:
            continue
        d = d[:10]
        bucket = out.setdefault(d, {"date": d, "r1": 0, "r2": 0, "r3": 0, "r4": 0, "r5": 0})
        # Already-bucketed shape
        if any(k in r for k in ("r1", "r2", "r3", "r4", "r5")):
            for k in ("r1", "r2", "r3", "r4", "r5"):
                bucket[k] += int(r.get(k) or 0)
            continue
        # Per-row star shape
        stars = r.get("stars") or r.get("rating") or r.get("score")
        try:
            n = int(stars)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 5:
            bucket[f"r{n}"] += 1
    return sorted(out.values(), key=lambda x: x["date"])


# ---------- merge ----------


def _survey_key(s: dict) -> tuple:
    """Natural dedup key for a survey row."""
    return (s.get("date"), (s.get("server") or "").strip(), s.get("overall"),
            s.get("recommend"), s.get("covers"), s.get("hour"))


def merge_guest(existing: dict | None, fresh: dict) -> dict:
    """Append-merge fresh into existing, dedup on natural keys.

    `fresh` keys: surveys, ratings (no comments via API for now), google.
    Existing google block is preserved (Google sync is a separate script).
    """
    out = dict(existing) if existing else {}
    out["as_of"] = fresh.get("as_of") or date.today().isoformat()
    out["source"] = "resy_sync"

    # Surveys — dedup-merge
    seen = {_survey_key(s) for s in (out.get("surveys") or [])}
    surveys = list(out.get("surveys") or [])
    for s in fresh.get("surveys") or []:
        if _survey_key(s) not in seen:
            surveys.append(s); seen.add(_survey_key(s))
    out["surveys"] = sorted(surveys, key=lambda x: x.get("date") or "", reverse=True)

    # Ratings — by date, take max so same-day re-pull doesn't double-count
    rmap: dict[str, dict] = {r["date"]: r for r in (out.get("ratings") or [])}
    for r in fresh.get("ratings") or []:
        prev = rmap.get(r["date"])
        if not prev:
            rmap[r["date"]] = r
        else:
            for k in ("r1", "r2", "r3", "r4", "r5"):
                prev[k] = max(prev.get(k, 0), r.get(k, 0))
    out["ratings"] = sorted(rmap.values(), key=lambda x: x["date"])

    return out


# ---------- IO ----------


def load_outlet(data_dir: Path, outlet_id: str) -> dict:
    p = data_dir / f"{outlet_id}.json"
    if not p.exists():
        return {"outlet_id": outlet_id}
    return json.loads(p.read_text(encoding="utf-8"))


def write_outlet(data_dir: Path, outlet_id: str, payload: dict) -> None:
    p = data_dir / f"{outlet_id}.json"
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(p)


# ---------- main ----------


def cmd_probe(email: str, password: str, venues: dict[str, str]) -> int:
    """Authenticate, then walk the candidate endpoints for each venue and
    print which ones respond with usable data. Use the output to lock the
    correct paths into RESY_FEEDBACK_PATH / RESY_RATINGS_PATH."""
    print(f"Authenticating as {email}...")
    sess = auth_login(email, password)
    print(f"  ✓ token={sess['_token'][:16]}...   user_id={sess.get('id')}")
    headers = auth_headers(sess["_token"])
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=30)).isoformat()
    print(f"\nProbing endpoints with date window {start} → {end}\n")
    for oid, vid in venues.items():
        print(f"--- {oid} (venue_id={vid}) ---")
        for label, candidates in (("FEEDBACK", FEEDBACK_CANDIDATES), ("RATINGS", RATINGS_CANDIDATES)):
            print(f"  [{label}]")
            for path in candidates:
                sc, body = _try_get(headers, path, venue_id=vid, start=start, end=end)
                rows = _coerce_rows(body)
                if sc == 200 and rows is not None:
                    print(f"    ✓ HTTP 200, {len(rows):>4} rows  ← {path}")
                    if rows:
                        print(f"        sample keys: {sorted(rows[0].keys())[:10]}")
                else:
                    snippet = json.dumps(body)[:120] if isinstance(body, (dict, list)) else str(body)[:120]
                    print(f"    · HTTP {sc:>4}                ← {path}    {snippet}")
        print()
    return 0


def cmd_sync(email: str, password: str, venues: dict[str, str], data_dir: Path,
             only: str | None, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] skipping network; writing fixture survey to data/_resy_dry_run.json")
        fixture = {
            "as_of": date.today().isoformat(), "source": "resy_dry_run",
            "surveys": [{"date": date.today().isoformat(), "overall": 100, "sentiment": 100,
                         "service": 100, "food": 100, "atmos": 100, "server": "TEST",
                         "recommend": 10, "covers": 2, "dow": date.today().weekday(), "hour": 19}],
            "ratings": [{"date": date.today().isoformat(), "r1": 0, "r2": 0, "r3": 0, "r4": 0, "r5": 1}],
        }
        (data_dir / "_resy_dry_run.json").write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    sess = auth_login(email, password)
    headers = auth_headers(sess["_token"])
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    print(f"Window: {start} → {end}")

    targets = {oid: vid for oid, vid in venues.items() if not only or oid == only}
    if not targets:
        sys.stderr.write(f"no matching venues (only='{only}', configured={list(venues.keys())})\n")
        return 1

    failures: list[str] = []
    for oid, vid in targets.items():
        print(f"\n[{oid}] venue_id={vid}")
        feedback = fetch_feedback(headers, vid, start, end)
        ratings  = fetch_ratings(headers, vid, start, end)
        if feedback is None and ratings is None:
            print(f"  ✗ no working endpoints — run --probe and lock RESY_FEEDBACK_PATH/RESY_RATINGS_PATH")
            failures.append(oid); continue

        surveys_norm = normalize_surveys(feedback or [])
        ratings_norm = normalize_ratings(ratings or [])
        print(f"  ✓ feedback: {len(feedback or [])} raw → {len(surveys_norm)} surveys")
        print(f"  ✓ ratings:  {len(ratings or [])} raw → {len(ratings_norm)} day buckets")

        payload = load_outlet(data_dir, oid)
        existing = payload.get("guest") or {}
        merged = merge_guest(existing, {"surveys": surveys_norm, "ratings": ratings_norm,
                                        "as_of": date.today().isoformat()})
        # Preserve any pre-existing google block (separate script owns it).
        if "google" in existing and "google" not in merged:
            merged["google"] = existing["google"]
        payload["guest"] = merged
        write_outlet(data_dir, oid, payload)
        print(f"  → wrote data/{oid}.json   surveys={len(merged.get('surveys') or [])}  ratings={len(merged.get('ratings') or [])}")

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync Resy guest-experience data into data/<outlet>.json")
    ap.add_argument("--outlet", help="single outlet id to sync (default: all configured)")
    ap.add_argument("--data-dir", default="../data", help="dir of <outlet>.json files (default: ../data)")
    ap.add_argument("--probe", action="store_true", help="auth + endpoint discovery; no writes")
    ap.add_argument("--dry-run", action="store_true", help="no network; emit fixture")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    email = os.environ.get("RESY_EMAIL")
    password = os.environ.get("RESY_PASSWORD")
    venues = parse_venues()

    if args.dry_run:
        return cmd_sync("dry@dry", "dry", venues or {"_dry": "0"}, data_dir, args.outlet, dry_run=True)

    if not (email and password):
        sys.stderr.write("RESY_EMAIL / RESY_PASSWORD missing — exiting cleanly (no-op)\n")
        return 0
    if not venues:
        sys.stderr.write("RESY_VENUES is empty — nothing to sync\n")
        return 0

    if args.probe:
        return cmd_probe(email, password, venues)
    return cmd_sync(email, password, venues, data_dir, args.outlet, dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())
