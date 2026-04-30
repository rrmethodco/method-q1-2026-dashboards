#!/usr/bin/env python3
"""
Method Co — Forecast engine (port of helixo-2 forecast.service.ts).

Generates a per-day {date, net_sales, guests, orders} forecast for each
outlet and writes it under `forecast.daily` in data/<outlet>.json. The
dashboard's tri-comparison KPI cards (vs Forecast / vs STLY / vs Budget)
read this block — when the data is missing, the cards render
"Forecast — not wired" muted text.

Faithful to helixo-2's weighted-ensemble approach:
  base = mean(similar past days)
  forecasted_covers   = base × dayFactor × seasonFactor × weatherFactor
  forecasted_revenue  = forecasted_covers × avg_check

We keep:
  - Day-of-week multipliers (Mon 0.65 ... Sat 1.10)
  - Seasonal month multipliers (Jan 0.85 ... Dec 1.20)
  - Similar-day pattern matching (DOW + month proximity + recency)

We drop:
  - Weather signal (no weather feed wired in dashboards repo yet)
  - Resy reservation signal (helixo-2 disabled it 2026-04-07 due to
    broken ingestion — same scraper feeds both repos)
  - Hourly breakdown + staffing requirements (the dashboard surfaces
    daily roll-ups; staffing belongs in helixo-2 not here)

The forecast covers BOTH history and future:
  - For each date in [first_history → today + LOOKAHEAD_DAYS], compute
    the forecast value. Historical values give the dashboard meaningful
    "vs Forecast" deltas in past periods (otherwise every prior week
    would show "— not wired" instead of an actionable +/- %).

Usage:
  python3 forecast_engine.py                # all outlets (data/*.json)
  python3 forecast_engine.py --outlet lsbr  # single outlet
  python3 forecast_engine.py --dry-run      # print summaries, no writes
  python3 forecast_engine.py --lookahead 90 # forward window (default 120)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean


# ---------- helixo-2-aligned multipliers ----------

# Day-of-week multipliers, indexed Sunday=0..Saturday=6.
# Verbatim from helixo-2/forecast.service.ts dayOfWeekMultipliers:
#   Sun 0.75 / Mon 0.65 / Tue 0.70 / Wed 0.75 / Thu 0.85 / Fri 1.15 / Sat 1.10
DAY_OF_WEEK_MULTIPLIER = {
    0: 0.75,  # Sunday
    1: 0.65,  # Monday
    2: 0.70,  # Tuesday
    3: 0.75,  # Wednesday
    4: 0.85,  # Thursday
    5: 1.15,  # Friday
    6: 1.10,  # Saturday
}

# Month multipliers, indexed Jan=0..Dec=11. Verbatim from helixo-2.
SEASONAL_MULTIPLIER = {
    0: 0.85, 1: 0.80, 2: 0.90,    # Jan-Mar (slow)
    3: 1.00, 4: 1.05, 5: 1.15,    # Apr-Jun (picking up)
    6: 1.10, 7: 1.05, 8: 1.00,    # Jul-Sep (summer)
    9: 1.05, 10: 1.10, 11: 1.20,  # Oct-Dec (holiday season)
}


# ---------- daily-row plumbing ----------

@dataclass
class DailyRow:
    """A historical revenue day — outlet-level (RC-summed)."""
    date: date
    net_sales: float
    guests: int
    orders: int


def parse_iso_date(s: str) -> date | None:
    if not s or len(s) < 10:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def collect_outlet_history(payload: dict) -> list[DailyRow]:
    """Sum every revenue center's daily rows into outlet-level rows.

    Mirrors the dashboard's combinedDailySales(): outlet-wide history
    aggregated across rc keys. order_details is preferred (richer
    fields); falls back to sales_summary if needed."""
    by_date: dict[date, dict] = defaultdict(lambda: {"net_sales": 0.0, "guests": 0, "orders": 0})

    od = payload.get("order_details") or {}
    for rc_key, rc_body in od.items():
        if not isinstance(rc_body, dict):
            continue
        for row in (rc_body.get("daily") or []):
            d = parse_iso_date(row.get("date"))
            if not d:
                continue
            cell = by_date[d]
            cell["net_sales"] += row.get("amount") or row.get("net_sales") or 0
            cell["guests"]    += int(row.get("guests") or 0)
            cell["orders"]    += int(row.get("orders") or 0)

    if not by_date:
        # Fallback to sales_summary
        ss = payload.get("sales_summary") or {}
        for rc_key, rc_body in ss.items():
            if not isinstance(rc_body, dict):
                continue
            for row in (rc_body.get("daily") or []):
                d = parse_iso_date(row.get("date"))
                if not d:
                    continue
                cell = by_date[d]
                cell["net_sales"] += row.get("net_sales") or 0
                cell["guests"]    += int(row.get("guests") or 0)
                cell["orders"]    += int(row.get("orders") or 0)

    return sorted(
        (DailyRow(d, c["net_sales"], c["guests"], c["orders"]) for d, c in by_date.items()),
        key=lambda r: r.date,
    )


# ---------- similar-day pattern matching (helixo-2 findSimilarDays) ----------

def similarity_score(snap_date: date, target_dow: int, target_month: int, today: date) -> float:
    """Helixo-2's similarity scoring:
       - 0.5 for same DOW, 0.2 for adjacent DOW
       - 0.3 × (1 − monthDiff/3) for seasonal proximity
       - 0.2 × (1 − ageDays/365) for recency
    """
    sim = 0.0
    snap_dow = (snap_date.weekday() + 1) % 7  # JS-style Sun=0..Sat=6
    if snap_dow == target_dow:
        sim += 0.5
    elif abs(snap_dow - target_dow) == 1:
        sim += 0.2

    # Seasonal proximity (months close together, wrapping at year-end)
    snap_month = snap_date.month - 1
    month_diff = min(abs(snap_month - target_month), 12 - abs(snap_month - target_month))
    sim += 0.3 * max(0, 1 - month_diff / 3)

    # Recency bonus
    age_days = (today - snap_date).days
    sim += 0.2 * max(0, 1 - age_days / 365)

    return sim


def find_similar_days(
    history: list[DailyRow],
    target_date: date,
    today: date,
    min_similarity: float = 0.3,
    top_n: int = 10,
) -> list[DailyRow]:
    """Score each historical day against the target and return the top N."""
    target_dow = (target_date.weekday() + 1) % 7  # JS Sun=0 convention
    target_month = target_date.month - 1

    scored: list[tuple[float, DailyRow]] = []
    for row in history:
        # Don't use the target date itself as a "similar day" — that
        # would make historical-period forecasts trivially equal to actuals.
        if row.date == target_date:
            continue
        # Skip zero-revenue days (closed days, data gaps) — they pull
        # the forecast toward zero.
        if row.net_sales <= 0:
            continue
        sim = similarity_score(row.date, target_dow, target_month, today)
        if sim > min_similarity:
            scored.append((sim, row))

    scored.sort(key=lambda kv: -kv[0])
    return [row for _, row in scored[:top_n]]


# ---------- forecast generation ----------

def generate_daily_forecast(
    target_date: date,
    history: list[DailyRow],
    today: date,
) -> dict | None:
    """Generate a single day's forecast row.

    Returns {date, net_sales, guests, orders} or None if there isn't
    enough history to produce a meaningful prediction.

    Faithful to helixo-2's:
       baseCovers = historicalAvgCovers × dayFactor × seasonFactor × weatherFactor
       avgCheck   = historicalAvgRevenue / historicalAvgCovers (or $35 fallback)
       forecastedRevenue = forecastedCovers × avgCheckSize
    """
    similar = find_similar_days(history, target_date, today)
    if not similar:
        return None

    avg_covers  = mean(r.guests    for r in similar) if any(r.guests for r in similar) else 0
    avg_revenue = mean(r.net_sales for r in similar)
    # Helixo-2 uses an order count proxy; we have orders directly.
    avg_orders = mean(r.orders for r in similar) if any(r.orders for r in similar) else 0

    target_dow = (target_date.weekday() + 1) % 7
    day_factor    = DAY_OF_WEEK_MULTIPLIER.get(target_dow, 1.0)
    season_factor = SEASONAL_MULTIPLIER.get(target_date.month - 1, 1.0)
    weather_factor = 1.0  # Weather feed not wired in dashboards repo

    multiplier = day_factor * season_factor * weather_factor

    # NOTE: helixo-2 multiplies the mean of similar days by these factors,
    # which slightly double-counts DOW/seasonal patterns (the similar
    # days were already filtered by DOW + month). We mirror that
    # behavior verbatim — when helixo-2 retunes their weights, we follow.
    forecasted_covers  = round(avg_covers  * multiplier) if avg_covers  else 0
    forecasted_revenue = round(avg_revenue * multiplier, 2)
    forecasted_orders  = round(avg_orders  * multiplier) if avg_orders  else 0

    return {
        "date":      target_date.isoformat(),
        "net_sales": forecasted_revenue,
        "guests":    forecasted_covers,
        "orders":    forecasted_orders,
        # Tracking-grade fields — not consumed by the dashboard yet, but
        # stored here so the next iteration can surface confidence + the
        # actual similar-day pool that drove this prediction.
        "_meta": {
            "similar_days": len(similar),
            "day_factor":    round(day_factor, 3),
            "season_factor": round(season_factor, 3),
            "model":         "weighted_ensemble" if len(similar) >= 5 else "historical_avg",
        },
    }


def forecast_outlet(payload: dict, lookahead_days: int = 120) -> dict:
    """Generate the full {forecast: {as_of, source, daily: [...]}} block."""
    today = date.today()
    history = collect_outlet_history(payload)
    if not history:
        return {
            "as_of":  today.isoformat(),
            "source": "forecast_engine_v1",
            "daily":  [],
            "_note":  "no historical revenue data — cannot forecast",
        }

    # Forecast EVERY historical date (lets the dashboard show vs-Forecast
    # deltas for any past period the operator filters to) plus
    # `lookahead_days` of forward dates.
    start = history[0].date
    end   = today + timedelta(days=lookahead_days)

    rows: list[dict] = []
    cur = start
    while cur <= end:
        row = generate_daily_forecast(cur, history, today)
        if row is not None:
            rows.append(row)
        cur += timedelta(days=1)

    return {
        "as_of":  today.isoformat(),
        "source": "forecast_engine_v1 (helixo-2 port)",
        "daily":  rows,
        "_note":  f"history range {history[0].date.isoformat()} → {history[-1].date.isoformat()}; "
                  f"forward window {lookahead_days}d",
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


# ---------- driver ----------

def discover_outlets(data_dir: Path) -> list[str]:
    return sorted(
        p.stem for p in data_dir.glob("*.json")
        if not p.stem.startswith("_")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--data-dir",  default="../data", help="dir of <outlet>.json")
    ap.add_argument("--outlet",    default="",       help="single outlet id (default: all)")
    ap.add_argument("--lookahead", type=int, default=120, help="forward window in days (default 120)")
    ap.add_argument("--dry-run",   action="store_true", help="print summaries; no writes")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        sys.stderr.write(f"data dir not found: {data_dir}\n")
        return 1

    outlets = [args.outlet] if args.outlet else discover_outlets(data_dir)
    if not outlets:
        sys.stderr.write("no outlets to forecast\n")
        return 1

    print(f"Forecasting {len(outlets)} outlet(s) — lookahead {args.lookahead}d")
    for oid in outlets:
        payload = load_outlet(data_dir, oid)
        block = forecast_outlet(payload, lookahead_days=args.lookahead)
        rows = block.get("daily") or []
        future = [r for r in rows if r["date"] > date.today().isoformat()]
        past   = [r for r in rows if r["date"] <= date.today().isoformat()]
        rev_30 = sum(r["net_sales"] for r in future[:30])
        print(f"  {oid:<14} history+forecast rows={len(rows):>5} (past={len(past)}, future={len(future)}) "
              f"next-30d ${rev_30:,.0f}")
        if not args.dry_run:
            payload["forecast"] = block
            write_outlet(data_dir, oid, payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
