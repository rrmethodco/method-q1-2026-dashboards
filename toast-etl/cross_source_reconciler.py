"""Cross-source reconciler — Python port for GH Actions.

Twin of supabase/functions/agent-worker/agents/cross_source_reconciler.ts
that runs via GH Actions instead of the Supabase Edge Function. Lets the
reconciliation layer work even when the Edge Function isn't deployed
(Phase A.1 deploy is gated on Slack admin approval; this script is not).

Per outlet, last 30 days:

  1. INTERNAL: order_details.amount / order_details.net_sales ratio.
     Sanity bounds [0.90, 1.50]. Breach → ETL regression flag.

  2. EXTERNAL: order_details.net_sales vs sales_summary.net_sales on
     overlapping dates. Drift threshold ±5%. Breach → source-feed
     mismatch (one of the two is stale or misaligned).

  3. STALE: if sales_summary.daily latest date > 7 days behind today,
     flag the upstream feed as stale.

Outputs a reconciliation report to data/_validation/_reconciliation.json
that the dashboard reads + the morning briefing email pulls from. NO
direct Slack posting (skipped per Ross's note: pending admin approval).

When the Edge Function deploys, this script becomes redundant with the
TS agent — they produce identical audits. Two parallel implementations
is fine for now (defense in depth) and the Edge Function can be deactivated
later if desired.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VALIDATION_DIR = DATA_DIR / "_validation"
OUT_PATH = VALIDATION_DIR / "_reconciliation.json"

OUTLETS = ["lsbr", "mulherins", "kampers", "lowland", "vessel", "anthology",
           "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin"]

# Sanity bounds for amount/net_sales ratio. Healthy LSBR sits ~1.02
# post-PR-#100 (UI-aligned formula); the original bug had it at 1.28.
# A breach above 1.50 means check.amount started including something
# new the formula doesn't subtract. Below 0.90 means net_sales somehow
# exceeds amount — schema regression or sign error.
RATIO_LOWER = 0.90
RATIO_UPPER = 1.50

# Cross-source drift threshold: 5% sustained delta across the 30-day
# lookback between two independently-computed Net Sales values.
CROSS_SOURCE_DRIFT = 0.05

# Minimum coverage before checks are meaningful.
MIN_DAYS = 7
LOOKBACK_DAYS = 30


def _aggregate_order_details(payload: dict, dates: set[str]) -> list[dict]:
    """Sum order_details.daily across all RC keys per date."""
    od = payload.get("order_details") or {}
    by_date: dict[str, dict] = {}
    for _rc, b in od.items():
        for r in b.get("daily", []) or []:
            d = r.get("date") or ""
            if d not in dates:
                continue
            slot = by_date.setdefault(d, {"date": d, "amount": 0.0, "net_sales": 0.0,
                                           "has_net_sales": False})
            slot["amount"]    += float(r.get("amount") or 0)
            ns = r.get("net_sales")
            if ns is not None:
                slot["net_sales"] += float(ns)
                if float(ns) != 0:
                    slot["has_net_sales"] = True
    return sorted(by_date.values(), key=lambda r: r["date"])


def _aggregate_sales_summary(payload: dict, dates: set[str]) -> list[dict]:
    """Sum sales_summary.daily across all RC keys per date."""
    ss = payload.get("sales_summary") or {}
    by_date: dict[str, dict] = {}
    for _rc, b in ss.items():
        for r in b.get("daily", []) or []:
            d = r.get("date") or ""
            if d not in dates:
                continue
            slot = by_date.setdefault(d, {"date": d, "net_sales": 0.0})
            slot["net_sales"] += float(r.get("net_sales") or 0)
    return sorted(by_date.values(), key=lambda r: r["date"])


def reconcile_outlet(outlet: str) -> dict:
    """Run all three checks against one outlet, return a structured result."""
    payload_path = DATA_DIR / f"{outlet}.json"
    if not payload_path.exists():
        return {"outlet": outlet, "checks": [{"kind": "missing_payload",
                "status": "skip", "detail": f"data/{outlet}.json not found"}]}
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    today = date.today()
    dates = {(today - timedelta(days=i)).isoformat() for i in range(1, LOOKBACK_DAYS + 1)}
    od = _aggregate_order_details(payload, dates)
    ss = _aggregate_sales_summary(payload, dates)

    checks: list[dict] = []

    # --- Check 1: internal amount/net_sales ratio ---
    od_with_ns = [r for r in od if r["has_net_sales"]]
    if len(od_with_ns) < MIN_DAYS:
        checks.append({"kind": "internal_ratio", "status": "skip",
                       "detail": f"only {len(od_with_ns)}/{LOOKBACK_DAYS} days with "
                                 f"net_sales populated (need ≥{MIN_DAYS}); "
                                 f"deferred until backfill catches up"})
    else:
        amt = sum(r["amount"] for r in od_with_ns)
        ns  = sum(r["net_sales"] for r in od_with_ns)
        if ns > 0:
            ratio = amt / ns
            band = (RATIO_LOWER, RATIO_UPPER)
            in_band = RATIO_LOWER <= ratio <= RATIO_UPPER
            checks.append({
                "kind": "internal_ratio",
                "status": "ok" if in_band else "alert",
                "ratio": round(ratio, 4),
                "amount_30d": round(amt, 2),
                "net_sales_30d": round(ns, 2),
                "days_compared": len(od_with_ns),
                "band": band,
                "detail": (f"amount/net_sales = {ratio:.3f} "
                           f"(${amt:,.0f}/${ns:,.0f} over {len(od_with_ns)}d)"
                           + ("" if in_band
                              else " — OUTSIDE [0.90, 1.50] sanity band")),
            })
        else:
            checks.append({"kind": "internal_ratio", "status": "alert",
                           "detail": f"net_sales sum is {ns:.2f} — "
                                     "either zero revenue or schema regression"})

    # --- Check 2: external order_details vs sales_summary ---
    if not ss:
        checks.append({"kind": "external_drift", "status": "skip",
                       "detail": "no sales_summary baseline available "
                                 "(awaiting Toast Sales Summary auto-pull)"})
    else:
        od_map = {r["date"]: r for r in od}
        overlap = []
        for r in ss:
            o = od_map.get(r["date"])
            if not o or not o["has_net_sales"]:
                continue
            overlap.append({"date": r["date"], "od": o["net_sales"], "ss": r["net_sales"]})
        if len(overlap) < MIN_DAYS:
            checks.append({"kind": "external_drift", "status": "skip",
                           "detail": f"only {len(overlap)} overlapping days with both "
                                     f"sources (need ≥{MIN_DAYS})"})
        else:
            tot_od = sum(r["od"] for r in overlap)
            tot_ss = sum(r["ss"] for r in overlap)
            if tot_ss > 0:
                drift = (tot_od - tot_ss) / tot_ss
                ok = abs(drift) <= CROSS_SOURCE_DRIFT
                checks.append({
                    "kind": "external_drift",
                    "status": "ok" if ok else "alert",
                    "drift_pct": round(drift * 100, 4),
                    "od_total": round(tot_od, 2),
                    "ss_total": round(tot_ss, 2),
                    "overlap_days": len(overlap),
                    "threshold_pct": CROSS_SOURCE_DRIFT * 100,
                    "detail": (f"order_details ${tot_od:,.0f} vs "
                               f"sales_summary ${tot_ss:,.0f} = {drift*100:+.2f}% drift"
                               + ("" if ok
                                  else f" — exceeds ±{CROSS_SOURCE_DRIFT*100:.0f}%")),
                })
            else:
                checks.append({"kind": "external_drift", "status": "alert",
                               "detail": "sales_summary baseline is zero — import regression"})

        # --- Check 3: stale baseline ---
        ss_latest = max((r["date"] for r in ss), default=None)
        if ss_latest:
            age = (today - date.fromisoformat(ss_latest)).days
            ok = age <= 7
            checks.append({
                "kind": "stale_baseline",
                "status": "ok" if ok else "alert",
                "ss_latest_date": ss_latest,
                "age_days": age,
                "detail": (f"sales_summary latest = {ss_latest} ({age}d ago)"
                           + ("" if ok else f" — exceeds 7d freshness threshold")),
            })

    return {"outlet": outlet, "checks": checks}


def main() -> int:
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {
        "ran_at": ran_at,
        "lookback_days": LOOKBACK_DAYS,
        "sanity_bounds": {"ratio_lower": RATIO_LOWER, "ratio_upper": RATIO_UPPER,
                          "drift_threshold": CROSS_SOURCE_DRIFT},
        "outlets": [],
    }
    n_alerts = 0
    n_ok = 0
    for outlet in OUTLETS:
        result = reconcile_outlet(outlet)
        for c in result["checks"]:
            if c["status"] == "alert":
                n_alerts += 1
            elif c["status"] == "ok":
                n_ok += 1
        report["outlets"].append(result)
    report["summary"] = {"checks_ok": n_ok, "checks_alert": n_alerts,
                         "outlets_reconciled": len(OUTLETS)}

    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.stdout.write(f"wrote {OUT_PATH} — {n_ok} OK, {n_alerts} alerts across "
                     f"{len(OUTLETS)} outlets\n")

    # Print per-outlet summary to stdout for the workflow log
    for r in report["outlets"]:
        outlet = r["outlet"]
        for c in r["checks"]:
            status_glyph = {"ok": "OK", "alert": "ALERT", "skip": "skip"}[c["status"]]
            sys.stdout.write(f"  [{outlet:<14}] {c['kind']:<18} {status_glyph:<5} "
                             f"{c.get('detail', '')}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
