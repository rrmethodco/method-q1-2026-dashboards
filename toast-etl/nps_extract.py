#!/usr/bin/env python3
"""Extract Resy survey + Google review data from per-outlet NPS Report HTML
files and merge a `guest` block into each data/<outlet>.json.

Source HTMLs embed four globals near the top of the file:
    window.RAW_SURVEYS  — per-survey rows (overall/sentiment/service/food/atmos
                          scores 0-100, server, recommend 0-10, covers, dow, hour)
    window.RAW_RATINGS  — daily Resy 1-5 star distribution (r1..r5 counts)
    window.RAW_COMMENTS — comment metadata (date/recommend/server/overall — no
                          comment text in current exports; tracked for response
                          rate)
    window.GOOGLE_DATA  — Google Reviews aggregate (total/avg/monthly/samples/
                          owner_reply_rate/distribution_full)

Output schema attached as `guest` to each data/<outlet>.json:
    guest: {
      as_of: ISO date,
      source_file: str,
      surveys:  [ {date, overall, sentiment, service, food, atmos,
                   server, recommend, covers, dow, hour}, ... ],
      ratings:  [ {date, r1, r2, r3, r4, r5}, ... ],
      comments: [ {date, recommend, server, overall}, ... ],
      google:   { venue, total_reviews, avg_rating, owner_reply_rate,
                  monthly:[...], distribution_full:{...}, samples:[...] },
    }

The dashboard's runtime loader merges payload.guest into DATA.outlets[id] and
flips sources.resy on, so the chip lights up and renderGuestSection() picks it
up. Aggregations (NPS, monthly trends, server rollups, daypart) are computed
client-side from `surveys` so the period filter that drives the rest of the
dashboard also drives the guest section.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Outlet-id (data/<oid>.json basename) → NPS report HTML filename.
# Multiple HTMLs exist for some venues (older vs. newer snapshot, accented vs.
# unaccented filename). We resolve to the freshest file (largest mtime) when
# multiple candidates are listed.
_OUTLET_NPS_CANDIDATES: dict[str, list[str]] = {
    # lsbr is Le Suprême + Bar Rotunda combined; Resy survey export covers
    # the Le Suprême RC only (Bar Rotunda is a service bar, no Resy surveys).
    "lsbr":       ["Le Suprême - NPS Report.html", "Le Supreme - NPS Report.html"],
    "hiroki_phl": ["HIROKI Philadelphia - NPS Report.html"],
    "hiroki_det": ["HIROKI-SAN Detroit - NPS Report.html"],
    "kampers":    ["Kampers - NPS Report.html"],
    "lowland":    ["Lowland - NPS Report.html"],
    "mulherins":  ["Wm Mulherin's Sons - NPS Report.html",
                   "Wm Mulherins Sons - NPS Report.html"],
}

# Match `window.NAME = <json-literal> ;` allowing greedy-but-safe capture up to
# the next semicolon-newline. Works because the Resy NPS exporter writes each
# global on its own line and never embeds a `;\n` in the JSON values.
def _extract_window_var(html: str, name: str) -> Any | None:
    pat = rf'window\.{name}\s*=\s*(\[[\s\S]*?\]|\{{[\s\S]*?\}})\s*;\s*\n'
    m = re.search(pat, html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _resolve_nps_path(repo_root: Path, candidates: list[str]) -> Path | None:
    found: list[Path] = []
    for fn in candidates:
        p = repo_root / fn
        if p.exists():
            found.append(p)
    if not found:
        return None
    # Prefer the largest file (most surveys / freshest snapshot).
    return max(found, key=lambda p: p.stat().st_size)


def _sanitize_surveys(rows: list[dict]) -> list[dict]:
    """Coerce types, drop rows that lack a usable date or recommend score."""
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or len(d) < 10:
            continue
        rec = r.get("recommend")
        if not isinstance(rec, (int, float)):
            continue
        out.append({
            "date":      d[:10],
            "overall":   r.get("overall"),
            "sentiment": r.get("sentiment"),
            "service":   r.get("service"),
            "food":      r.get("food"),
            "atmos":     r.get("atmos"),
            "server":    r.get("server"),
            "recommend": rec,
            "covers":    r.get("covers"),
            "dow":       r.get("dow"),
            "hour":      r.get("hour"),
        })
    return out


def _sanitize_ratings(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or len(d) < 10:
            continue
        out.append({
            "date": d[:10],
            "r1": int(r.get("r1") or 0),
            "r2": int(r.get("r2") or 0),
            "r3": int(r.get("r3") or 0),
            "r4": int(r.get("r4") or 0),
            "r5": int(r.get("r5") or 0),
        })
    return out


def _sanitize_comments(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or len(d) < 10:
            continue
        out.append({
            "date":      d[:10],
            "recommend": r.get("recommend"),
            "server":    r.get("server"),
            "overall":   r.get("overall"),
        })
    return out


def _sanitize_google(g: dict | None) -> dict | None:
    if not isinstance(g, dict):
        return None
    keep = ("venue", "total_reviews", "avg_rating", "owner_reply_rate",
            "owner_reply_count", "local_guide_count", "google_published_rating",
            "monthly", "daily_last30", "distribution_full", "samples")
    return {k: g[k] for k in keep if k in g}


def extract_one(html_path: Path) -> dict[str, Any]:
    src = html_path.read_text(encoding="utf-8")
    surveys  = _extract_window_var(src, "RAW_SURVEYS")  or []
    ratings  = _extract_window_var(src, "RAW_RATINGS")  or []
    comments = _extract_window_var(src, "RAW_COMMENTS") or []
    google   = _extract_window_var(src, "GOOGLE_DATA")

    return {
        "as_of":       date.today().isoformat(),
        "source_file": html_path.name,
        "surveys":     _sanitize_surveys(surveys),
        "ratings":     _sanitize_ratings(ratings),
        "comments":    _sanitize_comments(comments),
        "google":      _sanitize_google(google),
    }


def merge_into_outlet(data_path: Path, guest: dict[str, Any]) -> None:
    payload = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else {}
    payload["guest"] = guest
    # Match toast_sync.py's `indent=2` so git diffs stay reviewable.
    data_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract Resy/Google NPS data into data/*.json")
    p.add_argument("--repo-root", default=".", help="repo root containing NPS HTMLs")
    p.add_argument("--data-dir",  default="data", help="dir of <outlet>.json files")
    p.add_argument("--only", help="comma-separated outlet ids to process (default: all)")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    data_dir  = Path(args.data_dir).resolve()
    if not data_dir.exists():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    only = set(s.strip() for s in args.only.split(",")) if args.only else None
    targets = {oid: cands for oid, cands in _OUTLET_NPS_CANDIDATES.items()
               if not only or oid in only}

    ok = 0
    skipped: list[str] = []
    for oid, candidates in targets.items():
        html = _resolve_nps_path(repo_root, candidates)
        if html is None:
            print(f"  [skip] {oid} — no NPS HTML found (looked for: {candidates})", file=sys.stderr)
            skipped.append(oid)
            continue
        guest = extract_one(html)
        n_s = len(guest["surveys"])
        n_r = sum(r["r1"] + r["r2"] + r["r3"] + r["r4"] + r["r5"] for r in guest["ratings"])
        n_c = len(guest["comments"])
        g_yn = "Y" if guest["google"] else "n"
        data_path = data_dir / f"{oid}.json"
        merge_into_outlet(data_path, guest)
        print(f"  [ok]   {oid:<12} ← {html.name}   surveys={n_s} ratings={n_r} comments={n_c} google={g_yn}")
        ok += 1

    print(f"\nUpdated {ok} outlet(s); skipped {len(skipped)}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
