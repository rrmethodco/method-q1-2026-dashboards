#!/usr/bin/env python3
"""
Method Co — Tripleseat Private Events sync.

Pulls events (with financials), classifies them by Method Co's brand
revenue-credit rules (mirroring helixo-2's tripleseat-sync.ts), and
writes per-outlet `events` blocks into data/<outlet>.json so the
dashboard's Private Events tab can render booked revenue, event count,
F&B mix, lead time, and segment (Wedding/Corporate/Social/Other).

============================================================================
What we use
============================================================================
  Auth:     OAuth 1.0a HMAC-SHA1 (two-legged, consumer-only).
            Per the support@tripleseat.com integration email, Method
            uses one consumer_key + consumer_secret pair across all
            properties (Tripleseat shares one account-level credential
            for the org, with location_id as the per-property dimension).

            We pull this credential pair from the helixo-2 Supabase
            project's `tripleseat_config` table — same source of truth
            as helixo-2's working tripleseat-sync.ts in production.
            That guarantees the dashboards repo stays in sync with
            whatever Tripleseat key Method's leadership rotates.

  Base URL: https://api.tripleseat.com/v1

  Endpoints used:
    GET /sites.json                       → list all sites + locations
    GET /events.json?show_financial=true  → all events with financials
                                            (Tripleseat ignores date filters
                                             — we paginate the whole
                                             account and partition
                                             client-side by location +
                                             event_date)

  Rate limit: 10 req/sec per Tripleseat docs. We sleep 0.11s between
  pages (well below the cap).

============================================================================
Setup
============================================================================
  GitHub Secrets:
    SUPABASE_URL              (e.g. https://mmwislzsgnjxjxssynwm.supabase.co)
    SUPABASE_SERVICE_ROLE_KEY (server key — bypasses RLS, needed to read
                                tripleseat_config rows)

  Optional (override Supabase lookup with hardcoded creds):
    TRIPLESEAT_CONSUMER_KEY
    TRIPLESEAT_CONSUMER_SECRET

  If neither path produces credentials, the sync exits cleanly with a
  warning so the workflow still runs end-to-end.

============================================================================
Usage
============================================================================
  python3 tripleseat_sync.py                # all outlets (Supabase-driven)
  python3 tripleseat_sync.py --probe        # auth-only health check
  python3 tripleseat_sync.py --discover     # list all Tripleseat sites
  python3 tripleseat_sync.py --dry-run      # write fixture, no network
  python3 tripleseat_sync.py --print-config # show resolved TS site → outlet map
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

try:
    import requests
except ImportError:
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)

from schemas.tripleseat_event import TripleseatEvent
from validation.runner import run_validation


# ---------- config ----------

BASE_URL = (os.environ.get("TRIPLESEAT_BASE_URL") or "https://api.tripleseat.com/v1").rstrip("/")
REQUEST_TIMEOUT = 45
USER_AGENT = "MethodCo-Dashboards/1.0 (tripleseat_sync.py; +https://github.com/rrmethodco)"
RATE_LIMIT_SLEEP = 0.11  # 9 req/sec, safely under the 10/sec cap


# ---------- OAuth 1.0a (HMAC-SHA1, two-legged) ----------

def _percent_encode(s: str) -> str:
    """RFC 5849-compliant percent encoding (uppercase hex)."""
    return quote(str(s), safe="-._~")


def _oauth1_header(consumer_key: str, consumer_secret: str,
                   method: str, url: str, params: dict) -> str:
    o = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    all_p = {**params, **o}
    ps = "&".join(f"{_percent_encode(k)}={_percent_encode(all_p[k])}"
                  for k in sorted(all_p))
    base = "&".join([method.upper(), _percent_encode(url), _percent_encode(ps)])
    sk = f"{_percent_encode(consumer_secret)}&"  # empty token secret (two-legged)
    sig = base64.b64encode(hmac.new(sk.encode(), base.encode(), hashlib.sha1).digest()).decode()
    o["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_percent_encode(k)}="{_percent_encode(v)}"'
                                for k, v in o.items())


# ---------- Supabase-driven config ----------

# Helixo-2 hardcoded these UUIDs in tripleseat-sync.ts. We mirror them so
# we can map the Supabase location.id (UUID) back to Method's outlet slug
# without needing additional metadata in the locations table. If a row in
# tripleseat_config references a UUID that's not in this dict, the script
# falls back to fuzzy-matching locations.name → outlet slug.
SUPA_UUID_TO_OUTLET: dict[str, str] = {
    "84f4ea7f-722d-4296-894b-6ecfe389b2d5": "anthology",
    "b7d3e1a4-5f2c-4a8b-9e6d-1c3f5a7b9d2e": "kampers",
    "ae99ee33-1b8e-4c8f-8451-e9f3d0fa28ce": "lsbr",
    "b4035001-0928-4ada-a0f0-f2a272393147": "hiroki_det",
    "580ae0a6-34b8-402e-a8a6-2e55310207e4": "rosemary_rose",
}

# Per helixo-2's tripleseat-sync.ts: hardcoded Tripleseat
# location_id → Method outlet overrides that take precedence over the
# Supabase config. Used for venues that Tripleseat models differently
# than Method's Supabase locations table (e.g. The Nickel Hotel hosts
# Rosemary Rose events under a separate location_id from the
# restaurant's Supabase row).
TS_LOC_HARDCODED_OUTLET: dict[int, str] = {
    31924: "rosemary_rose",  # The Nickel Hotel → Rosemary Rose (Charleston)
}

# Tripleseat location_id of "Anthology Events at Book Tower" — single
# Tripleseat venue covering 5 Method brands. Brand-split routing via
# room names (see classify_room below).
TS_LOC_BOOK_TOWER = 22266

# Outlet IDs that can receive routed Book Tower events (via brand
# allocation). Mirrors helixo-2's BRAND_TO_LOC keys.
BOOK_TOWER_BRANDS = ("anthology", "kampers", "lsbr", "hiroki_det", "rosemary_rose")

# Method outlet slugs that exist in this repo — used for fuzzy name matching
KNOWN_OUTLETS = (
    "anthology", "hiroki_det", "hiroki_phl", "kampers", "little_wing",
    "lowland", "lsbr", "mulherins", "quoin", "rosemary_rose", "vessel",
)


def _normalize_name(name: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy matching."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# Manual name → slug overrides for cases where fuzzy matching would
# otherwise fail or pick the wrong outlet. Operator updates this when
# Supabase locations get renamed.
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
    "hirokisan": "hiroki_phl",  # default Hiroki goes Philly unless explicitly Detroit
    "anthology": "anthology",
    "anthologyevents": "anthology",
    "anthologyeventsatbooktower": "anthology",
    "rosemaryrose": "rosemary_rose",
    "rosemaryandrose": "rosemary_rose",
    "littlewing": "little_wing",
    "littlewingcoffeeandgoods": "little_wing",
    "vessel": "vessel",
    "vesselroostbaltimore": "vessel",
    "thenickelhotel": "rosemary_rose",  # restaurant inside Nickel Hotel
}


def name_to_outlet(name: str | None) -> str | None:
    """Best-effort match of a Supabase locations.name → Method outlet slug."""
    if not name:
        return None
    norm = _normalize_name(name)
    if norm in NAME_TO_OUTLET_OVERRIDES:
        return NAME_TO_OUTLET_OVERRIDES[norm]
    # Direct slug-style match
    for outlet in KNOWN_OUTLETS:
        if _normalize_name(outlet) == norm:
            return outlet
    # Substring contains (last resort)
    for outlet in KNOWN_OUTLETS:
        if _normalize_name(outlet) in norm or norm in _normalize_name(outlet):
            return outlet
    return None


# ---------- Supabase REST client (PostgREST) ----------

class Supabase:
    """Minimal Supabase REST client for read-only config lookups."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

    def select(self, table: str, columns: str = "*",
               filters: dict | None = None) -> list[dict]:
        params = {"select": columns}
        if filters:
            params.update(filters)
        url = f"{self.base}/{table}?{urlencode(params)}"
        r = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Supabase {r.status_code} on {table}: {r.text[:300]}")
        return r.json()


def load_supabase_config(supabase: Supabase) -> tuple[str, str, dict[int, str]]:
    """Pull tripleseat_config + locations from Supabase and resolve the
    per-Tripleseat-location → Method outlet mapping.

    Returns (consumer_key, consumer_secret, ts_location_id → outlet_slug).

    The map covers every enabled config row (after deduping rows that
    share a tripleseat_site_id, like Book Tower's 3 disabled+1 enabled
    duplicates). The Book Tower entry itself maps to a sentinel
    "__BOOK_TOWER__" slug so the partitioner knows to apply brand-split
    rules instead of routing to a single outlet.
    """
    cfg_rows = supabase.select(
        "tripleseat_config",
        columns="location_id,tripleseat_site_id,consumer_key,consumer_secret,enabled",
        filters={"enabled": "eq.true"},
    )
    if not cfg_rows:
        raise RuntimeError("tripleseat_config returned 0 enabled rows")

    # Locations lookup (UUID → name) for fuzzy outlet matching when the
    # row's location_id isn't in SUPA_UUID_TO_OUTLET.
    loc_uuids = sorted({r["location_id"] for r in cfg_rows})
    in_clause = "in.(" + ",".join(loc_uuids) + ")"
    loc_rows = supabase.select(
        "locations",
        columns="id,name",
        filters={"id": in_clause},
    )
    name_by_uuid = {r["id"]: r.get("name") for r in loc_rows}

    # All rows share the same key/secret (per Method's account-level
    # Tripleseat creds). Pick the first non-empty pair.
    ck = cs = None
    for r in cfg_rows:
        if r.get("consumer_key") and r.get("consumer_secret"):
            ck, cs = r["consumer_key"], r["consumer_secret"]
            break
    if not (ck and cs):
        raise RuntimeError("no consumer_key/consumer_secret in tripleseat_config rows")

    # Build the TS location_id → outlet slug map. Dedupe by ts_site_id
    # (Book Tower has 3 disabled + 1 enabled, all sharing site=22266).
    seen_ts: set[int] = set()
    ts_to_outlet: dict[int, str] = {}
    for r in cfg_rows:
        try:
            ts_id = int(r["tripleseat_site_id"])
        except (TypeError, ValueError):
            continue
        if ts_id in seen_ts:
            continue
        seen_ts.add(ts_id)

        # Book Tower → sentinel
        if ts_id == TS_LOC_BOOK_TOWER:
            ts_to_outlet[ts_id] = "__BOOK_TOWER__"
            continue

        uuid = r["location_id"]
        outlet = SUPA_UUID_TO_OUTLET.get(uuid)
        if outlet is None:
            outlet = name_to_outlet(name_by_uuid.get(uuid))
        if outlet:
            ts_to_outlet[ts_id] = outlet
        else:
            sys.stderr.write(
                f"  ! tripleseat_site_id={ts_id} (loc {uuid} '{name_by_uuid.get(uuid)}') "
                "did not match any known outlet — events will be skipped\n"
            )

    # Apply hardcoded overrides (always win)
    for ts_id, slug in TS_LOC_HARDCODED_OUTLET.items():
        ts_to_outlet[ts_id] = slug

    return ck, cs, ts_to_outlet


# ---------- Tripleseat client ----------

class TripleseatClient:
    def __init__(self, consumer_key: str, consumer_secret: str):
        self.ck = consumer_key
        self.cs = consumer_secret
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        params = {k: str(v) for k, v in (params or {}).items()}
        url = f"{BASE_URL}{path}"
        auth_header = _oauth1_header(self.ck, self.cs, "GET", url, params)
        r = self.session.get(url, headers={"Authorization": auth_header},
                             params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            raise PermissionError(f"Tripleseat 401 at {path}: {r.text[:200]}")
        if r.status_code == 429:
            time.sleep(2.0)
            return self._get(path, params)
        r.raise_for_status()
        time.sleep(RATE_LIMIT_SLEEP)
        return r.json()

    def get_sites(self) -> list[dict]:
        body = self._get("/sites.json")
        return body if isinstance(body, list) else (body.get("results") or body.get("sites") or [])

    def get_all_events(self, show_financial: bool = True,
                       max_pages: int = 2000) -> list[dict]:
        """Paginate through every event in the account. Tripleseat's
        /events ignores date/location filters; partitioning happens
        client-side. ~19k events for Method per helixo-2's notes
        (5-30 min sync at 9 req/sec)."""
        out: list[dict] = []
        page = 1
        total_pages = None
        while page <= max_pages:
            params = {"page": page}
            if show_financial:
                params["show_financial"] = "true"
            body = self._get("/events.json", params)
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list):
                results = body if isinstance(body, list) else []
            if not results:
                break
            out.extend(results)
            if total_pages is None and isinstance(body, dict):
                total_pages = body.get("total_pages")
            if total_pages and page >= total_pages:
                break
            page += 1
            if page % 25 == 0:
                sys.stderr.write(f"    [tripleseat] paged {page} / {total_pages or '?'}\n")
        return out


# ---------- Method Co brand-split rules (mirrors helixo-2/room-rules.ts) ----------


def classify_room(room_name: str) -> str | None:
    """Map a Tripleseat room name → Method outlet_id (Book Tower only)."""
    if not room_name:
        return None
    lower = room_name.lower().strip()
    if any(s in lower for s in ("13th floor", "linden", "conservatory", "tastings",
                                  "photography", "terrace club", "the study",
                                  "business center", "green room", "graystone",
                                  "entertainment suite")):
        return "anthology"
    if any(s in lower for s in ("le supr", "le suprême", "le supreme", "rotunda")):
        return "lsbr"
    if "kamper" in lower:
        return "kampers"
    if any(s in lower for s in ("hiroki", "sakazuki", "aladdin sane")):
        return "hiroki_det"
    if "rosemary" in lower:
        return "rosemary_rose"
    return None


def allocate_brands(room_names: list[str]) -> list[tuple[str, float]]:
    """Return [(outlet_id, share), ...] summing to 1.0 for an event."""
    brands = set()
    for r in room_names or []:
        b = classify_room(r)
        if b:
            brands.add(b)
    if not brands:
        return [("anthology", 1.0)]
    if len(brands) == 1:
        return [(next(iter(brands)), 1.0)]
    if "anthology" in brands:
        others = sorted(brands - {"anthology"})
        share = 0.20 / len(others)
        return [("anthology", 0.80)] + [(b, share) for b in others]
    sorted_b = sorted(brands)
    return [(b, 1.0 / len(sorted_b)) for b in sorted_b]


# ---------- segment classification (matches helixo-2 conventions) ----------

SEGMENT_MAP = {
    "Wedding": "Wedding",
    "Wedding Reception": "Wedding",
    "Wedding Ceremony": "Wedding",
    "Corporate Events": "Corporate",
    "Corporate": "Corporate",
    "Meeting": "Corporate",
    "Social": "Social",
    "Shower": "Social",
    "Dinner": "Social",
    "Anniversary": "Social",
    "Lunch": "Social",
    "Cocktail Hour/Reception": "Social",
    "Photography Session": "Other",
    "Internal": "Other",
}


def classify_segment(raw: str | None) -> str:
    if not raw:
        return "Wedding"  # null → Wedding (per helixo-2 audit)
    return SEGMENT_MAP.get(raw, "Other")


# ---------- financial extraction (mirrors helixo-2's logic) ----------

def _num(v):
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def bucket_categories(cats: list | None) -> dict:
    """Bucket Tripleseat category_totals into food / beverage / rental / av / other."""
    out = {"food": 0.0, "beverage": 0.0, "rental": 0.0, "av": 0.0, "other": 0.0}
    if not cats:
        return out
    for c in cats:
        name = (c.get("name") or "").lower()
        amt = _num(c.get("total") or c.get("value"))
        if not amt:
            continue
        if name == "food":
            out["food"] += amt
        elif "beverage" in name:
            out["beverage"] += amt
        elif "room rental" in name or name == "rental":
            out["rental"] += amt
        elif "audio" in name or name == "av" or "a/v" in name:
            out["av"] += amt
        else:
            out["other"] += amt
    return out


def extract_billing(bills: list | None) -> dict:
    """Pull tax / gratuity / service charge from billing_totals."""
    out = {"tax": 0.0, "gratuity": 0.0, "service_charge": 0.0}
    if not bills:
        return out
    for b in bills:
        label = ((b.get("name") or b.get("description") or "")).lower()
        amt = _num(b.get("total") or b.get("total_price"))
        if not amt:
            continue
        if "tax" in label:
            out["tax"] += amt
        elif "gratuity" in label or "tip" in label:
            out["gratuity"] += amt
        elif "service" in label or "admin" in label:
            out["service_charge"] += amt
    return out


def compute_financials(evt: dict) -> dict:
    """Net revenue = grand_total − tax − gratuity (service charge stays in).
    Mirrors helixo-2's canonical reporting formula."""
    cats = bucket_categories(evt.get("category_totals"))
    bills = extract_billing(evt.get("billing_totals"))
    grand_total = _num(evt.get("grand_total"))
    total = max(0.0, grand_total - bills["tax"] - bills["gratuity"])
    return {
        "total": round(total, 2),
        "grand_total": round(grand_total, 2),
        "food": round(cats["food"], 2),
        "beverage": round(cats["beverage"], 2),
        "rental": round(cats["rental"], 2),
        "av": round(cats["av"], 2),
        "other": round(cats["other"], 2),
        "tax": round(bills["tax"], 2),
        "gratuity": round(bills["gratuity"], 2),
        "service_charge": round(bills["service_charge"], 2),
        "actual_amount": round(_num(evt.get("actual_amount")), 2),
        "fb_minimum": round(_num(evt.get("food_and_beverage_min")), 2),
    }


# ---------- transform + roll up per-outlet ----------

def transform_event(evt: dict) -> dict:
    """Project a Tripleseat event into our schema. Drops PII (account
    contact, customer email/phone) — keeps just operations metadata."""
    fin = compute_financials(evt)
    rooms = []
    for r in (evt.get("rooms") or []):
        if isinstance(r, dict) and r.get("name"):
            rooms.append(r["name"])
    if not rooms:
        rooms = list(evt.get("room_names") or [])
    booked_revenue = max(fin["actual_amount"], fin["fb_minimum"])
    if booked_revenue == 0:
        booked_revenue = fin["total"]
    return {
        "event_id": evt.get("id"),
        "name": (evt.get("name") or "").strip()[:120],
        "status": evt.get("status"),
        "event_start": evt.get("event_start"),
        "event_end": evt.get("event_end"),
        "guest_count": evt.get("guest_count"),
        "event_type": evt.get("event_type_name") or evt.get("event_type"),
        "segment": classify_segment(evt.get("event_type_name") or evt.get("event_type")),
        "location_id": evt.get("location_id"),
        "booking_id": evt.get("booking_id"),
        "rooms": rooms,
        "created_at": evt.get("created_at"),
        "lead_time_days": _lead_time_days(evt.get("created_at"), evt.get("event_start")),
        "financials": fin,
        "booked_revenue": round(booked_revenue, 2),
    }


def _lead_time_days(created_at: str | None, event_start: str | None) -> int | None:
    if not created_at or not event_start:
        return None
    try:
        c = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        e = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
        return max(0, (e.date() - c.date()).days)
    except (ValueError, TypeError):
        return None


def partition_to_outlets(events: list[dict],
                         ts_to_outlet: dict[int, str]) -> dict[str, list[dict]]:
    """Split Tripleseat events into per-outlet event lists.

    Routing rules (in priority order):
      1. TS_LOC_HARDCODED_OUTLET overrides (e.g. 31924 → rosemary_rose)
         — already merged into ts_to_outlet by load_supabase_config.
      2. Book Tower (TS location 22266 / sentinel "__BOOK_TOWER__"):
         brand-split via room names per Method Co policy.
      3. Direct map from ts_to_outlet (1:1 location → outlet).
      4. Unmapped Tripleseat locations: skipped with a stderr line.
    """
    by_outlet: dict[str, list[dict]] = defaultdict(list)
    skipped_ts: dict[int, int] = defaultdict(int)
    for evt in events:
        loc_id = evt.get("location_id")
        try:
            loc_id_int = int(loc_id) if loc_id is not None else None
        except (TypeError, ValueError):
            loc_id_int = None
        outlet = ts_to_outlet.get(loc_id_int) if loc_id_int is not None else None

        if outlet == "__BOOK_TOWER__":
            # Book Tower — brand-split via room names. Multi-brand events
            # produce multiple rows (revenue scaled by share).
            room_names: list[str] = []
            for r in (evt.get("rooms") or []):
                if isinstance(r, dict) and r.get("name"):
                    room_names.append(r["name"])
            if not room_names:
                room_names = list(evt.get("room_names") or [])
            # Some payloads send "Bar Rotunda, 13th Floor - Event Space" as a
            # single string — split on commas.
            expanded: list[str] = []
            for n in room_names:
                for piece in re.split(r",\s*", str(n)):
                    if piece.strip():
                        expanded.append(piece.strip())
            allocations = allocate_brands(expanded)
            for outlet_id, share in allocations:
                shared = dict(evt)
                shared["_revenue_share"] = share
                by_outlet[outlet_id].append(shared)
        elif outlet:
            shared = dict(evt)
            shared["_revenue_share"] = 1.0
            by_outlet[outlet].append(shared)
        else:
            if loc_id_int is not None:
                skipped_ts[loc_id_int] += 1

    if skipped_ts:
        sys.stderr.write("  ! skipped events from unmapped Tripleseat locations:\n")
        for ts_id, count in sorted(skipped_ts.items(), key=lambda x: -x[1]):
            sys.stderr.write(f"      ts_loc={ts_id}  events={count}\n")
    return by_outlet


def build_outlet_events_block(events: list[dict]) -> dict:
    """Produce the `events` block for a single outlet's data/<id>.json."""
    transformed = []
    for evt in events:
        t = transform_event(evt)
        t["revenue_share"] = evt.get("_revenue_share", 1.0)
        t["booked_revenue"] = round(t["booked_revenue"] * t["revenue_share"], 2)
        for k in ("total", "grand_total", "food", "beverage", "rental", "av", "other"):
            t["financials"][k] = round(t["financials"][k] * t["revenue_share"], 2)
        transformed.append(t)
    monthly = defaultdict(lambda: {"events": 0, "guests": 0, "booked_revenue": 0.0,
                                     "fb_revenue": 0.0, "by_segment": defaultdict(float)})
    by_segment_total = defaultdict(lambda: {"events": 0, "booked_revenue": 0.0})
    for e in transformed:
        m = (e.get("event_start") or "")[:7] or "Unknown"
        b = monthly[m]
        b["events"] += 1
        b["guests"] += e.get("guest_count") or 0
        b["booked_revenue"] += e.get("booked_revenue") or 0
        b["fb_revenue"] += (e["financials"].get("food", 0) + e["financials"].get("beverage", 0))
        b["by_segment"][e.get("segment")] += e.get("booked_revenue") or 0
        s = by_segment_total[e.get("segment")]
        s["events"] += 1
        s["booked_revenue"] += e.get("booked_revenue") or 0

    monthly_out = []
    for m in sorted(monthly):
        b = monthly[m]
        monthly_out.append({
            "month": m,
            "events": b["events"],
            "guests": b["guests"],
            "booked_revenue": round(b["booked_revenue"], 2),
            "fb_revenue": round(b["fb_revenue"], 2),
            "by_segment": {k: round(v, 2) for k, v in b["by_segment"].items()},
        })
    segment_out = [{"segment": k,
                    "events": v["events"],
                    "booked_revenue": round(v["booked_revenue"], 2)}
                   for k, v in sorted(by_segment_total.items())]
    return {
        "as_of": date.today().isoformat(),
        "source": "tripleseat_api",
        "events": transformed,
        "monthly_rollup": monthly_out,
        "by_segment": segment_out,
    }


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


# ---------- credential resolution ----------

def resolve_credentials() -> tuple[str | None, str | None, dict[int, str]]:
    """Resolve Tripleseat creds + ts_location → outlet map.

    Priority:
      1. Supabase tripleseat_config (canonical, mirrors helixo-2 prod)
      2. Hardcoded TRIPLESEAT_CONSUMER_KEY/SECRET env vars (fallback)
         — uses TS_LOC_HARDCODED_OUTLET only, plus Book Tower sentinel.
    """
    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if sb_url and sb_key:
        try:
            sb = Supabase(sb_url, sb_key)
            ck, cs, ts_to_outlet = load_supabase_config(sb)
            print(f"  ✓ loaded {len(ts_to_outlet)} Tripleseat→outlet mappings from Supabase")
            for ts_id, outlet in sorted(ts_to_outlet.items()):
                print(f"      ts_loc={ts_id:<6} → {outlet}")
            return ck, cs, ts_to_outlet
        except Exception as e:
            sys.stderr.write(f"  ! Supabase lookup failed: {e}\n")
    # Fallback to plain env vars (no per-location info beyond Book Tower
    # + hardcoded overrides).
    ck = os.environ.get("TRIPLESEAT_CONSUMER_KEY")
    cs = os.environ.get("TRIPLESEAT_CONSUMER_SECRET")
    if ck and cs:
        ts_to_outlet = {TS_LOC_BOOK_TOWER: "__BOOK_TOWER__"}
        ts_to_outlet.update(TS_LOC_HARDCODED_OUTLET)
        return ck, cs, ts_to_outlet
    return None, None, {}


# ---------- commands ----------

def cmd_probe(ck: str, cs: str) -> int:
    print(f"Probing {BASE_URL}/sites.json ...\n")
    client = TripleseatClient(ck, cs)
    try:
        sites = client.get_sites()
        print(f"  ✓ {len(sites)} site(s)")
        for s in sites[:5]:
            print(f"    {s.get('id')}  {s.get('name')}  ({len(s.get('locations') or [])} locations)")
        return 0
    except PermissionError as e:
        sys.stderr.write(f"  ✗ {e}\n")
        sys.stderr.write("  → Tripleseat API access not yet enabled on this account.\n"
                         "    Email support@tripleseat.com to activate.\n")
        return 1


def cmd_discover(ck: str, cs: str) -> int:
    """Print all sites + locations so the operator can populate
    LOCATION_TO_OUTLET / Supabase rows."""
    client = TripleseatClient(ck, cs)
    sites = client.get_sites()
    print(f"\n{'site_id':<10} {'site name':<35} {'location_id':<14} {'location name'}")
    print("-" * 100)
    for s in sites:
        for loc in (s.get("locations") or []):
            print(f"{s.get('id') or '-':<10} {(s.get('name') or '')[:33]:<35} "
                  f"{loc.get('id') or '-':<14} {loc.get('name')}")
    return 0


def cmd_sync(ck: str, cs: str, ts_to_outlet: dict[int, str],
             data_dir: Path, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] writing fixture")
        fixture = {"as_of": date.today().isoformat(), "source": "tripleseat_dry_run",
                   "events": [], "monthly_rollup": [], "by_segment": []}
        for oid in BOOK_TOWER_BRANDS:
            payload = load_outlet(data_dir, oid)
            payload["events"] = fixture
            write_outlet(data_dir, oid, payload)
        return 0

    if not ts_to_outlet:
        sys.stderr.write("no Tripleseat→outlet mapping resolved — exiting\n")
        return 1

    client = TripleseatClient(ck, cs)
    print("Pulling all events from Tripleseat — this can take 5-30 min for ~20k events...")
    try:
        events = client.get_all_events(show_financial=True)
    except PermissionError as e:
        sys.stderr.write(f"\n  ✗ {e}\n")
        sys.stderr.write("    Email support@tripleseat.com to activate API access.\n")
        return 1
    print(f"  fetched {len(events)} events\n")

    by_outlet = partition_to_outlets(events, ts_to_outlet)

    # Ensure every routed-to outlet (including Book Tower brands that
    # didn't book any events this run) gets an empty block — keeps the
    # dashboard from showing stale data.
    routed_outlets = set(ts_to_outlet.values()) | set(BOOK_TOWER_BRANDS)
    routed_outlets.discard("__BOOK_TOWER__")

    print(f"Partitioned into {len(by_outlet)} outlets with events:")
    for outlet_id in sorted(routed_outlets):
        oevents = by_outlet.get(outlet_id, [])
        _v = run_validation(
            rows=oevents, model_cls=TripleseatEvent, source="tripleseat_event",
            outlets_touched=[outlet_id],
            data_dir=Path(__file__).resolve().parent.parent / "data",
        )
        sys.stdout.write(f"  tripleseat_event validation [{outlet_id}]: "
                         f"{_v['rows_valid']}/{_v['rows_in']} ok, "
                         f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned\n")
        oevents = _v['valid_rows']
        block = build_outlet_events_block(oevents)
        payload = load_outlet(data_dir, outlet_id)
        payload["events"] = block
        write_outlet(data_dir, outlet_id, payload)
        rev = sum(e.get("booked_revenue") or 0 for e in block["events"])
        print(f"  ✓ {outlet_id:<14} {len(block['events']):>5} events, ${rev:,.0f} booked revenue")
    return 0


def cmd_print_config(ts_to_outlet: dict[int, str]) -> int:
    if not ts_to_outlet:
        print("(empty — set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY and re-run)")
        return 1
    print(f"{'ts_location_id':<16}  outlet")
    print("-" * 40)
    for ts_id, slug in sorted(ts_to_outlet.items()):
        print(f"{ts_id:<16}  {slug}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--probe", action="store_true", help="auth-only probe")
    ap.add_argument("--discover", action="store_true", help="list all sites + locations")
    ap.add_argument("--print-config", action="store_true",
                    help="show resolved TS site → outlet map (no network)")
    ap.add_argument("--dry-run", action="store_true", help="write fixture; no network")
    ap.add_argument("--data-dir", default="../data", help="dir of <outlet>.json files")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    if args.dry_run:
        return cmd_sync("DRY", "DRY", {}, data_dir, dry_run=True)

    ck, cs, ts_to_outlet = resolve_credentials()

    if args.print_config:
        return cmd_print_config(ts_to_outlet)
    if not (ck and cs):
        sys.stderr.write(
            "no Tripleseat credentials — set SUPABASE_URL+SUPABASE_SERVICE_ROLE_KEY "
            "OR TRIPLESEAT_CONSUMER_KEY+SECRET. Exiting cleanly (no-op).\n"
        )
        return 0

    if args.probe:
        return cmd_probe(ck, cs)
    if args.discover:
        return cmd_discover(ck, cs)
    return cmd_sync(ck, cs, ts_to_outlet, data_dir, dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())
