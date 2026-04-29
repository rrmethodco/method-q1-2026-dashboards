#!/usr/bin/env python3
"""
Method Co — MarginEdge Cost of Goods sync.

Pulls invoice + vendor + product + category data from MarginEdge's Public
REST API and writes a `cogs` block into each `data/<outlet>.json` so the
dashboard's Cost of Goods section can render weekly + monthly COGS%,
top-vendor spend, and category trends.

Sits alongside `toast_sync.py` (orders + labor) and
`google_reviews_sync.py` (reviews + business hours) on the same nightly
cron rhythm.

============================================================================
SETUP — three values to lock in from MarginEdge (one-time, ~5 min)
============================================================================
The MarginEdge developer portal is a JS SPA behind Cloudflare and the
public help-center article 403s non-browser clients, so this scaffold
codes the auth + endpoints behind a thin client where the three
operator-supplied values plug in:

  1. **Per-outlet API keys.** In MarginEdge as an admin: click your name
     (top right) → Settings → Security → "Create new API key". Mint one
     per outlet. Save the key on display — it's revealed once.

     If the Security tab isn't visible for an outlet, the public API is
     not yet enabled. Email Jeff Burger (jeff@marginedge.com) with the
     restaurant names and ask him to flip it on.

  2. **Auth header style** — confirm in the portal whether MarginEdge
     wants `Authorization: Bearer <key>` or `X-API-Key: <key>`. Set
     env vars accordingly:
       MARGINEDGE_AUTH_HEADER=Authorization     (default)
       MARGINEDGE_AUTH_PREFIX=Bearer            (or empty string)

  3. **Endpoint paths** — the portal's Invoices/Vendors/Products/
     Categories endpoint URLs and pagination convention. Default values
     below are MarginEdge's accounting-export convention; override via
     env if the portal differs:
       MARGINEDGE_BASE_URL=https://api.marginedge.com/v1
       MARGINEDGE_INVOICES_PATH=/invoices?startDate={start}&endDate={end}
       MARGINEDGE_VENDORS_PATH=/vendors
       MARGINEDGE_PRODUCTS_PATH=/products
       MARGINEDGE_CATEGORIES_PATH=/categories

============================================================================
GitHub Secrets
============================================================================
  MARGINEDGE_KEYS — outlet_id=key;outlet_id=key;...
                    (e.g. lsbr=ABCD;lowland=EFGH;...)

  Optional overrides (only set if the portal differs from defaults):
  MARGINEDGE_BASE_URL, MARGINEDGE_AUTH_HEADER, MARGINEDGE_AUTH_PREFIX,
  MARGINEDGE_INVOICES_PATH, MARGINEDGE_VENDORS_PATH,
  MARGINEDGE_PRODUCTS_PATH, MARGINEDGE_CATEGORIES_PATH,
  MARGINEDGE_LOOKBACK_DAYS  (default 90)

============================================================================
Usage
============================================================================
  python3 marginedge_sync.py                    # all configured outlets
  python3 marginedge_sync.py --outlet lsbr      # one outlet
  python3 marginedge_sync.py --probe            # auth-only probe; no writes
                                                  (use this to confirm
                                                   each outlet's API key
                                                   reaches a known endpoint)
  python3 marginedge_sync.py --dry-run          # write fixture, no network

Behavior:
  - Append-merges with existing `cogs` block on natural key (invoice_id)
    so re-runs don't double-count and historical seed survives.
  - Atomic write via .tmp.
  - Exits 0 cleanly when MARGINEDGE_KEYS is missing — lets the workflow
    schedule before secrets are populated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


# ---------- config ----------

BASE_URL = (os.environ.get("MARGINEDGE_BASE_URL") or "https://api.marginedge.com/v1").rstrip("/")
AUTH_HEADER = os.environ.get("MARGINEDGE_AUTH_HEADER") or "Authorization"
AUTH_PREFIX = os.environ.get("MARGINEDGE_AUTH_PREFIX")
if AUTH_PREFIX is None:
    AUTH_PREFIX = "Bearer"
INVOICES_PATH = os.environ.get("MARGINEDGE_INVOICES_PATH") or "/invoices?startDate={start}&endDate={end}"
VENDORS_PATH = os.environ.get("MARGINEDGE_VENDORS_PATH") or "/vendors"
PRODUCTS_PATH = os.environ.get("MARGINEDGE_PRODUCTS_PATH") or "/products"
CATEGORIES_PATH = os.environ.get("MARGINEDGE_CATEGORIES_PATH") or "/categories"
LOOKBACK_DAYS = int(os.environ.get("MARGINEDGE_LOOKBACK_DAYS") or 90)

REQUEST_TIMEOUT = 45
USER_AGENT = "MethodCo-Dashboards/1.0 (marginedge_sync.py; +https://github.com/rrmethodco)"
RATE_LIMIT_SLEEP = 0.25  # seconds between calls — ME's published limits aren't documented


def parse_keys(raw: str) -> dict[str, str]:
    """MARGINEDGE_KEYS: outlet_id=key;outlet_id=key;..."""
    out: dict[str, str] = {}
    for chunk in (raw or "").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        oid, key = chunk.split("=", 1)
        oid = oid.strip(); key = key.strip()
        if oid and key:
            out[oid] = key
    return out


# ---------- thin client ----------

class MarginEdgeClient:
    """One client per outlet (per-restaurant API key)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        # Auth header: either `Authorization: Bearer xxx` or `X-API-Key: xxx`
        # depending on what the portal documents. AUTH_PREFIX is empty for
        # the X-API-Key case.
        auth_value = f"{AUTH_PREFIX} {api_key}".strip() if AUTH_PREFIX else api_key
        self.session.headers.update({
            AUTH_HEADER: auth_value,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })

    def _get_paged(self, path: str) -> list[dict]:
        """Fetch a possibly paginated endpoint, returning all rows.

        Default assumption: response is either a bare list `[{...}]` OR a
        wrapped dict `{"data": [...], "next": "<url>"}` / `{"items": [...]}`.
        Pagination convention varies by API — adjust here once the portal's
        actual envelope is confirmed.
        """
        url = f"{BASE_URL}{path}"
        rows: list[dict] = []
        seen = set()  # cycle guard — some pagination loops on bad cursors
        while url and url not in seen:
            seen.add(url)
            r = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                # Backoff per ME's undocumented rate limits
                import time
                time.sleep(2.0)
                continue
            r.raise_for_status()
            try:
                body = r.json()
            except ValueError:
                break
            if isinstance(body, list):
                rows.extend(body)
                break  # bare list = no pagination
            elif isinstance(body, dict):
                # Common envelope shapes
                page = body.get("data") or body.get("items") or body.get("results") or []
                if isinstance(page, list):
                    rows.extend(page)
                # Common pagination cursors
                next_url = body.get("next") or body.get("nextUrl") or (body.get("links") or {}).get("next")
                if next_url and isinstance(next_url, str):
                    url = next_url if next_url.startswith("http") else f"{BASE_URL}{next_url}"
                else:
                    url = None
            else:
                break
        return rows

    def get_invoices(self, start: str, end: str) -> list[dict]:
        return self._get_paged(INVOICES_PATH.format(start=start, end=end))

    def get_vendors(self) -> list[dict]:
        return self._get_paged(VENDORS_PATH)

    def get_products(self) -> list[dict]:
        return self._get_paged(PRODUCTS_PATH)

    def get_categories(self) -> list[dict]:
        return self._get_paged(CATEGORIES_PATH)


# ---------- transform ----------

def _line_total(li: dict) -> float:
    """Resolve a line item's $ amount across MarginEdge's possible shapes."""
    for k in ("extended", "extendedTotal", "total", "lineTotal", "amount"):
        v = li.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    qty = li.get("quantity") or li.get("qty") or 0
    unit = li.get("unitPrice") or li.get("unit_cost") or li.get("price") or 0
    try:
        return float(qty) * float(unit)
    except (TypeError, ValueError):
        return 0.0


def _invoice_date(inv: dict) -> str:
    for k in ("date", "invoiceDate", "billDate", "transactionDate"):
        v = inv.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return ""


def _invoice_total(inv: dict) -> float:
    for k in ("total", "invoiceTotal", "amount", "grandTotal"):
        v = inv.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    # Fallback: sum line items
    return sum(_line_total(li) for li in (inv.get("lineItems") or inv.get("items") or []))


def transform_invoice(inv: dict, vendor_lookup: dict, category_lookup: dict) -> dict:
    """Project one ME invoice into our schema, dropping fields we don't use."""
    line_items = inv.get("lineItems") or inv.get("items") or []
    out_li = []
    for li in line_items:
        cat_id = li.get("categoryId") or li.get("category_id")
        cat_name = category_lookup.get(cat_id) if cat_id else (li.get("categoryName") or li.get("category"))
        out_li.append({
            "product_id": li.get("productId") or li.get("product_id") or li.get("id"),
            "product_name": li.get("productName") or li.get("product_name") or li.get("name"),
            "category": cat_name,
            "quantity": li.get("quantity") or li.get("qty"),
            "unit": li.get("unit") or li.get("uom"),
            "unit_price": li.get("unitPrice") or li.get("unit_cost") or li.get("price"),
            "extended": _line_total(li),
        })
    vendor_id = inv.get("vendorId") or inv.get("vendor_id") or (inv.get("vendor") or {}).get("id")
    vendor_name = inv.get("vendorName") or inv.get("vendor_name") or vendor_lookup.get(vendor_id) \
        or (inv.get("vendor") or {}).get("name")
    return {
        "invoice_id": inv.get("id") or inv.get("invoiceId"),
        "invoice_number": inv.get("invoiceNumber") or inv.get("number"),
        "date": _invoice_date(inv),
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "total": _invoice_total(inv),
        "status": inv.get("status"),
        "line_items": out_li,
    }


def build_rollups(invoices: list[dict]) -> dict:
    """Compute weekly + monthly category & top-vendor rollups."""
    by_week: dict[str, dict] = {}
    by_month: dict[str, dict] = {}

    def _ym(d: str) -> str:
        return d[:7] if len(d) >= 7 else ""

    def _week_start(d: str) -> str:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            return (dt - timedelta(days=dt.weekday())).isoformat()  # Monday
        except (ValueError, TypeError):
            return ""

    def _bucket(map_, key, inv):
        if not key:
            return
        if key not in map_:
            map_[key] = {"total_cogs": 0.0, "by_category": defaultdict(float),
                         "by_vendor": defaultdict(float)}
        b = map_[key]
        for li in inv["line_items"]:
            b["by_category"][li.get("category") or "Uncategorized"] += float(li.get("extended") or 0)
        b["total_cogs"] += float(inv.get("total") or 0)
        b["by_vendor"][inv.get("vendor_name") or "Unknown vendor"] += float(inv.get("total") or 0)

    for inv in invoices:
        d = inv.get("date") or ""
        _bucket(by_week, _week_start(d), inv)
        _bucket(by_month, _ym(d), inv)

    def _finalize(map_, key_name):
        out = []
        for k, b in map_.items():
            top5 = sorted(b["by_vendor"].items(), key=lambda kv: -kv[1])[:5]
            out.append({
                key_name: k,
                "total_cogs": round(b["total_cogs"], 2),
                "by_category": {cat: round(v, 2) for cat, v in b["by_category"].items()},
                "by_vendor_top5": [{"vendor": v, "total": round(t, 2)} for v, t in top5],
            })
        out.sort(key=lambda r: r[key_name])
        return out

    return {
        "weekly_rollup": _finalize(by_week, "week_start"),
        "monthly_rollup": _finalize(by_month, "month"),
    }


def merge_invoices(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Append-merge by invoice_id. Fresh wins on collisions."""
    by_id = {(inv.get("invoice_id") or ""): inv for inv in (existing or [])}
    for inv in fresh:
        iid = inv.get("invoice_id") or ""
        if iid:
            by_id[iid] = inv
    return sorted(by_id.values(), key=lambda r: r.get("date") or "")


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


# ---------- commands ----------

def cmd_probe(keys: dict[str, str]) -> int:
    """Auth-only check: hit /vendors for each outlet, report status."""
    print(f"Probing {len(keys)} outlet(s) against {BASE_URL}{VENDORS_PATH}...\n")
    print(f"{'outlet':<14} {'status':<10} {'vendors_returned':<18}")
    print("-" * 50)
    any_fail = False
    for oid, key in keys.items():
        try:
            client = MarginEdgeClient(key)
            r = client.session.get(f"{BASE_URL}{VENDORS_PATH}", timeout=REQUEST_TIMEOUT)
            n = "—"
            if r.status_code == 200:
                try:
                    body = r.json()
                    rows = body if isinstance(body, list) else (body.get("data") or body.get("items") or [])
                    n = str(len(rows))
                except Exception:
                    n = "non-JSON"
            else:
                any_fail = True
            print(f"{oid:<14} HTTP {r.status_code:<6} {n}")
        except Exception as e:
            any_fail = True
            print(f"{oid:<14} ERROR      {e}")
    return 1 if any_fail else 0


def cmd_sync(keys: dict[str, str], data_dir: Path, only: str | None,
             lookback_days: int, dry_run: bool) -> int:
    if dry_run:
        print(f"[dry-run] no network; writing fixture to data/_marginedge_dry_run.json")
        fixture = {
            "as_of": date.today().isoformat(), "source": "marginedge_dry_run",
            "lookback_days": lookback_days,
            "invoices": [{"invoice_id": "TEST-1", "date": date.today().isoformat(),
                          "vendor_name": "Test Vendor", "total": 100.0,
                          "line_items": [{"product_name": "Test Item", "category": "Test",
                                          "extended": 100.0}]}],
            "weekly_rollup": [], "monthly_rollup": [],
            "vendors": [{"id": "v1", "name": "Test Vendor"}],
            "categories": [{"id": "c1", "name": "Test"}],
        }
        (data_dir / "_marginedge_dry_run.json").write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    targets = {oid: k for oid, k in keys.items() if not only or oid == only}
    if not targets:
        sys.stderr.write(f"no matching outlets (only={only!r})\n")
        return 1

    failures: list[str] = []
    for oid, key in targets.items():
        print(f"\n[{oid}] window {start_iso} → {end_iso}")
        try:
            client = MarginEdgeClient(key)
            vendors = client.get_vendors()
            categories = client.get_categories()
            invoices_raw = client.get_invoices(start_iso, end_iso)
            print(f"  fetched: {len(vendors)} vendors, {len(categories)} categories, "
                  f"{len(invoices_raw)} invoices")

            vendor_lookup = {(v.get("id") or v.get("vendorId")): (v.get("name") or v.get("vendorName"))
                             for v in vendors}
            cat_lookup = {(c.get("id") or c.get("categoryId")): (c.get("name") or c.get("categoryName"))
                          for c in categories}
            invoices = [transform_invoice(i, vendor_lookup, cat_lookup) for i in invoices_raw]

            payload = load_outlet(data_dir, oid)
            existing_cogs = (payload.get("cogs") or {})
            merged_invoices = merge_invoices(existing_cogs.get("invoices") or [], invoices)
            rollups = build_rollups(merged_invoices)

            payload["cogs"] = {
                "as_of": date.today().isoformat(),
                "source": "marginedge_api",
                "lookback_days": lookback_days,
                "invoices": merged_invoices,
                "vendors": [{"id": vid, "name": name} for vid, name in vendor_lookup.items() if vid],
                "categories": [{"id": cid, "name": name} for cid, name in cat_lookup.items() if cid],
                **rollups,
            }
            write_outlet(data_dir, oid, payload)
            n_existing = len(existing_cogs.get("invoices") or [])
            print(f"  ✓ {oid}  invoices: {n_existing} → {len(merged_invoices)} "
                  f"(+{len(merged_invoices) - n_existing})")
        except Exception as e:  # noqa: BLE001
            failures.append(oid)
            sys.stderr.write(f"  ✗ {oid}: {e}\n")

    if failures:
        sys.stderr.write(f"\n{len(failures)} outlet(s) failed: {failures}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--outlet", help="single outlet id (default: all configured)")
    ap.add_argument("--probe", action="store_true",
                    help="auth-only probe (hits /vendors); no writes")
    ap.add_argument("--dry-run", action="store_true",
                    help="write fixture; no network")
    ap.add_argument("--data-dir", default="../data",
                    help="dir of <outlet>.json files (default: ../data)")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                    help=f"days back to pull invoices (default: {LOOKBACK_DAYS})")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    raw = (os.environ.get("MARGINEDGE_KEYS") or "").strip()
    if not raw and not args.dry_run:
        sys.stderr.write("MARGINEDGE_KEYS missing — exiting cleanly (no-op)\n")
        return 0

    keys = parse_keys(raw)
    if args.dry_run:
        return cmd_sync({"_dry": "_dry"}, data_dir, args.outlet, args.lookback, dry_run=True)
    if not keys:
        sys.stderr.write("MARGINEDGE_KEYS parsed empty\n")
        return 0
    if args.probe:
        return cmd_probe(keys)
    return cmd_sync(keys, data_dir, args.outlet, args.lookback, dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())
