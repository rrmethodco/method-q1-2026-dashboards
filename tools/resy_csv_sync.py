#!/usr/bin/env python3
"""
resy_csv_sync.py — local-on-Mac Resy survey scraper

Drives Resy OS in a headless Chromium with the operator's stored login,
clicks the Export CSV button on each venue's Surveys page, saves each
CSV under a date-stamped folder, then ingests every CSV into the
dashboard's data/<outlet>.json files.

This replaces the GitHub Actions JSON-API path for Resy because Resy's
survey API stopped returning numeric ratings around 2026-04-17 (every
recommend/food/service/atmos/sentiment field came back null after that
date). The CSV export from the OS portal still includes everything.

Run via the launchd job (com.methodco.resy-sync.plist) or manually:
    python3 tools/resy_csv_sync.py
    python3 tools/resy_csv_sync.py --outlet lsbr   # one venue
    python3 tools/resy_csv_sync.py --skip-export   # re-ingest existing CSVs

Configuration (env vars or default paths):
    RESY_OS_STORAGE_STATE_PATH  default: ~/.config/method-dashboards/resy-storage-state.json
    RESY_OS_VENUES              "lsbr=det/le-supreme;lowland=chs/lowland;..."
                                 default: read from ~/.config/method-dashboards/resy-venues.txt
    RESY_CSV_OUT_DIR            default: ~/Documents/Method/resy-csvs
    --data-dir                  default: <repo root>/data

Failure modes:
    1. Storage state expired → script exits 2; reseed via
       tools/refresh_resy_storage.py (~21 day cadence).
    2. Export button selector drift → script logs which selectors were
       tried per venue; update SELECTORS list.
    3. Resy CSV column drift → ingest preserves raw CSVs in
       RESY_CSV_OUT_DIR/<date>/; column-rename is a one-line patch.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write(
        "missing dependency: pip3 install playwright && python3 -m playwright install chromium\n"
    )
    sys.exit(2)


# ---------- config ----------

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 30_000
DEFAULT_STATE_PATH = Path.home() / ".config" / "method-dashboards" / "resy-storage-state.json"
DEFAULT_VENUES_PATH = Path.home() / ".config" / "method-dashboards" / "resy-venues.txt"
DEFAULT_CSV_OUT = Path.home() / "Documents" / "Method" / "resy-csvs"

EXPORT_SELECTORS = [
    'button:has-text("Export")',
    'button:has-text("Download")',
    'a:has-text("Export")',
    '[aria-label*="export" i]',
    '[data-testid*="export" i]',
]


def parse_venues(raw: str) -> dict[str, str]:
    """outlet_id=city/slug;outlet_id=city/slug → {oid: slug}."""
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


def load_venues() -> dict[str, str]:
    raw = os.environ.get("RESY_OS_VENUES", "").strip()
    if raw:
        return parse_venues(raw)
    if DEFAULT_VENUES_PATH.exists():
        return parse_venues(DEFAULT_VENUES_PATH.read_text(encoding="utf-8"))
    sys.stderr.write(
        f"venue mapping missing — set RESY_OS_VENUES or create {DEFAULT_VENUES_PATH}\n"
    )
    sys.exit(2)


def load_storage_state(custom: Path | None) -> dict:
    p = custom or Path(os.environ.get("RESY_OS_STORAGE_STATE_PATH", "")) or DEFAULT_STATE_PATH
    if not p or not p.exists():
        sys.stderr.write(
            f"storage state missing at {p} — run tools/refresh_resy_storage.py\n"
        )
        sys.exit(2)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"storage state at {p} is not valid JSON: {e}\n")
        sys.exit(2)


# ---------- export step ----------

def export_venue_csv(page, slug: str, out_path: Path) -> bool:
    """Navigate to <slug>/analytics/Surveys, click Export, save CSV.
    Returns True on success."""
    url = f"https://os.resy.com/portal/{slug}/analytics/Surveys"
    try:
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception as e:
        sys.stderr.write(f"  [{slug}] navigate failed: {e}\n")
        return False
    # Let the SPA render (the Export button appears after first XHR settles).
    try:
        page.wait_for_timeout(6000)
    except Exception:
        pass
    for sel in EXPORT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0 or not btn.is_visible(timeout=1000):
                continue
        except Exception:
            continue
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                btn.click(timeout=5_000)
            dl = dl_info.value
            out_path.parent.mkdir(parents=True, exist_ok=True)
            dl.save_as(str(out_path))
            return True
        except PWTimeout:
            sys.stderr.write(
                f"  [{slug}] export click fired no download with selector {sel}\n"
            )
            continue
        except Exception as e:
            sys.stderr.write(f"  [{slug}] export click failed ({sel}): {e}\n")
            continue
    sys.stderr.write(f"  [{slug}] no working Export selector among {EXPORT_SELECTORS}\n")
    return False


def run_exports(
    storage_state: dict, venues: dict[str, str], out_root: Path,
    only: str | None,
) -> dict[str, Path]:
    """Return {outlet_id: csv_path} for venues that exported successfully."""
    out_root.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    today_dir = out_root / today
    saved: dict[str, Path] = {}
    targets = {oid: slug for oid, slug in venues.items() if not only or oid == only}
    if not targets:
        sys.stderr.write(f"no matching venues (only={only!r})\n")
        return saved
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        for oid, slug in targets.items():
            print(f"[{oid}] slug={slug}")
            csv_path = today_dir / f"{oid}.csv"
            ok = export_venue_csv(page, slug, csv_path)
            if ok:
                size_kb = csv_path.stat().st_size / 1024
                print(f"  ✓ saved {csv_path.relative_to(out_root)} ({size_kb:.1f} KB)")
                saved[oid] = csv_path
        browser.close()
    return saved


# ---------- ingest step ----------

# Resy's CSV column names vary; map liberally on substring.
def col_match(headers: list[str], *needles: str) -> str | None:
    """Find first header whose lowercase form contains any needle."""
    for h in headers:
        lh = h.lower().strip()
        if any(n in lh for n in needles):
            return h
    return None


def parse_resy_csv(path: Path) -> list[dict]:
    """Parse one Resy CSV export → list of survey rows in our schema.

    Mapping is substring-tolerant since Resy varies header names by
    venue config. Columns we extract:
      date           ← date submitted / completion / visit
      overall        ← overall score (0-100)
      recommend      ← recommendation 0-10 / NPS
      food / service / atmos / sentiment   ← category scores
      server, covers, dow, hour
      text[]         ← any free-text comment columns, kept as [{q, a}]
    """
    out: list[dict] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    headers = reader.fieldnames or []
    if not headers:
        return out

    col_date = col_match(headers, "submitted", "completed", "date completed", "date_submitted")
    if not col_date:
        col_date = col_match(headers, "visit date", "reservation date", "date")
    col_overall = col_match(headers, "overall score", "overall_score", "overall")
    col_recommend = col_match(headers, "recommend", "nps", "likely")
    col_food = col_match(headers, "food", "menu")
    col_service = col_match(headers, "service", "staff")
    col_atmos = col_match(headers, "atmosphere", "ambien", "vibe", "decor")
    col_sentiment = col_match(headers, "sentiment", "experience")
    col_server = col_match(headers, "server")
    col_covers = col_match(headers, "party size", "covers", "guests")
    col_seated = col_match(headers, "date seated", "seated", "date arrived", "reservation time")

    # Free-text columns: any header containing "comment", "feedback",
    # "share", "improve", "anything", "note", "tell us"
    text_cols = [h for h in headers if any(t in h.lower() for t in
        ("comment", "feedback", "share", "improve", "anything", "tell us", "note"))]

    def parse_num(v: str) -> float | None:
        if v is None: return None
        s = str(v).strip()
        if not s: return None
        try:
            return float(s)
        except ValueError:
            return None

    def parse_date(v: str) -> str | None:
        if not v: return None
        s = str(v).strip()
        # Accept ISO, YYYY-MM-DD HH:MM, or M/D/YYYY
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{yy:04d}-{mm:02d}-{dd:02d}"
        return None

    def parse_hour(v: str) -> int | None:
        if not v: return None
        s = str(v).strip()
        # Match HH:MM somewhere in the string
        m = re.search(r"(\d{1,2}):(\d{2})", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None

    for row in reader:
        d = parse_date(row.get(col_date) if col_date else None)
        if not d:
            continue
        seated = row.get(col_seated, "") if col_seated else ""
        hour = parse_hour(seated)
        try:
            from datetime import date as _d
            y, mo, dy = (int(x) for x in d.split("-"))
            dow = _d(y, mo, dy).weekday()
        except Exception:
            dow = None

        text_responses: list[dict] = []
        for tc in text_cols:
            v = (row.get(tc) or "").strip()
            if v and len(v) >= 4 and v.lower() not in {"yes","no","n/a","na","none","nothing","-"}:
                text_responses.append({"q": tc[:120], "a": v[:600]})

        out.append({
            "date":      d,
            "overall":   parse_num(row.get(col_overall)) if col_overall else None,
            "recommend": parse_num(row.get(col_recommend)) if col_recommend else None,
            "food":      parse_num(row.get(col_food)) if col_food else None,
            "service":   parse_num(row.get(col_service)) if col_service else None,
            "atmos":     parse_num(row.get(col_atmos)) if col_atmos else None,
            "sentiment": parse_num(row.get(col_sentiment)) if col_sentiment else None,
            "server":    (row.get(col_server) or "").strip() or None if col_server else None,
            "covers":    int(parse_num(row.get(col_covers)) or 0) or None if col_covers else None,
            "dow":       dow,
            "hour":      hour,
            "text":      text_responses or None,
            "_csv_src":  path.name,  # provenance — strip before write if too noisy
        })
    return out


def merge_into_outlet(data_dir: Path, outlet_id: str, csv_rows: list[dict]) -> dict:
    """Append-merge CSV rows into data/<outlet>.json. Returns stats."""
    p = data_dir / f"{outlet_id}.json"
    if p.exists():
        payload = json.loads(p.read_text(encoding="utf-8"))
    else:
        payload = {"outlet_id": outlet_id}
    guest = payload.get("guest") or {}
    surveys = list(guest.get("surveys") or [])

    # Natural-key dedup: (date, server, overall, covers). Same as the
    # existing API path so historical seed rows stay deduped.
    def key(s: dict) -> tuple:
        return (s.get("date"), s.get("server"), s.get("overall"), s.get("covers"))

    idx = {key(s): i for i, s in enumerate(surveys)}
    added = 0
    upgraded = 0
    for r in csv_rows:
        # Strip provenance before storing (or keep in a separate field)
        r = {k: v for k, v in r.items() if k != "_csv_src"}
        k = key(r)
        if k in idx:
            old = surveys[idx[k]]
            old_text = bool(old.get("text"))
            new_text = bool(r.get("text"))
            old_rec = old.get("recommend") is not None
            new_rec = r.get("recommend") is not None
            # Replace if the new row carries data the old one was
            # missing (text comments OR rating scores). CSV is the
            # authoritative source going forward, so winning the diff
            # is fine.
            if (new_text and not old_text) or (new_rec and not old_rec):
                surveys[idx[k]] = r
                upgraded += 1
        else:
            surveys.append(r)
            idx[k] = len(surveys) - 1
            added += 1

    guest["surveys"] = surveys
    guest["as_of"] = date.today().isoformat()
    guest["source"] = "resy_csv_sync"
    payload["guest"] = guest
    payload["generated_at_resy"] = datetime.now(timezone.utc).isoformat()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return {"added": added, "upgraded": upgraded, "total": len(surveys)}


# ---------- entry ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--outlet", help="single outlet id (default: all)")
    ap.add_argument("--data-dir", help="path to dashboard data/ dir",
                    default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--csv-dir", help="root for date-stamped CSVs",
                    default=str(Path(os.environ.get("RESY_CSV_OUT_DIR") or DEFAULT_CSV_OUT)))
    ap.add_argument("--storage-state", help="path to playwright storage state JSON",
                    default=None)
    ap.add_argument("--skip-export", action="store_true",
                    help="re-ingest today's CSVs without re-running browser")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    csv_dir = Path(args.csv_dir)
    venues = load_venues()

    if args.skip_export:
        # Pick up today's existing CSVs
        today_dir = csv_dir / date.today().isoformat()
        if not today_dir.exists():
            sys.stderr.write(f"no CSVs found for today at {today_dir}\n")
            return 1
        saved = {oid: today_dir / f"{oid}.csv" for oid in venues
                 if (today_dir / f"{oid}.csv").exists()}
        if not saved:
            sys.stderr.write(f"no per-outlet CSVs in {today_dir}\n")
            return 1
        if args.outlet:
            saved = {k: v for k, v in saved.items() if k == args.outlet}
    else:
        storage_state = load_storage_state(
            Path(args.storage_state) if args.storage_state else None
        )
        saved = run_exports(storage_state, venues, csv_dir, args.outlet)
        if not saved:
            sys.stderr.write("no exports succeeded — aborting ingest\n")
            return 1

    # Ingest
    print()
    print("Ingest:")
    total_added = total_upgraded = 0
    for oid, path in saved.items():
        try:
            rows = parse_resy_csv(path)
        except Exception as e:
            sys.stderr.write(f"  [{oid}] CSV parse failed: {e}\n")
            continue
        if not rows:
            print(f"  [{oid}] {path.name} → 0 parseable rows (header mismatch?)")
            continue
        stats = merge_into_outlet(data_dir, oid, rows)
        total_added += stats["added"]
        total_upgraded += stats["upgraded"]
        print(f"  [{oid}] {path.name} → +{stats['added']} new · upgraded {stats['upgraded']} · total {stats['total']}")
    print()
    print(f"Done. {total_added} new + {total_upgraded} upgraded across {len(saved)} venue(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
