#!/usr/bin/env python3
"""
Method Co — MarginEdge Cost of Goods sync.

Pulls order (invoice) totals + vendor catalog + category catalog from
MarginEdge's Public REST API and writes a `cogs` block into each
`data/<outlet>.json`. Sits alongside `toast_sync.py` (orders + labor)
and `google_reviews_sync.py` (reviews + business hours) on the same
nightly cron rhythm.

============================================================================
What we confirmed about the API (probed 2026-04-29)
============================================================================
  Auth:     X-Api-Key: <key>
  Base URL: https://api.marginedge.com/public
  Scoping:  Each request requires `restaurantUnitId` query param.
            One MarginEdge API key can see ALL units in the parent
            account — discovered via GET /restaurantUnits on probe.

  Endpoints (verified):
    GET /restaurantUnits                              → {restaurants:[{id,name},...]}
    GET /categories?restaurantUnitId=X                → {categories:[...], nextPage}
    GET /vendors?restaurantUnitId=X                   → {vendors:[...],    nextPage}
    GET /products?restaurantUnitId=X                  → {products:[...],   nextPage}
    GET /orders?restaurantUnitId=X&startDate=Y&endDate=Z → {orders:[{orderId,
        invoiceDate, vendorId, vendorName, invoiceNumber, customerNumber,
        paymentAccount, status, orderTotal, createdDate}, ...], nextPage}
    GET /orders/{orderId}?restaurantUnitId=X          → {…top-level fields,
        lineItems:[{categoryId, companyConceptProductId, linePrice,
        packagingId, quantity, unitPrice, vendorItemCode, vendorItemName}]}

  Pagination: `nextPage` cursor (base64-encoded). Pass back as `cursor`
              query param to fetch the next page. 100 rows/page.

  NOT EXPOSED:
    - "controllableProfitAndLoss" / theoretical food cost / ideal-vs-actual
      (these are MarginEdge UI features only)
    - per-outlet POS daily sales (we already have that from Toast)

============================================================================
Setup
============================================================================
  1. In MarginEdge as an admin: name (top right) → Settings → Security
     → Create new API key. Save on display. ONE key is sufficient — it
     sees every restaurant on the account.

     If the Security tab isn't visible, email Jeff Burger
     (jeff@marginedge.com) to enable the Public API.

  2. GitHub Secret:
       MARGINEDGE_API_KEY  = <the key>

  3. Optionally override defaults:
       MARGINEDGE_BASE_URL         = https://api.marginedge.com/public
       MARGINEDGE_LOOKBACK_DAYS    = 90
       MARGINEDGE_WITH_LINE_ITEMS  = 0   (1 enables per-order line items —
                                          ~100x slower; only enable when
                                          we need spend-by-category)

============================================================================
Usage
============================================================================
  python3 marginedge_sync.py                      # all configured outlets
  python3 marginedge_sync.py --outlet lowland     # one outlet
  python3 marginedge_sync.py --probe              # auth-only probe
  python3 marginedge_sync.py --discover-units     # print all restaurantUnitIds
  python3 marginedge_sync.py --dry-run            # write fixture, no network
  python3 marginedge_sync.py --with-line-items    # fetch line item detail
                                                    (slower; off by default)

Behavior:
  - Pulls catalog (vendors/categories) once per outlet — small.
  - Pulls orders for a configurable trailing window (default 90d).
  - Optionally fetches line items per order (off by default — adds 1
    API call per order, ~100x slower).
  - Builds weekly + monthly rollups: total_cogs, by_category (when line
    items enabled), by_vendor_top5, cogs_pct_revenue.
  - Append-merge by orderId so re-runs don't double-count.
  - Atomic write via .tmp.
  - Exits 0 cleanly when MARGINEDGE_API_KEY missing — workflow can run
    before secrets are populated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


# ---------- config ----------

BASE_URL = (os.environ.get("MARGINEDGE_BASE_URL") or "https://api.marginedge.com/public").rstrip("/")
LOOKBACK_DAYS = int(os.environ.get("MARGINEDGE_LOOKBACK_DAYS") or 90)
# Default to line-item fetch ON. Adds ~1 API call per order (~10 min for
# all 11 outlets vs. ~1 min without), in exchange for category-level
# rollups (food / beer / wine / liquor / NA beverage spend by period).
# Override with MARGINEDGE_WITH_LINE_ITEMS=0 for a fast catalog-only run.
WITH_LINE_ITEMS = (os.environ.get("MARGINEDGE_WITH_LINE_ITEMS") or "1") in ("1", "true", "yes")
REQUEST_TIMEOUT = 45
USER_AGENT = "MethodCo-Dashboards/1.0 (marginedge_sync.py; +https://github.com/rrmethodco)"
# ME's rate limits aren't published. Empirically, /orders/{id} starts
# returning 429 after ~100 consecutive calls at 10/sec — bumping the
# inter-call sleep to 0.30s (3.3 req/sec sustained) eliminates them.
RATE_LIMIT_SLEEP = 0.30
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BACKOFF_BASE = 3.0  # 3, 6, 9, 12, 15, 18s before giving up


# Outlet (data/<id>.json basename) → MarginEdge restaurantUnitId.
# Discovered 2026-04-29 from `/restaurantUnits` for Method's API key.
# Re-discover with `--discover-units` if the account changes.
OUTLET_TO_ME_UNIT = {
    "lsbr":          625097257,  # Le Supreme + Bar Rotunda
    "mulherins":     628612642,  # Wm. Mulherin's Sons
    "hiroki_det":    625096509,  # HIROKI - SAN
    "kampers":       625098218,  # Kamper's
    "quoin":         628616377,  # The Quoin Restaurant
    "lowland":       628614396,  # Lowland & The Quinte
    "rosemary_rose": 865768244,  # Rosemary Rose
    "hiroki_phl":    628614022,  # HIROKI (PHL)
    "anthology":     625099044,  # Anthology
    "little_wing":   650675034,  # Little Wing Goods (ROOST Baltimore building)
    "vessel":        650675034,  # Same ME entity as little_wing — Vessel + Little
                                 # Wing share procurement under one ROOST Baltimore
                                 # MarginEdge restaurant unit. Both outlets'
                                 # data/<id>.json get the same cogs block; the
                                 # dashboard surfaces it identically. Don't sum
                                 # COGS across these two when rolling up portfolio.
}


# ---------- thin client ----------

class MarginEdgeClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{BASE_URL}{path}"
        for attempt in range(RATE_LIMIT_RETRIES):
            r = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                # Exponential-ish backoff. Server occasionally returns a
                # Retry-After header; respect it when present.
                wait = float(r.headers.get("Retry-After") or RATE_LIMIT_BACKOFF_BASE * (attempt + 1))
                sys.stderr.write(f"    [429] {path} — sleeping {wait:.1f}s (attempt {attempt+1}/{RATE_LIMIT_RETRIES})\n")
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(RATE_LIMIT_SLEEP)
            return r.json()
        raise RuntimeError(f"rate-limited {RATE_LIMIT_RETRIES}x on {path}")

    def list_units(self) -> list[dict]:
        body = self._get("/restaurantUnits")
        return body.get("restaurants") or []

    def _get_paged(self, path: str, params: dict, list_key: str) -> list[dict]:
        """Paginate via the `nextPage` cursor (verified 2026-04-29:
        ME echoes the cursor as a `nextPage` query param on subsequent
        calls; other names like `cursor` are silently ignored which
        caused the script to loop on page 1 and hit rate limits)."""
        out: list[dict] = []
        cur_params = dict(params)
        for _ in range(100):  # hard cap to avoid runaway pagination
            body = self._get(path, cur_params)
            out.extend(body.get(list_key) or [])
            nxt = body.get("nextPage")
            if not nxt:
                break
            cur_params["nextPage"] = nxt
        return out

    def get_categories(self, unit_id: int) -> list[dict]:
        return self._get_paged("/categories", {"restaurantUnitId": unit_id}, "categories")

    def get_vendors(self, unit_id: int) -> list[dict]:
        return self._get_paged("/vendors", {"restaurantUnitId": unit_id}, "vendors")

    def get_products(self, unit_id: int) -> list[dict]:
        return self._get_paged("/products", {"restaurantUnitId": unit_id}, "products")

    def get_orders(self, unit_id: int, start: str, end: str) -> list[dict]:
        return self._get_paged("/orders",
                               {"restaurantUnitId": unit_id, "startDate": start, "endDate": end},
                               "orders")

    def get_order_detail(self, order_id: str, unit_id: int) -> dict:
        return self._get(f"/orders/{order_id}", {"restaurantUnitId": unit_id})


# ---------- transform ----------

# MarginEdge's categoryType taxonomy → buckets the dashboard cares about.
# Verified across all 11 outlets (n=703 categories): FOOD, BEER, WINE,
# LIQUOR, NA_BEVERAGES are the COGS-relevant types. LABOR rows are kept
# out of COGS rollups (labor cost is sourced from Toast). OTHER captures
# operating expenses (advertising, bank charges, cleaning, etc.) — also
# excluded from COGS. Anything not matched (e.g. null on Sake) buckets
# to "uncategorized" for visibility.
COGS_TYPES = {
    "FOOD":          "food",
    "BEER":          "beer",
    "WINE":          "wine",
    "LIQUOR":        "liquor",
    "NA_BEVERAGES":  "na_beverages",
}


def cogs_bucket(category_type: str | None) -> str:
    """Map a MarginEdge categoryType → our cogs bucket name.
    LABOR/OTHER/null all return None → excluded from COGS rollups."""
    if not category_type:
        return None
    return COGS_TYPES.get(category_type.upper())


def transform_order(o: dict, line_items: list | None,
                    category_lookup: dict, category_type_lookup: dict) -> dict:
    """Project an ME order into the dashboard's invoice schema.

    `category_type_lookup` maps categoryId → categoryType (FOOD, BEER,
    WINE, LIQUOR, NA_BEVERAGES, LABOR, OTHER). We attach categoryType
    on each line item so the dashboard can group spend by hospitality
    cost-of-goods buckets without a second lookup at render time.
    """
    out_li = []
    for li in (line_items or []):
        cat_id = li.get("categoryId")
        cat_name = category_lookup.get(cat_id)
        cat_type = category_type_lookup.get(cat_id)
        out_li.append({
            "product_id":    li.get("companyConceptProductId") or li.get("vendorItemCode"),
            "product_name":  li.get("vendorItemName"),
            "category":      cat_name,
            "category_id":   cat_id,
            "category_type": cat_type,           # FOOD / BEER / WINE / LIQUOR / NA_BEVERAGES / OTHER / LABOR
            "cogs_bucket":   cogs_bucket(cat_type),  # food/beer/wine/liquor/na_beverages or None
            "quantity":      li.get("quantity"),
            "unit_price":    li.get("unitPrice"),
            "extended":      li.get("linePrice"),
        })
    return {
        "invoice_id":     o.get("orderId"),
        "invoice_number": o.get("invoiceNumber"),
        "date":           o.get("invoiceDate") or o.get("createdDate"),
        "vendor_id":      o.get("vendorId"),
        "vendor_name":    o.get("vendorName"),
        "total":          o.get("orderTotal"),
        "status":         o.get("status"),
        "line_items":     out_li,
    }


def build_rollups(invoices: list[dict], net_sales_by_week: dict, net_sales_by_month: dict) -> dict:
    """Compute weekly + monthly category & top-vendor rollups.
    `net_sales_by_*` are dicts keyed on week_start (YYYY-MM-DD Monday)
    or month (YYYY-MM); used to compute cogs_pct_revenue."""
    by_week: dict[str, dict] = {}
    by_month: dict[str, dict] = {}

    def _ym(d: str) -> str:
        return d[:7] if len(d) >= 7 else ""

    def _week_start(d: str) -> str:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            return (dt - timedelta(days=dt.weekday())).isoformat()
        except (ValueError, TypeError):
            return ""

    def _bucket(map_, key, inv):
        if not key:
            return
        if key not in map_:
            map_[key] = {
                "total_cogs": 0.0,
                "cogs_only_total": 0.0,  # excludes OTHER/LABOR — true food+bev COGS
                "by_category": defaultdict(float),
                "by_vendor": defaultdict(float),
                "by_cogs_type": defaultdict(float),  # food/beer/wine/liquor/na_beverages
            }
        b = map_[key]
        for li in (inv.get("line_items") or []):
            ext = float(li.get("extended") or 0)
            b["by_category"][li.get("category") or "Uncategorized"] += ext
            bucket = li.get("cogs_bucket")
            if bucket:
                b["by_cogs_type"][bucket] += ext
                b["cogs_only_total"] += ext
        b["total_cogs"] += float(inv.get("total") or 0)
        b["by_vendor"][inv.get("vendor_name") or "Unknown vendor"] += float(inv.get("total") or 0)

    for inv in invoices:
        d = inv.get("date") or ""
        _bucket(by_week, _week_start(d), inv)
        _bucket(by_month, _ym(d), inv)

    def _finalize(map_, key_name, ns_map):
        out = []
        for k, b in map_.items():
            top5 = sorted(b["by_vendor"].items(), key=lambda kv: -kv[1])[:5]
            ns = ns_map.get(k)
            row = {
                key_name: k,
                "total_cogs": round(b["total_cogs"], 2),
                "cogs_only_total": round(b["cogs_only_total"], 2),
                "by_category": {cat: round(v, 2) for cat, v in b["by_category"].items()},
                "by_cogs_type": {t: round(v, 2) for t, v in b["by_cogs_type"].items()},
                "by_vendor_top5": [{"vendor": v, "total": round(t, 2)} for v, t in top5],
            }
            if ns and ns > 0:
                row["cogs_pct_revenue"] = round(b["total_cogs"] / ns, 4)
                row["cogs_only_pct_revenue"] = round(b["cogs_only_total"] / ns, 4)
            out.append(row)
        out.sort(key=lambda r: r[key_name])
        return out

    return {
        "weekly_rollup":  _finalize(by_week,  "week_start", net_sales_by_week),
        "monthly_rollup": _finalize(by_month, "month",      net_sales_by_month),
    }


def merge_invoices(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Append-merge by invoice_id. Fresh wins on collisions."""
    by_id = {(inv.get("invoice_id") or ""): inv for inv in (existing or [])}
    for inv in fresh:
        iid = inv.get("invoice_id") or ""
        if iid:
            by_id[iid] = inv
    return sorted(by_id.values(), key=lambda r: r.get("date") or "")


def net_sales_by_period(payload: dict) -> tuple[dict, dict]:
    """Pull Toast net_sales from existing payload, bucketed by week+month."""
    by_week: dict[str, float] = defaultdict(float)
    by_month: dict[str, float] = defaultdict(float)
    od = payload.get("order_details") or {}
    for rc in od.values():
        for r in (rc.get("daily") or []):
            d = r.get("date") or ""
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").date()
                week = (dt - timedelta(days=dt.weekday())).isoformat()
                month = d[:7]
                by_week[week]  += float(r.get("amount") or r.get("net_sales") or 0)
                by_month[month] += float(r.get("amount") or r.get("net_sales") or 0)
            except (ValueError, TypeError):
                continue
    return by_week, by_month


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

def cmd_probe(api_key: str) -> int:
    print(f"Probing {BASE_URL} ...\n")
    client = MarginEdgeClient(api_key)
    units = client.list_units()
    print(f"  ✓ /restaurantUnits  → {len(units)} restaurants")
    for u in units:
        print(f"      {u.get('id'):>11}  {u.get('name')}")
    return 0


def cmd_discover_units(api_key: str) -> int:
    print("Mapping outlet_id → MarginEdge restaurantUnitId.\n")
    client = MarginEdgeClient(api_key)
    units = client.list_units()
    print(f"{'restaurantUnitId':<18} {'name':<40}")
    print("-" * 60)
    for u in units:
        print(f"{u.get('id'):<18} {u.get('name')}")
    print("\nUpdate OUTLET_TO_ME_UNIT in marginedge_sync.py if any names changed.")
    return 0


def cmd_sync(api_key: str, data_dir: Path, only: str | None,
             lookback_days: int, with_line_items: bool, dry_run: bool) -> int:
    if dry_run:
        print(f"[dry-run] writing fixture to data/_marginedge_dry_run.json")
        fixture = {
            "as_of": date.today().isoformat(), "source": "marginedge_dry_run",
            "lookback_days": lookback_days,
            "invoices": [{"invoice_id": "TEST-1", "date": date.today().isoformat(),
                          "vendor_name": "Test Vendor", "total": 100.0,
                          "line_items": []}],
            "weekly_rollup": [], "monthly_rollup": [],
        }
        (data_dir / "_marginedge_dry_run.json").write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    targets = {oid: uid for oid, uid in OUTLET_TO_ME_UNIT.items() if not only or oid == only}
    if not targets:
        sys.stderr.write(f"no matching outlets (only={only!r}). known: {list(OUTLET_TO_ME_UNIT)}\n")
        return 1

    client = MarginEdgeClient(api_key)
    failures: list[str] = []

    for oid, unit_id in targets.items():
        print(f"\n[{oid}] unit={unit_id} window {start_iso} → {end_iso}")
        try:
            categories = client.get_categories(unit_id)
            vendors = client.get_vendors(unit_id)
            orders = client.get_orders(unit_id, start_iso, end_iso)
            print(f"  fetched: {len(vendors)} vendors, {len(categories)} categories, "
                  f"{len(orders)} orders")

            cat_lookup = {c.get("categoryId"): c.get("categoryName") for c in categories}
            cat_type_lookup = {c.get("categoryId"): c.get("categoryType") for c in categories}
            vendor_lookup = {v.get("vendorId"): v.get("vendorName") for v in vendors}

            # Line item fetch — adds ~1 API call per order (~10 min for
            # all outlets) but unlocks per-category COGS rollup
            # (food / beer / wine / liquor / NA beverages).
            invoices = []
            if with_line_items:
                print(f"  fetching line items for {len(orders)} orders (~{len(orders) * RATE_LIMIT_SLEEP:.0f}s)...")
                for i, o in enumerate(orders):
                    detail = client.get_order_detail(o.get("orderId"), unit_id)
                    invoices.append(transform_order(o, detail.get("lineItems") or [], cat_lookup, cat_type_lookup))
                    if (i + 1) % 50 == 0:
                        print(f"    ...{i+1}/{len(orders)}")
            else:
                invoices = [transform_order(o, None, cat_lookup, cat_type_lookup) for o in orders]

            payload = load_outlet(data_dir, oid)
            existing_cogs = (payload.get("cogs") or {})
            merged_invoices = merge_invoices(existing_cogs.get("invoices") or [], invoices)
            ns_by_week, ns_by_month = net_sales_by_period(payload)
            rollups = build_rollups(merged_invoices, ns_by_week, ns_by_month)

            payload["cogs"] = {
                "as_of": date.today().isoformat(),
                "source": "marginedge_api",
                "lookback_days": lookback_days,
                "with_line_items": with_line_items,
                "invoices": merged_invoices,
                "vendors": [{"id": vid, "name": vendor_lookup[vid]}
                            for vid in vendor_lookup if vid],
                "categories": [{"id": cid, "name": cat_lookup[cid]}
                               for cid in cat_lookup if cid],
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
                    help="auth-only probe (lists restaurantUnits); no writes")
    ap.add_argument("--discover-units", action="store_true",
                    help="print all restaurantUnitIds the API key can see")
    ap.add_argument("--with-line-items", action="store_true",
                    help="fetch line items per order (slower)")
    ap.add_argument("--dry-run", action="store_true",
                    help="write fixture; no network")
    ap.add_argument("--data-dir", default="../data",
                    help="dir of <outlet>.json files (default: ../data)")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                    help=f"days back to pull orders (default: {LOOKBACK_DAYS})")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    api_key = os.environ.get("MARGINEDGE_API_KEY") or os.environ.get("MARGINEDGE_KEYS", "").split("=")[-1]
    if not api_key and not args.dry_run:
        sys.stderr.write("MARGINEDGE_API_KEY missing — exiting cleanly (no-op)\n")
        return 0

    if args.dry_run:
        return cmd_sync("DRY", data_dir, args.outlet, args.lookback,
                        args.with_line_items, dry_run=True)
    if args.probe:
        return cmd_probe(api_key)
    if args.discover_units:
        return cmd_discover_units(api_key)
    return cmd_sync(api_key, data_dir, args.outlet, args.lookback,
                    args.with_line_items or WITH_LINE_ITEMS, dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())
