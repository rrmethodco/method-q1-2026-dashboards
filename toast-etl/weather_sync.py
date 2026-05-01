#!/usr/bin/env python3
"""
Method Co — Weather sync (Helixo daily_weather → dashboard outlets).

Pulls the Helixo Supabase `daily_weather` table and writes a
`weather.daily` block into each data/<outlet>.json. The dashboard's
Forward Window forecast table (and any future weather-aware view)
reads this block.

Source schema (Helixo `daily_weather`):
  location_id        UUID
  business_date      DATE
  temp_high          NUMERIC
  temp_low           NUMERIC
  condition          TEXT     (Clear / Clouds / Rain / Snow / Mist / etc.)
  icon               TEXT     (OpenWeather code: 01d, 02d, 10d, 11d, 13d, 50d, ...)
  description        TEXT
  precipitation_pct  NUMERIC

Output written under outlet.weather.daily, schema:
  [{ date, temp_high, temp_low, condition, icon, description, precipitation_pct }]

Coverage at the time of writing: ~500 days per outlet (Jan 2025 → 15
days forward), refreshed nightly by Helixo's weather cron.

Setup (already wired for tripleseat / forecast / budget):
  GitHub Secrets:
    SUPABASE_URL              https://mmwislzsgnjxjxssynwm.supabase.co
    SUPABASE_SERVICE_ROLE_KEY (server key — bypasses RLS)

Usage:
  python3 weather_sync.py                # all outlets
  python3 weather_sync.py --outlet lsbr  # single outlet
  python3 weather_sync.py --probe        # auth-only probe
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


# ---------- helixo-2 location UUID → Method outlet (mirror forecast_engine.py) ----------

SUPA_UUID_TO_OUTLET: dict[str, str] = {
    "84f4ea7f-722d-4296-894b-6ecfe389b2d5": "anthology",
    "b7d3e1a4-5f2c-4a8b-9e6d-1c3f5a7b9d2e": "kampers",
    "ae99ee33-1b8e-4c8f-8451-e9f3d0fa28ce": "lsbr",
    "b4035001-0928-4ada-a0f0-f2a272393147": "hiroki_det",
    "580ae0a6-34b8-402e-a8a6-2e55310207e4": "rosemary_rose",
    "f36fdb18-a97b-48af-8456-7374dea4b0f9": "lowland",
}

KNOWN_OUTLETS = (
    "anthology", "hiroki_det", "hiroki_phl", "kampers", "little_wing",
    "lowland", "lsbr", "mulherins", "quoin", "rosemary_rose", "vessel",
)

NAME_TO_OUTLET_OVERRIDES: dict[str, str] = {
    "wmmulherinssons": "mulherins", "wmmulherinssonshotel": "mulherins",
    "lowlandcharleston": "lowland", "lowland": "lowland",
    "thequoin": "quoin", "quoinrooftop": "quoin",
    "lesupreme": "lsbr", "lesuprême": "lsbr", "barrotunda": "lsbr",
    "lesupremebarrotunda": "lsbr",
    "kampersbar": "kampers", "kampersrooftopbar": "kampers",
    "hirokisandetroit": "hiroki_det", "hirokisanphiladelphia": "hiroki_phl",
    "hirokiphiladelphia": "hiroki_phl", "hirokisan": "hiroki_phl",
    "anthology": "anthology", "anthologyevents": "anthology",
    "anthologyeventsatbooktower": "anthology",
    "rosemaryrose": "rosemary_rose", "rosemaryandrose": "rosemary_rose",
    "littlewing": "little_wing", "littlewingcoffeeandgoods": "little_wing",
    "vessel": "vessel", "vesselroostbaltimore": "vessel",
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


def resolve_uuid_to_outlet(uuid: str, name_lookup: dict[str, str]) -> str | None:
    if uuid in SUPA_UUID_TO_OUTLET:
        return SUPA_UUID_TO_OUTLET[uuid]
    return name_to_outlet(name_lookup.get(uuid))


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


def fetch_all_weather(sb: Supabase) -> list[dict]:
    """Pull every row from Helixo daily_weather. PostgREST 1000-row
    page cap; loop with Range until exhausted. ~5500 rows total
    (500 days × 11 outlets) — 6 page iterations."""
    out: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        rng = f"{offset}-{offset + page_size - 1}"
        rows = sb.select(
            "daily_weather",
            columns="location_id,business_date,temp_high,temp_low,condition,icon,description,precipitation_pct",
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
    if not uuids:
        return {}
    in_clause = "in.(" + ",".join(uuids) + ")"
    rows = sb.select("locations", columns="id,name", filters={"id": in_clause})
    return {r["id"]: r.get("name") for r in rows}


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


# ---------- driver ----------

def cmd_probe(sb: Supabase) -> int:
    print(f"Probing {sb.base}/daily_weather ...\n")
    try:
        rows = sb.select("daily_weather",
                         columns="location_id,business_date,temp_high,condition,icon",
                         filters={"limit": "5"})
        print(f"  ✓ daily_weather reachable ({len(rows)} sample row(s))")
        for r in rows:
            print(f"    {r.get('business_date')}  loc={r.get('location_id', '')[:8]}…  "
                  f"high={r.get('temp_high')}  cond={r.get('condition')}  icon={r.get('icon')}")
        return 0
    except Exception as e:
        sys.stderr.write(f"  ✗ {e}\n")
        return 1


def cmd_sync(sb: Supabase, data_dir: Path, only_outlet: str = "") -> int:
    rows = fetch_all_weather(sb)
    if not rows:
        sys.stderr.write("no daily_weather rows in Helixo — nothing to sync\n")
        return 0
    name_lookup = fetch_locations_by_id(sb, sorted({r["location_id"] for r in rows}))

    by_outlet: dict[str, list[dict]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    for r in rows:
        outlet = resolve_uuid_to_outlet(r["location_id"], name_lookup)
        if not outlet:
            skipped[r["location_id"]] += 1
            continue
        by_outlet[outlet].append({
            "date":              (r.get("business_date") or "")[:10],
            "temp_high":         _to_float(r.get("temp_high")),
            "temp_low":          _to_float(r.get("temp_low")),
            "condition":         r.get("condition"),
            "icon":              r.get("icon"),
            "description":       r.get("description"),
            "precipitation_pct": _to_float(r.get("precipitation_pct")),
        })

    if skipped:
        sys.stderr.write("  ! skipped weather rows from unmapped locations:\n")
        for uuid, count in sorted(skipped.items(), key=lambda x: -x[1]):
            nm = name_lookup.get(uuid, "(unknown)")
            sys.stderr.write(f"      uuid={uuid} name={nm!r}  rows={count}\n")

    today = date.today().isoformat()
    targets = [only_outlet] if only_outlet else sorted(by_outlet.keys())
    print(f"Writing weather.daily to {len(targets)} outlet(s) — source: Helixo daily_weather")
    for outlet in targets:
        daily = sorted(by_outlet.get(outlet, []), key=lambda r: r["date"])
        if not daily:
            sys.stderr.write(f"  ! {outlet}: no weather rows in Helixo daily_weather — skipping\n")
            continue
        future = [r for r in daily if r["date"] >= today]
        payload = load_outlet(data_dir, outlet)
        payload["weather"] = {
            "as_of":  today,
            "source": "helixo_daily_weather",
            "daily":  daily,
            "_note":  f"range {daily[0]['date']} → {daily[-1]['date']} · "
                      f"{len(daily)} days ({len(future)} forward)",
        }
        write_outlet(data_dir, outlet, payload)
        print(f"  ✓ {outlet:<14} {len(daily):>4} days ({len(future)} forward)")

    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--data-dir", default="../data", help="dir of <outlet>.json")
    ap.add_argument("--outlet",   default="",       help="single outlet id (default: all)")
    ap.add_argument("--probe",    action="store_true", help="auth-only probe")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (sb_url and sb_key):
        sys.stderr.write(
            "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required — exiting cleanly (no-op).\n"
        )
        return 0
    sb = Supabase(sb_url, sb_key)

    if args.probe:
        return cmd_probe(sb)
    return cmd_sync(sb, data_dir, only_outlet=args.outlet)


if __name__ == "__main__":
    raise SystemExit(main())
