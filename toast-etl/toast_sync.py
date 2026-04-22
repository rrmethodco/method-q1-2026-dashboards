#!/usr/bin/env python3
"""
Method Co F&B -- Toast Sync

Pulls orders from the Toast API, transforms them into the shape the
Method_Co_FB_Performance_Dashboard.html expects, and writes a JSON
payload per outlet to ./data/<outlet>.json.

Expected dashboard shape (per revenue center):
  order_details[rcKey] = {
    daily:    [{date, orders, guests, amount, tip, gratuity, discount,
                ticket_time_sec_sum, ticket_time_count}],
    monthly:  [{month, orders, amount}],
    hour_dow: [{hour, dow, amount}],          # dow in Mon..Sun
    servers:  [{name, orders, guests, amount, tip,
                ticket_time_sec_sum, ticket_time_count}],
    tables:   [{name, orders, guests, amount}],
    totals:   {tip_bins: [...11 buckets]},     # 0-10%, 10-20% ... 100%+
  }

  Weighted-average ticket time over a period = sum(ticket_time_sec_sum) /
  sum(ticket_time_count) / 60 for minutes. Aggregating sum+count separately
  (vs. storing pre-computed averages) preserves precision across arbitrary
  date filters.

Per Ross:
  - Close time for a check = *payment time*, not order open / firing time.
    Toast ordersBulk returns paidDate on the Check object -- we key on that.
  - Avg Ticket Time = paidDate - openedDate (on the check). We fall back to
    order.openedDate if the check lacks one. Emitted as weighted sum/count
    per day + per server so the dashboard can average over any period slice.
    Extreme outliers (>8h) are dropped -- they're almost always stale checks
    re-opened to tack on a tab, not real service time.

Design notes:
  - OAuth2 machine-client flow (partner credentials).
  - One Toast restaurantGuid = one revenue center in our model. Outlets
    group multiple guids. Configure via TOAST_OUTLETS env (see below).
  - Pagination via `page` param on ordersBulk. Date range chunked per day
    to stay under the 1h window limit Toast enforces on some tenants.
  - Idempotent output. Safe to re-run; writes atomically via tmp file.
  - `--dry-run` skips network + writes a fixture payload so you can sanity
    check the transform against the dashboard without real credentials.

Required env:
  TOAST_CLIENT_ID           partner client id
  TOAST_CLIENT_SECRET       partner client secret
  TOAST_OUTLETS             outlet=rc_key:guid[@filter][,rc_key:guid[@filter]]; ...
                            Filter forms:
                              @rc=Name1|Name2       include only these RCs
                              @rc_not=Name1|Name2   include all RCs except these
                                                    (catch-all -- also matches orders
                                                     with no RC assigned)
                            example:
                              # one GUID per rc_key, no filter
                              hiroki_phl=main:45d266c6-...;
                              # shared GUID split by RC name (LSBR)
                              lsbr=bar_rotunda:99d1...@rc=Bar Rotunda,le_supreme:99d1...@rc_not=Bar Rotunda;
                              # multiple GUIDs merged into one rc_key (Quoin legacy rollup)
                              quoin_restaurant=main:guid-a,main:guid-b,main:guid-c
  DAYS_BACK                 lookback window in days (default: 400, ~13mo for YoY)
  TOAST_BASE                override for sandbox (default prod ws-api)

Usage:
  python3 toast_sync.py                      # sync all outlets
  python3 toast_sync.py --outlet lsbr        # one outlet
  python3 toast_sync.py --dry-run            # emit fixture, no network
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


# ---------- config ----------

TOAST_BASE = (os.environ.get("TOAST_BASE") or "https://ws-api.toasttab.com").rstrip("/")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "400"))
REQUEST_TIMEOUT = 45
PAGE_SIZE = 100
SLEEP_BETWEEN_PAGES = 0.25
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIP_BIN_EDGES = [0.10 * i for i in range(11)]  # 11 buckets: 0-10%,10-20%,...,100%+

# ---------- outlet config parser ----------


def parse_outlets() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Parse TOAST_OUTLETS into {outlet_id: {rc_key: [Source, ...]}}.

    Source shape:
      {"guid": "<restaurantGuid>",
       "include": ["RC name", ...] | None,  # whitelist of Toast RC names
       "exclude": ["RC name", ...] | None}  # blacklist (catch-all bucket)

    Config syntax:
      outlet_id=rc_key:GUID[@filter][,rc_key:GUID[@filter]][; ...]
    where @filter is either:
      @rc=Name1|Name2       keep only orders whose RC name is in this list
      @rc_not=Name1|Name2   keep everything except these RCs (plus orders
                            with no RC assigned -- "everything else" bucket)

    Examples:
      # 1:1 guid:rc_key, no filter
      hiroki_phl=main:45d266c6-7dd1-43cb-9b09-2c157f277a3c

      # shared GUID split by RC name (Bar Rotunda + Le Supreme share one Toast instance)
      lsbr=bar_rotunda:99d1583c-...@rc=Bar Rotunda,le_supreme:99d1583c-...@rc_not=Bar Rotunda

      # multiple GUIDs merged under one rc_key (Quoin Rooftop + Simmer Down historicals)
      quoin_restaurant=main:2d1d8888-...,main:01ed27a0-...,main:fdef7a8f-...
    """
    raw = os.environ.get("TOAST_OUTLETS", "").strip()
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if not raw:
        return out
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        outlet_id, pairs = chunk.split("=", 1)
        rc_map: dict[str, list[dict[str, Any]]] = {}
        for p in pairs.split(","):
            p = p.strip()
            if ":" not in p:
                continue
            core, *filter_parts = p.split("@", 1)
            if ":" not in core:
                continue
            rc_key, guid = core.split(":", 1)
            src: dict[str, Any] = {"guid": guid.strip(), "include": None, "exclude": None}
            if filter_parts:
                fstr = filter_parts[0].strip()
                if fstr.startswith("rc_not="):
                    src["exclude"] = [n.strip() for n in fstr[len("rc_not="):].split("|") if n.strip()]
                elif fstr.startswith("rc="):
                    src["include"] = [n.strip() for n in fstr[len("rc="):].split("|") if n.strip()]
                else:
                    sys.stderr.write(f"[parse_outlets] ignoring unknown filter '@{fstr}' on {rc_key}:{guid[:8]}...\n")
            rc_map.setdefault(rc_key.strip(), []).append(src)
        if rc_map:
            out[outlet_id.strip()] = rc_map
    return out


# ---------- auth ----------


def get_token(client_id: str, client_secret: str) -> str:
    url = f"{TOAST_BASE}/authentication/v1/authentication/login"
    r = requests.post(
        url,
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    tok = body.get("token", {}).get("accessToken")
    if not tok:
        raise RuntimeError(f"no accessToken in auth response: {body}")
    return tok


# ---------- fetch ----------


def _iso_day_range(d: datetime) -> tuple[str, str]:
    start = d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%S.000-0000"),
        end.strftime("%Y-%m-%dT%H:%M:%S.999-0000"),
    )


def fetch_orders_for_day(token: str, guid: str, day: datetime) -> list[dict[str, Any]]:
    """Pull every order for a single business day via /orders/v2/ordersBulk."""
    start, end = _iso_day_range(day)
    headers = {"Authorization": f"Bearer {token}", "Toast-Restaurant-External-ID": guid}
    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{TOAST_BASE}/orders/v2/ordersBulk"
        r = requests.get(
            url,
            headers=headers,
            params={"startDate": start, "endDate": end, "pageSize": PAGE_SIZE, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 429:
            time.sleep(2.0)
            continue
        r.raise_for_status()
        batch = r.json() or []
        collected.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return collected


def fetch_orders(token: str, guid: str, start_day: datetime, end_day: datetime) -> list[dict[str, Any]]:
    """Day-by-day pull to keep windows small and stay under Toast's query ceiling."""
    results: list[dict[str, Any]] = []
    cur = start_day
    while cur <= end_day:
        try:
            day_orders = fetch_orders_for_day(token, guid, cur)
            results.extend(day_orders)
        except requests.HTTPError as e:
            sys.stderr.write(f"[{guid}] {cur:%Y-%m-%d} fetch failed: {e}\n")
        cur += timedelta(days=1)
    return results


def fetch_revenue_centers(token: str, guid: str) -> list[dict[str, Any]]:
    """GET /config/v2/revenueCenters for a restaurant.

    Returns a list of {"guid": "...", "name": "...", ...}. Used to resolve
    human-friendly RC names in TOAST_OUTLETS filters into the guids that
    appear on order.revenueCenter.guid.
    """
    url = f"{TOAST_BASE}/config/v2/revenueCenters"
    headers = {"Authorization": f"Bearer {token}", "Toast-Restaurant-External-ID": guid}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 429:
        time.sleep(2.0)
        return fetch_revenue_centers(token, guid)
    r.raise_for_status()
    body = r.json() or []
    return body if isinstance(body, list) else body.get("results", [])


def filter_orders_by_rc(
    orders: list[dict[str, Any]],
    rc_guids: set[str],
    exclude: bool,
) -> list[dict[str, Any]]:
    """Filter orders by revenueCenter.guid.

    include mode (exclude=False): keep orders whose RC guid is in rc_guids.
                                  Orders with no RC assigned are dropped.
    exclude mode (exclude=True):  keep orders whose RC guid is NOT in rc_guids,
                                  PLUS orders with no RC assigned (these flow
                                  into the catch-all "everything else" bucket).
    """
    kept: list[dict[str, Any]] = []
    for o in orders:
        rc_ref = (o.get("revenueCenter") or {}).get("guid")
        if rc_ref is None:
            if exclude:
                kept.append(o)
            continue
        in_set = rc_ref in rc_guids
        # XOR: (in_set and include) or (not in_set and exclude)
        if in_set != exclude:
            kept.append(o)
    return kept


# ---------- transform ----------


def _tip_bin(amount: float, tip: float) -> int:
    if not amount or amount <= 0:
        return 0
    ratio = tip / amount
    for i in range(10):
        if ratio < TIP_BIN_EDGES[i + 1]:
            return i
    return 10  # 100%+


def _as_local_date(iso: str | None) -> tuple[str, int, str] | None:
    """Return (YYYY-MM-DD, hour 0-23, DOW) in UTC. Close enough for heatmaps;
    swap to the restaurant's IANA tz once we have it per-outlet."""
    if not iso:
        return None
    try:
        # Toast emits 2026-04-22T15:03:21.000Z or with an offset.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d"), dt.hour, DOW[dt.weekday()]


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


# Ticket-time sanity guardrail. Anything over this is treated as a re-opened
# / dangling check and excluded from the average.
TICKET_TIME_MAX_SEC = 8 * 60 * 60


def transform_orders(raw_orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold raw Toast orders into the dashboard shape for a single revenue center."""
    daily: dict[str, dict[str, float]] = {}
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: {"orders": 0, "amount": 0.0})
    hour_dow_map: dict[tuple[int, str], float] = defaultdict(float)
    servers: dict[str, dict[str, float]] = {}
    tables: dict[str, dict[str, float]] = {}
    tip_bins = [0] * 11

    for order in raw_orders:
        if order.get("voided") or order.get("deleted"):
            continue
        checks = order.get("checks") or []
        for check in checks:
            if check.get("voided") or check.get("deleted"):
                continue
            paid_iso = check.get("paidDate") or check.get("closedDate") or order.get("closedDate")
            parsed = _as_local_date(paid_iso)
            if not parsed:
                continue
            date_str, hour, dow = parsed
            month = date_str[:7]

            amount = float(check.get("amount") or 0.0)  # pre-tax subtotal
            tip = float(check.get("tipAmount") or 0.0)
            gratuity = float(
                sum(float(sc.get("amount") or 0.0) for sc in (check.get("appliedServiceCharges") or []))
            )
            discount = float(
                sum(float(ad.get("discountAmount") or 0.0) for ad in (check.get("appliedDiscounts") or []))
            )
            guests = int(order.get("numberOfGuests") or check.get("customer", {}).get("guestCount") or 0)

            # Ticket time: paidDate - openedDate on the check (fall back to order.openedDate).
            # Only count if both timestamps are present and duration is within the guardrail.
            opened_dt = _parse_iso(check.get("openedDate") or order.get("openedDate"))
            paid_dt = _parse_iso(paid_iso)
            ticket_sec = None
            if opened_dt and paid_dt:
                delta = (paid_dt - opened_dt).total_seconds()
                if 0 < delta <= TICKET_TIME_MAX_SEC:
                    ticket_sec = delta

            # daily
            d = daily.setdefault(
                date_str,
                {"date": date_str, "orders": 0, "guests": 0, "amount": 0.0, "tip": 0.0,
                 "gratuity": 0.0, "discount": 0.0,
                 "ticket_time_sec_sum": 0.0, "ticket_time_count": 0},
            )
            d["orders"] += 1
            d["guests"] += guests
            d["amount"] += amount
            d["tip"] += tip
            d["gratuity"] += gratuity
            d["discount"] += discount
            if ticket_sec is not None:
                d["ticket_time_sec_sum"] += ticket_sec
                d["ticket_time_count"] += 1

            # monthly
            m = monthly[month]
            m["month"] = month  # idempotent
            m["orders"] += 1
            m["amount"] += amount

            # hour_dow -- revenue ($) per hour slot
            hour_dow_map[(hour, dow)] += amount

            # servers (by assigned employee on the check)
            server_ref = check.get("server") or {}
            server_name = (server_ref.get("firstName") or "").strip()
            last = (server_ref.get("lastName") or "").strip()
            if server_name or last:
                display = f"{server_name} {last[:1]}.".strip() if last else server_name
                s = servers.setdefault(
                    display,
                    {"name": display, "orders": 0, "guests": 0, "amount": 0.0, "tip": 0.0,
                     "ticket_time_sec_sum": 0.0, "ticket_time_count": 0},
                )
                s["orders"] += 1
                s["guests"] += guests
                s["amount"] += amount
                s["tip"] += tip
                if ticket_sec is not None:
                    s["ticket_time_sec_sum"] += ticket_sec
                    s["ticket_time_count"] += 1

            # tables (by table name)
            table_ref = (order.get("table") or {}).get("name") or ""
            if table_ref:
                t = tables.setdefault(table_ref, {"name": table_ref, "orders": 0, "guests": 0, "amount": 0.0})
                t["orders"] += 1
                t["guests"] += guests
                t["amount"] += amount

            # tip bins (check-level)
            tip_bins[_tip_bin(amount, tip)] += 1

    out = {
        "daily": sorted(daily.values(), key=lambda r: r["date"]),
        "monthly": sorted(monthly.values(), key=lambda r: r["month"]),
        "hour_dow": [
            {"hour": h, "dow": d, "amount": round(v, 2)}
            for (h, d), v in sorted(hour_dow_map.items(), key=lambda kv: (kv[0][0], DOW.index(kv[0][1])))
        ],
        "servers": sorted(servers.values(), key=lambda r: r["amount"], reverse=True),
        "tables": sorted(tables.values(), key=lambda r: r["amount"], reverse=True),
        "totals": {"tip_bins": tip_bins},
    }

    # round money fields for cleaner JSON
    for row in out["daily"]:
        for k in ("amount", "tip", "gratuity", "discount"):
            row[k] = round(row[k], 2)
        row["ticket_time_sec_sum"] = round(row["ticket_time_sec_sum"], 1)
    for row in out["monthly"]:
        row["amount"] = round(row["amount"], 2)
    for row in out["servers"]:
        row["amount"] = round(row["amount"], 2)
        row["tip"] = round(row["tip"], 2)
        row["ticket_time_sec_sum"] = round(row["ticket_time_sec_sum"], 1)
    for row in out["tables"]:
        row["amount"] = round(row["amount"], 2)

    return out


# ---------- partner enumeration ----------


def list_partner_restaurants(token: str) -> list[dict[str, Any]]:
    """Enumerate every restaurant this partner client has access to.

    Endpoint: GET /partners/v1/restaurants
    Returns rows with restaurantGuid, restaurantName, locationName,
    managementGroupGuid, createdByEmailAddress, modifiedDate, etc.
    """
    url = f"{TOAST_BASE}/partners/v1/restaurants"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json() or []
    return body if isinstance(body, list) else body.get("results", [])


def print_restaurants(rows: list[dict[str, Any]]) -> None:
    """Human-readable dump of restaurant rows, plus a ready-to-paste
    TOAST_OUTLETS template at the bottom."""
    if not rows:
        sys.stdout.write("no restaurants returned -- client may not be attached to any locations yet\n")
        return

    def _col(r: dict[str, Any], *keys: str) -> str:
        for k in keys:
            v = r.get(k)
            if v:
                return str(v)
        return ""

    sys.stdout.write(f"\n{'GUID':<40} {'LOCATION':<30} {'NAME':<30} {'GROUP':<40}\n")
    sys.stdout.write("-" * 142 + "\n")
    for r in rows:
        guid = _col(r, "restaurantGuid", "guid")
        loc = _col(r, "locationName", "restaurantName")[:29]
        name = _col(r, "restaurantName", "locationName")[:29]
        mg = _col(r, "managementGroupGuid")[:39]
        sys.stdout.write(f"{guid:<40} {loc:<30} {name:<30} {mg:<40}\n")

    sys.stdout.write("\n--- Paste into TOAST_OUTLETS secret (edit rc_key + outlet_id as needed) ---\n")
    for r in rows:
        guid = _col(r, "restaurantGuid", "guid")
        name = _col(r, "locationName", "restaurantName") or "UNKNOWN"
        slug = "".join(c for c in name.lower() if c.isalnum() or c == "_")[:24] or "location"
        sys.stdout.write(f"{slug}=main:{guid};\n")


# ---------- orchestration ----------


def sync_outlet(
    outlet_id: str,
    rc_map: dict[str, list[dict[str, Any]]],
    token: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Sync one outlet.

    Fetches orders ONCE per unique restaurantGuid in the outlet (so a shared
    GUID like LSBR's 99d1583c-... isn't hit twice), then applies per-source
    RC-name filters before folding into transform_orders().
    """
    # Collect every unique restaurant GUID referenced by any rc_key's sources.
    unique_guids: set[str] = {src["guid"] for sources in rc_map.values() for src in sources}
    needs_rc_lookup = any(
        src.get("include") or src.get("exclude")
        for sources in rc_map.values()
        for src in sources
    )

    # Pull orders once per restaurant.
    orders_by_guid: dict[str, list[dict[str, Any]]] = {}
    for rest_guid in unique_guids:
        sys.stdout.write(f"[{outlet_id}] pulling orders {start:%Y-%m-%d} -> {end:%Y-%m-%d} (guid={rest_guid})\n")
        sys.stdout.flush()
        orders_by_guid[rest_guid] = fetch_orders(token, rest_guid, start, end)
        sys.stdout.write(f"[{outlet_id}] guid={rest_guid[:8]}... {len(orders_by_guid[rest_guid])} orders\n")

    # Resolve RC name -> guid per restaurant (only if any filter is configured).
    rc_cache: dict[str, dict[str, str]] = {}
    if needs_rc_lookup:
        for rest_guid in unique_guids:
            try:
                rcs = fetch_revenue_centers(token, rest_guid)
                rc_cache[rest_guid] = {(r.get("name") or "").strip(): r.get("guid") for r in rcs if r.get("guid")}
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[{outlet_id}] RC lookup failed for {rest_guid[:8]}...: {e}\n")
                rc_cache[rest_guid] = {}

    # Combine sources per rc_key, applying filters.
    order_details: dict[str, Any] = {}
    for rc_key, sources in rc_map.items():
        combined: list[dict[str, Any]] = []
        for src in sources:
            rest_guid = src["guid"]
            orders = orders_by_guid.get(rest_guid, [])
            include = src.get("include")
            exclude = src.get("exclude")
            if include or exclude:
                name_to_guid = rc_cache.get(rest_guid, {})
                names = include or exclude
                rc_guids = {name_to_guid[n] for n in names if n in name_to_guid}
                missing = [n for n in names if n not in name_to_guid]
                if missing:
                    sys.stderr.write(
                        f"[{outlet_id}:{rc_key}] WARNING: RC name(s) not found on {rest_guid[:8]}...: {missing}. "
                        f"Available: {sorted(name_to_guid.keys())}\n"
                    )
                filtered = filter_orders_by_rc(orders, rc_guids, exclude=bool(exclude))
                mode = "exclude" if exclude else "include"
                sys.stdout.write(
                    f"[{outlet_id}:{rc_key}] guid={rest_guid[:8]}... {mode}={names} -> {len(filtered)}/{len(orders)} orders\n"
                )
                combined.extend(filtered)
            else:
                combined.extend(orders)
                sys.stdout.write(f"[{outlet_id}:{rc_key}] guid={rest_guid[:8]}... -> {len(orders)} orders\n")
        if len(sources) > 1:
            sys.stdout.write(f"[{outlet_id}:{rc_key}] merged {len(sources)} sources -> {len(combined)} total orders\n")
        order_details[rc_key] = transform_orders(combined)

    return {
        "outlet_id": outlet_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "toast_api_v2",
        "order_details": order_details,
    }


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def dry_run_fixture() -> dict[str, Any]:
    """Emit a believable fixture so we can validate the dashboard DATA shape
    without hitting Toast. Mirrors what transform_orders would produce for a
    single 3-day slice with one server and one table."""
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(2, -1, -1)]
    daily = []
    monthly_map: dict[str, dict[str, float]] = {}
    for d in days:
        amt = 3200.0 + (d.day * 15)
        # Simulate 72 of 85 checks having both timestamps, averaging 47 min/ticket.
        ticket_count = 72
        ticket_sum = ticket_count * 47 * 60  # seconds
        daily.append(
            {
                "date": d.isoformat(),
                "orders": 85,
                "guests": 140,
                "amount": amt,
                "tip": amt * 0.18,
                "gratuity": 0.0,
                "discount": amt * 0.03,
                "ticket_time_sec_sum": float(ticket_sum),
                "ticket_time_count": ticket_count,
            }
        )
        mk = d.strftime("%Y-%m")
        mm = monthly_map.setdefault(mk, {"month": mk, "orders": 0, "amount": 0.0})
        mm["orders"] += 85
        mm["amount"] += amt
    hour_dow = [
        {"hour": h, "dow": DOW[d.weekday()], "amount": round(300.0 + h * 25.0, 2)}
        for d in days
        for h in range(11, 23)
    ]
    return {
        "outlet_id": "lsbr",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "toast_api_v2__DRY_RUN",
        "order_details": {
            "le_supreme": {
                "daily": daily,
                "monthly": sorted(monthly_map.values(), key=lambda r: r["month"]),
                "hour_dow": hour_dow,
                "servers": [
                    {"name": "Jordan P.", "orders": 140, "guests": 260, "amount": 5420.0, "tip": 960.0,
                     "ticket_time_sec_sum": 120 * 47 * 60.0, "ticket_time_count": 120}
                ],
                "tables": [
                    {"name": "T14", "orders": 42, "guests": 90, "amount": 1820.0}
                ],
                "totals": {"tip_bins": [0, 4, 38, 110, 70, 22, 6, 1, 0, 0, 0]},
            }
        },
    }


# ---------- cli ----------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync Toast orders into dashboard-ready JSON.")
    p.add_argument("--outlet", help="sync just this outlet id")
    p.add_argument("--days", type=int, default=DAYS_BACK, help=f"lookback window (default {DAYS_BACK})")
    p.add_argument("--outdir", default="data", help="output directory (default ./data)")
    p.add_argument("--dry-run", action="store_true", help="emit fixture, skip network")
    p.add_argument(
        "--list-restaurants",
        action="store_true",
        help="enumerate all restaurantGuids this partner client can access, print, and exit",
    )
    args = p.parse_args(argv)

    outdir = Path(args.outdir)

    if args.dry_run:
        payload = dry_run_fixture()
        write_atomic(outdir / f"{payload['outlet_id']}.json", payload)
        sys.stdout.write(f"wrote fixture -> {outdir / (payload['outlet_id'] + '.json')}\n")
        return 0

    client_id = os.environ.get("TOAST_CLIENT_ID")
    client_secret = os.environ.get("TOAST_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.stderr.write("TOAST_CLIENT_ID and TOAST_CLIENT_SECRET are required (or use --dry-run)\n")
        return 2

    if args.list_restaurants:
        token = get_token(client_id, client_secret)
        rows = list_partner_restaurants(token)
        print_restaurants(rows)
        return 0

    outlets = parse_outlets()
    if not outlets:
        sys.stderr.write("TOAST_OUTLETS is empty -- nothing to sync\n")
        return 2
    if args.outlet:
        if args.outlet not in outlets:
            sys.stderr.write(f"unknown outlet '{args.outlet}'. known: {', '.join(outlets)}\n")
            return 2
        outlets = {args.outlet: outlets[args.outlet]}

    token = get_token(client_id, client_secret)
    end_day = datetime.now(timezone.utc)
    start_day = end_day - timedelta(days=args.days)

    any_error = False
    for outlet_id, rc_map in outlets.items():
        try:
            payload = sync_outlet(outlet_id, rc_map, token, start_day, end_day)
            write_atomic(outdir / f"{outlet_id}.json", payload)
            sys.stdout.write(f"wrote {outdir / (outlet_id + '.json')}\n")
        except Exception as e:  # noqa: BLE001
            any_error = True
            sys.stderr.write(f"[{outlet_id}] sync failed: {e}\n")

    (outdir / "_synced_at.txt").write_text(
        datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n", encoding="utf-8"
    )
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
