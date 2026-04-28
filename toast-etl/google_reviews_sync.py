#!/usr/bin/env python3
"""
Method Co — Google Reviews sync for the Guest Experience tab.

Pulls Place Details (rating, total review count, recent reviews) for each
configured outlet via the Google Places API and merges them into
data/<outlet>.json under guest.google. Designed to sit alongside resy_sync.py
on the same nightly schedule.

Setup (one-time):
  1. Create a GCP project; enable the Places API; mint an API key.
  2. Find each outlet's `place_id` (one-time, run with creds locally):
        python3 google_reviews_sync.py --lookup
     This searches each outlet's known name + city via Find Place From Text
     and prints the top match. Paste the place_ids into the GOOGLE_PLACES env.
  3. GitHub Secrets:
        GOOGLE_PLACES_API_KEY  — the API key
        GOOGLE_PLACES          — outlet_id=ChIJ...;outlet_id=ChIJ...;...

Usage:
  python3 google_reviews_sync.py                   # sync all
  python3 google_reviews_sync.py --outlet lowland  # one outlet
  python3 google_reviews_sync.py --lookup          # discover place_ids
  python3 google_reviews_sync.py --dry-run         # no network, fixture

Coverage notes:
  - Places API returns up to 5 most-relevant reviews per call. Full
    distribution (1★/2★/3★/4★/5★ counts), owner reply rate, and monthly
    trend require SerpAPI or scraping; this script PRESERVES those fields
    from the existing seed data. Re-seed via SerpAPI later if needed.
  - Free Places API tier covers small daily call volumes; ~11 outlets ×
    nightly = ~330 calls/month, well inside the free quota.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


GOOGLE_BASE     = "https://maps.googleapis.com/maps/api/place"
REQUEST_TIMEOUT = 30
PLACE_FIELDS    = "name,rating,user_ratings_total,reviews,formatted_address,place_id,url"


# Default search hints used by --lookup. Keyed by outlet_id (the data/<id>.json
# basename, NOT the dashboard's inline DATA outlet key — same convention as
# toast_sync / resy_sync).
_DEFAULT_LOOKUP_HINTS: dict[str, tuple[str, str]] = {
    "lsbr":          ("Le Suprême",            "Detroit MI"),
    "hiroki_phl":    ("HIROKI",                "Philadelphia PA"),
    "hiroki_det":    ("HIROKI-SAN",            "Detroit MI"),
    "kampers":       ("Kamper's Rooftop",      "Detroit MI"),
    "lowland":       ("Lowland",                       "Charleston SC"),
    "mulherins":     ("Wm. Mulherin's Sons",           "Philadelphia PA"),
    "anthology":     ("Anthology",                     "Detroit MI"),
    "little_wing":   ("Little Wing Coffee and Goods",  "Baltimore MD"),
    "quoin":         ("The Quoin",             "Wilmington DE"),
    "rosemary_rose": ("Rosemary & Rose",       "Philadelphia PA"),
    "vessel":        ("Vessel",                "Detroit MI"),
}


def parse_places() -> dict[str, str]:
    """Parse GOOGLE_PLACES into {outlet_id: place_id}.

    Format: outlet_id=ChIJ...;outlet_id=ChIJ...;...
    """
    raw = (os.environ.get("GOOGLE_PLACES") or "").strip()
    out: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        oid, pid = chunk.split("=", 1)
        oid = oid.strip(); pid = pid.strip()
        if oid and pid:
            out[oid] = pid
    return out


def find_place(api_key: str, query: str) -> dict | None:
    url = f"{GOOGLE_BASE}/findplacefromtext/json"
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id,name,formatted_address,rating,user_ratings_total",
        "key": api_key,
    }
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return None
    js = r.json()
    cands = js.get("candidates") or []
    return cands[0] if cands else None


def place_details(api_key: str, place_id: str) -> dict | None:
    url = f"{GOOGLE_BASE}/details/json"
    params = {"place_id": place_id, "fields": PLACE_FIELDS, "key": api_key}
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        sys.stderr.write(f"place_details HTTP {r.status_code} for {place_id}\n")
        return None
    js = r.json()
    if js.get("status") != "OK":
        sys.stderr.write(f"place_details status={js.get('status')} for {place_id}: {js.get('error_message','')}\n")
        return None
    return js.get("result")


def to_google_block(detail: dict, existing: dict | None) -> dict:
    """Project Place Details into the dashboard's google block shape, preserving
    fields the Places API can't refresh (monthly trend, full distribution,
    owner reply stats) from the existing seed."""
    samples = []
    for rev in (detail.get("reviews") or [])[:5]:
        samples.append({
            "author":   rev.get("author_name"),
            "rating":   rev.get("rating"),
            "text":     (rev.get("text") or "")[:1000],
            "relative": rev.get("relative_time_description"),
            "time":     rev.get("time"),
            "is_local_guide": bool(rev.get("author_url") and rev.get("rating") is not None
                                   and "Local Guide" in (rev.get("author_attribution") or "")),
        })
    block = {
        "venue":          detail.get("name"),
        "place_id":       detail.get("place_id"),
        "formatted_address": detail.get("formatted_address"),
        "url":            detail.get("url"),
        "avg_rating":     detail.get("rating"),
        "google_published_rating": detail.get("rating"),
        "total_reviews":  detail.get("user_ratings_total"),
        "samples":        samples,
        "as_of":          date.today().isoformat(),
        "source":         "google_places_api",
    }
    # Preserve fields the Places API doesn't expose (carry forward from seed).
    for k in ("monthly", "daily_last30", "distribution_full", "owner_reply_rate",
              "owner_reply_count", "local_guide_count"):
        if existing and existing.get(k) is not None:
            block[k] = existing[k]
    return block


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


def cmd_lookup(api_key: str) -> int:
    print("Looking up Google Place IDs for each outlet...\n")
    print(f"{'outlet':<14} {'★':<5} {'reviews':<8} {'place_id':<35}  formatted_address")
    print("-" * 110)
    pairs = []
    for oid, (name, region) in _DEFAULT_LOOKUP_HINTS.items():
        q = f"{name} {region}"
        cand = find_place(api_key, q)
        if not cand:
            print(f"{oid:<14} —     —        —                                    (no match for: {q})")
            continue
        pid = cand.get("place_id") or ""
        rating = cand.get("rating")
        n = cand.get("user_ratings_total")
        addr = cand.get("formatted_address", "")
        rstr = f"{rating:.2f}" if isinstance(rating, (int, float)) else "—"
        nstr = f"{n}" if n else "—"
        print(f"{oid:<14} {rstr:<5} {nstr:<8} {pid:<35}  {addr}")
        pairs.append(f"{oid}={pid}")
    print("\n--- Paste into GOOGLE_PLACES secret ---")
    print(";".join(pairs))
    return 0


def cmd_sync(api_key: str, places: dict[str, str], data_dir: Path,
             only: str | None, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] no network; writing fixture to data/_google_dry_run.json")
        fixture = {
            "venue": "TEST", "place_id": "ChIJ_dry_run", "avg_rating": 4.5,
            "total_reviews": 100, "samples": [], "as_of": date.today().isoformat(),
            "source": "google_dry_run",
        }
        (data_dir / "_google_dry_run.json").write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    targets = {oid: pid for oid, pid in places.items() if not only or oid == only}
    if not targets:
        sys.stderr.write(f"no matching place_ids (only='{only}', configured={list(places.keys())})\n")
        return 1

    failures: list[str] = []
    for oid, pid in targets.items():
        detail = place_details(api_key, pid)
        if not detail:
            failures.append(oid); continue
        payload = load_outlet(data_dir, oid)
        existing_guest = payload.get("guest") or {}
        existing_google = existing_guest.get("google") or {}
        new_google = to_google_block(detail, existing_google)
        # Surveys/ratings/comments are owned by resy_sync — never touch them.
        existing_guest["google"] = new_google
        if "as_of" not in existing_guest:
            existing_guest["as_of"] = date.today().isoformat()
        payload["guest"] = existing_guest
        write_outlet(data_dir, oid, payload)
        print(f"  ✓ {oid:<14} {detail.get('rating'):.2f}★ · {detail.get('user_ratings_total')} reviews · {detail.get('name')}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync Google Reviews into data/<outlet>.json")
    ap.add_argument("--outlet", help="single outlet id (default: all configured)")
    ap.add_argument("--data-dir", default="../data", help="dir of <outlet>.json files")
    ap.add_argument("--lookup", action="store_true", help="search Place IDs for each outlet, no writes")
    ap.add_argument("--dry-run", action="store_true", help="no network, fixture")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    places  = parse_places()

    if args.dry_run:
        return cmd_sync("DRY", places, data_dir, args.outlet, dry_run=True)

    if not api_key:
        sys.stderr.write("GOOGLE_PLACES_API_KEY missing — exiting cleanly (no-op)\n")
        return 0

    if args.lookup:
        return cmd_lookup(api_key)

    if not places:
        sys.stderr.write("GOOGLE_PLACES is empty — run --lookup to discover place_ids\n")
        return 0

    return cmd_sync(api_key, places, data_dir, args.outlet, dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())
