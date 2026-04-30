#!/usr/bin/env python3
"""
Method Co — Budget sync (helixo-2 daily_budget → dashboard outlets).

Pulls the helixo-2 Supabase `daily_budget` table and writes a
`budget.daily` block into each data/<outlet>.json. The dashboard's
tri-comparison KPI cards (vs Forecast / vs STLY / vs Budget) read
this block — when missing, the cards render "Budget — not wired".

Source schema (helixo-2 `daily_budget`):
  location_id        UUID
  business_date      DATE
  budget_revenue     NUMERIC      ← this is what we write
  server_budget      NUMERIC
  bartender_budget   NUMERIC
  ... 9 more position-specific labor budgets ...

The dashboard cards key off `net_sales` for the Net Sales card and
just need a per-day revenue number. We don't surface position-level
labor budgets here — that's helixo-2's domain (the Lead Sheet KPI
report Ross loads via scripts/load_budget.cjs lives over there).

Setup:
  GitHub Secrets (already wired for tripleseat_sync):
    SUPABASE_URL              https://mmwislzsgnjxjxssynwm.supabase.co
    SUPABASE_SERVICE_ROLE_KEY (server key — bypasses RLS)

Usage:
  python3 budget_sync.py                # all outlets
  python3 budget_sync.py --outlet lsbr  # single outlet
  python3 budget_sync.py --probe        # auth-only health check
  python3 budget_sync.py --print-config # show resolved location → outlet map
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


# ---------- helixo-2 Supabase location UUID → Method outlet slug ----------

# Same UUIDs the Tripleseat sync uses (helixo-2 keeps a single
# `locations` table, both engines key off it). Keep this in sync with
# tripleseat_sync.py::SUPA_UUID_TO_OUTLET.
SUPA_UUID_TO_OUTLET: dict[str, str] = {
    "84f4ea7f-722d-4296-894b-6ecfe389b2d5": "anthology",
    "b7d3e1a4-5f2c-4a8b-9e6d-1c3f5a7b9d2e": "kampers",
    "ae99ee33-1b8e-4c8f-8451-e9f3d0fa28ce": "lsbr",
    "b4035001-0928-4ada-a0f0-f2a272393147": "hiroki_det",
    "580ae0a6-34b8-402e-a8a6-2e55310207e4": "rosemary_rose",
    # The Lowland load_budget script targeted this UUID — known mapping:
    "f36fdb18-a97b-48af-8456-7374dea4b0f9": "lowland",
}

# Method outlet slugs known to this repo — used by name_to_outlet for
# fuzzy matching when a location_id isn't in the hardcoded UUID map.
KNOWN_OUTLETS = (
    "anthology", "hiroki_det", "hiroki_phl", "kampers", "little_wing",
    "lowland", "lsbr", "mulherins", "quoin", "rosemary_rose", "vessel",
)

# Manual normalized-name → outlet overrides (mirror tripleseat_sync.py).
NAME_TO_OUTLET_OVERRIDES: dict[str, str] = {
    "wmmulherinssons": "mulherins",
    "wmmulherinssonshotel": "mulherins",
    "lowlandcharleston": "lowland",
    "lowland": "lowland",
    "thequoin": "quoin",
    "quoinrooftop": "quoin",
    "lesupreme": "lsbr",
    "lesuprême": "lsbr",
    "barrotunda": "lsbr",
    "lesupremebarrotunda": "lsbr",
    "kampersbar": "kampers",
    "kampersrooftopbar": "kampers",
    "hirokisandetroit": "hiroki_det",
    "hirokisanphiladelphia": "hiroki_phl",
    "hirokiphiladelphia": "hiroki_phl",
    "hirokisan": "hiroki_phl",
    "anthology": "anthology",
    "anthologyevents": "anthology",
    "anthologyeventsatbooktower": "anthology",
    "rosemaryrose": "rosemary_rose",
    "rosemaryandrose": "rosemary_rose",
    "littlewing": "little_wing",
    "littlewingcoffeeandgoods": "little_wing",
    "vessel": "vessel",
    "vesselroostbaltimore": "vessel",
    "thenickelhotel": "rosemary_rose",
}


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def name_to_outlet(name: str | None) -> str | None:
    if not name:
        return None
    norm = _normalize_name(name)
    if norm in NAME_TO_OUTLET_OVERRIDES:
        return NAME_TO_OUTLET_OVERRIDES[norm]
    for outlet in KNOWN_OUTLETS:
        if _normalize_name(outlet) == norm:
            return outlet
    for outlet in KNOWN_OUTLETS:
        if _normalize_name(outlet) in norm or norm in _normalize_name(outlet):
            return outlet
    return None


# ---------- Supabase REST client ----------

REQUEST_TIMEOUT = 60


class Supabase:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

    def select(self, table: str, columns: str = "*",
               filters: dict | None = None,
               range_header: str | None = None) -> list[dict]:
        params = {"select": columns}
        if filters:
            params.update(filters)
        url = f"{self.base}/{table}?{urlencode(params)}"
        headers = dict(self.headers)
        if range_header:
            headers["Range"] = range_header
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"Supabase {r.status_code} on {table}: {r.text[:300]}")
        return r.json()


# ---------- budget pull + outlet routing ----------

def fetch_all_budgets(sb: Supabase) -> list[dict]:
    """Fetch every row from helixo-2's daily_budget. PostgREST defaults
    cap at 1000 rows per response; loop with Range headers until we run
    out so we don't silently truncate.

    Returns rows in insertion order — caller groups by location_id.
    """
    out: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        rng = f"{offset}-{offset + page_size - 1}"
        rows = sb.select(
            "daily_budget",
            columns="location_id,business_date,budget_revenue",
            range_header=rng,
        )
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return out


def fetch_locations_by_id(sb: Supabase, uuids: list[str]) -> dict[str, str]:
    """Return {uuid: name} for the given UUIDs (used for name fallback
    when SUPA_UUID_TO_OUTLET doesn't map a location)."""
    if not uuids:
        return {}
    in_clause = "in.(" + ",".join(uuids) + ")"
    rows = sb.select("locations", columns="id,name", filters={"id": in_clause})
    return {r["id"]: r.get("name") for r in rows}


def resolve_uuid_to_outlet(uuid: str, name_lookup: dict[str, str]) -> str | None:
    """Hardcoded UUID map wins; fallback to fuzzy name match."""
    if uuid in SUPA_UUID_TO_OUTLET:
        return SUPA_UUID_TO_OUTLET[uuid]
    return name_to_outlet(name_lookup.get(uuid))


# ---------- I/O ----------

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


def discover_outlets(data_dir: Path) -> list[str]:
    return sorted(
        p.stem for p in data_dir.glob("*.json")
        if not p.stem.startswith("_")
    )


# ---------- driver ----------

def cmd_probe(sb: Supabase) -> int:
    """Auth + table-presence sanity check. Pulls 1 row from daily_budget."""
    print(f"Probing {sb.base}/daily_budget?limit=1 ...\n")
    try:
        rows = sb.select("daily_budget", columns="location_id,business_date,budget_revenue",
                         filters={"limit": "1"})
        print(f"  ✓ daily_budget reachable ({len(rows)} sample row pulled)")
        if rows:
            print(f"    sample: {json.dumps(rows[0])[:200]}")
        return 0
    except Exception as e:
        sys.stderr.write(f"  ✗ {e}\n")
        return 1


def cmd_print_config(sb: Supabase) -> int:
    rows = fetch_all_budgets(sb)
    by_loc: dict[str, int] = defaultdict(int)
    for r in rows:
        by_loc[r["location_id"]] += 1
    if not by_loc:
        print("(no daily_budget rows in helixo-2)")
        return 0
    name_lookup = fetch_locations_by_id(sb, sorted(by_loc.keys()))
    print(f"{'location_id (UUID)':<40}  {'name':<32}  {'outlet':<14}  rows")
    print("-" * 100)
    for uuid, n in sorted(by_loc.items(), key=lambda kv: -kv[1]):
        name = name_lookup.get(uuid, "(unknown)")
        outlet = resolve_uuid_to_outlet(uuid, name_lookup) or "—"
        print(f"{uuid:<40}  {(name or ''):<32}  {outlet:<14}  {n}")
    return 0


def cmd_sync(sb: Supabase, data_dir: Path, only_outlet: str = "") -> int:
    rows = fetch_all_budgets(sb)
    if not rows:
        sys.stderr.write("no daily_budget rows in helixo-2 — nothing to sync\n")
        return 0
    name_lookup = fetch_locations_by_id(sb, sorted({r["location_id"] for r in rows}))

    # Group rows by Method outlet slug.
    by_outlet: dict[str, list[dict]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    for r in rows:
        outlet = resolve_uuid_to_outlet(r["location_id"], name_lookup)
        if not outlet:
            skipped[r["location_id"]] += 1
            continue
        by_outlet[outlet].append({
            "date": r["business_date"],
            "net_sales": float(r.get("budget_revenue") or 0),
            # The dashboard's tri-comparison helper also looks up
            # `guests` and `orders` for those KPI cards. The helixo-2
            # daily_budget table doesn't track per-day cover/order
            # budgets, so leave them null — the KPI card will fall
            # through to "—" for those fields, which is the right
            # behavior (no data > misleading zero).
            "guests": None,
            "orders": None,
        })

    if skipped:
        sys.stderr.write("  ! skipped budget rows from unmapped locations:\n")
        for uuid, count in sorted(skipped.items(), key=lambda x: -x[1]):
            nm = name_lookup.get(uuid, "(unknown)")
            sys.stderr.write(f"      uuid={uuid} name={nm!r}  rows={count}\n")

    today = date.today().isoformat()
    targets = [only_outlet] if only_outlet else sorted(by_outlet.keys())
    print(f"Writing budget.daily to {len(targets)} outlet(s):")
    for outlet in targets:
        daily = sorted(by_outlet.get(outlet, []), key=lambda r: r["date"])
        if not daily:
            sys.stderr.write(f"  ! {outlet}: no budget rows in helixo-2 daily_budget — skipping\n")
            continue
        payload = load_outlet(data_dir, outlet)
        payload["budget"] = {
            "as_of":  today,
            "source": "helixo2_daily_budget",
            "daily":  daily,
            "_note":  f"range {daily[0]['date']} → {daily[-1]['date']} · {len(daily)} days",
        }
        write_outlet(data_dir, outlet, payload)
        rev_total = sum(r["net_sales"] for r in daily)
        print(f"  ✓ {outlet:<14} {len(daily):>4} days, ${rev_total:,.0f} total budget revenue")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--data-dir",     default="../data", help="dir of <outlet>.json")
    ap.add_argument("--outlet",       default="",       help="single outlet id (default: all)")
    ap.add_argument("--probe",        action="store_true", help="auth-only probe")
    ap.add_argument("--print-config", action="store_true", help="show daily_budget→outlet routing")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (sb_url and sb_key):
        sys.stderr.write(
            "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required — "
            "exiting cleanly (no-op).\n"
        )
        return 0
    sb = Supabase(sb_url, sb_key)

    if args.probe:
        return cmd_probe(sb)
    if args.print_config:
        return cmd_print_config(sb)
    return cmd_sync(sb, data_dir, only_outlet=args.outlet)


if __name__ == "__main__":
    raise SystemExit(main())
