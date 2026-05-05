# Toast Net Sales Reconciliation — 2026-05-05

## What this is

The dashboard's Net Sales KPI was inflated +20–30% over Toast's official
"Net sales" figure on the Sales Summary report. Ross caught it on
2026-05-05 when Le Suprême + Bar Rotunda showed $143.3K for week ending
5/3 vs. Toast direct of $125,673.04 (+$17,627 / 14% overage).

This doc explains the cause, the fix, and the one-time backfill needed
to surface accurate Net Sales for periods after 2026-04-21 (the last
date covered by the manual Sales Summary CSV import).

## Root cause

`toast-etl/toast_sync.py:transform_orders()` was sourcing per-day Net
Sales from `check.amount` on Toast's Orders API
(`/orders/v2/ordersBulk`). That field is empirically not Net Sales — it
includes tips, non-gratuity service charges (e.g. 18–20% large-party
auto-grat configured as a service charge), and other items Toast's
Sales Summary report excludes.

Audit on lifetime LSBR data (2024-11 → 2026-04):

|                                        | $              | % of NS |
| -------------------------------------- | -------------- | ------- |
| Net sales (Sales Summary)              | $18,825,379.78 | —       |
| Tips                                   | $3,558,915.00  | 18.90%  |
| Gratuity                               | $210,174.98    | 1.12%   |
| **Tips + Gratuity**                    | **$3,769,090** | **20.02%** |
| Observed dashboard inflation (90-day)  | +25.6% (range +17%–+49% by DOW) | |

The 20% Tips+Grat ratio matches the inflation floor; remaining 5–10%
appears to be non-gratuity service charges and possibly voided-but-not-
stripped selections.

## The fix (in code)

### ETL — `toast-etl/toast_sync.py:transform_orders()`

Added a parallel `net_sales` field that walks `check.selections[]`:

```
net_sales =
  Σ over non-voided non-deleted non-deferred selections of
    (selection.preDiscountPrice − Σ selection.appliedDiscounts.discountAmount)
  − Σ check.appliedDiscounts.discountAmount
```

This matches Toast's Sales Summary "Net sales" definition exactly. The
legacy `check.amount` is preserved as `amount` for back-compat (it's
the gross "tickets value" on the check; useful for per-server attribution
and tip analysis).

Validated against 6 synthetic test cases (clean, discount, voids,
deferred gift cards, check-level discounts, large-party auto-grat).

### Dashboard — `combinedDailySales()`

Priority order (highest → lowest):

1. `order_details.daily[].net_sales` — Toast-aligned Net Sales (post-fix)
2. `sales_summary.daily[].net_sales` — manual CSV import (last LSBR
   refresh 2026-04-21)
3. `order_details.daily[].amount` — legacy fallback, flagged
   `_estimated:true` so the dashboard surfaces a yellow reconciliation
   banner on the Weekly Snapshot

### Dashboard — reconciliation banner

When the displayed week contains rows whose Net Sales fell back to the
legacy estimate, a yellow "NET SALES RECONCILIATION" strip renders
between the snap-head and the KPI grid, showing how many days are
estimated vs. confirmed.

## Backfill procedure (one-time, ~25 min)

The new `net_sales` field only populates as raw orders flow through
`transform_orders`. Pre-2026-05-05 daily rows in `data/<outlet>.json`
still lack the field. To populate it for the gap (4/22 → today), run a
backfill against all outlets:

```bash
# From repo root
cd toast-etl

# Set credentials (typically already in your shell env via direnv/.env)
export TOAST_CLIENT_ID=...
export TOAST_CLIENT_SECRET=...
export TOAST_OUTLETS='{...}'  # see existing pipeline config

# Backfill 14 days (covers 4/22 → today as of 2026-05-05)
# This will fetch raw orders from Toast, re-run transform_orders with
# the new net_sales math, and merge into the existing per-outlet JSON.
python3 toast_sync.py --days 14 --outdir ../data
```

What `merge_payloads` does (see `toast_sync.py:1458`):

* Daily rows with `date >= cutoff (= start_local)` are replaced with the
  freshly transformed rows (which now include `net_sales`).
* Daily rows older than the cutoff are preserved verbatim — they keep
  their legacy `amount` but lack `net_sales`. Dashboard handles this
  via `_estimated:true` flag on those rows AND falls through to
  `sales_summary.net_sales` where present.
* `totals` and `hour_dow` aggregates are recomputed from the merged
  daily, so they reflect the corrected `net_sales` going forward.

After the backfill commits the updated `data/*.json` files, the dashboard
auto-corrects every Net Sales KPI for 4/22 onward without any further
intervention. The reconciliation banner disappears for those weeks.

## Going forward — nightly sync

The same `--days 7` (or whatever the existing GH Actions cadence is)
nightly run picks up the new `net_sales` math automatically. No further
config or secret changes needed.

## What's still pending

1. **Cross-source reconciler agent** (Phase A.1+) — fires a Slack alert
   when `order_details.amount / order_details.net_sales > 1.10` (signals
   ETL drift, e.g. a new selection-level field Toast adds) OR when
   `order_details.net_sales / sales_summary.net_sales` drifts > 1.05
   (signals one of the two sources got stale or misaligned). Spec'd in
   `docs/superpowers/specs/2026-05-04-trustworthy-reporting-engine-design.md`
   under future hardening.

2. **`sales_summary` daily auto-pull** — eliminates the manual CSV
   refresh dependency entirely. Hits Toast's Reports API or the analogous
   `/reports/v1/sales` endpoint to pull the canonical Net sales line
   nightly. Even with the new selection-level math, an independent
   sales-summary baseline is a cheap reconciliation safety net.

## Verification

After the backfill, sanity-check LSBR week ending 5/3:

```bash
python3 -c "
import json
from datetime import date
with open('data/lsbr.json') as f:
    d = json.load(f)
total = 0
for rc, b in d['order_details'].items():
    for r in b.get('daily', []):
        if '2026-04-27' <= r['date'] <= '2026-05-03':
            total += r.get('net_sales') or 0
print(f'LSBR Net Sales 4/27-5/3: \${total:,.2f}  (Toast direct: \$125,673.04)')
"
```

Expected: within $1–2 of $125,673.04 (Toast rounds at the report layer
but item-level math should match within rounding).
