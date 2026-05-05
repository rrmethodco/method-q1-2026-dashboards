"""Lock in the Net Sales computation in transform_orders().

Background
----------
Pre-2026-05-05, the dashboard's "Net Sales" KPI sourced from
`check.amount` on Toast's Orders API. Empirically that field runs
+20–30% above Toast's official "Net sales" line on the Sales Summary
report (it includes tips, non-gratuity service charges, and other items
the report excludes). Ross caught the gap when LSBR week ending 5/3
showed $143.3K on the dashboard vs. $125,673.04 in Toast direct.

The fix added a parallel `net_sales` field that walks
`check.selections[]` and matches Toast's Net Sales definition exactly:

    net_sales =
      Σ over non-voided non-deleted non-deferred selections of
        (selection.preDiscountPrice − Σ selection.appliedDiscounts)
      − Σ check.appliedDiscounts

These tests pin that math down so the next time someone refactors
transform_orders, the Net Sales KPI doesn't silently drift.

See: docs/NET_SALES_RECONCILIATION.md
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `import toast_sync` without installing as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toast_sync import transform_orders  # noqa: E402


def _make_order(
    date: str,
    *,
    check_amount: float,
    tip: float = 0.0,
    service_charges: list[dict] | None = None,
    check_discounts: list[dict] | None = None,
    selections: list[dict] | None = None,
    guests: int = 2,
):
    """Build the minimal raw-order shape transform_orders consumes.

    Toast emits paidDate as UTC ISO with a Z suffix; transform_orders
    converts it to America/New_York for the daily bucket.
    """
    return {
        "voided": False,
        "deleted": False,
        "closedDate": f"{date}T20:00:00.000Z",
        "numberOfGuests": guests,
        "checks": [
            {
                "voided": False,
                "deleted": False,
                "paidDate": f"{date}T22:30:00.000Z",  # 18:30 ET — clean dinner-rush bucket
                "openedDate": f"{date}T21:00:00.000Z",
                "amount": check_amount,
                "tipAmount": tip,
                "appliedServiceCharges": service_charges or [],
                "appliedDiscounts": check_discounts or [],
                "selections": selections or [],
            }
        ],
    }


def _selection(
    *,
    pre_discount: float,
    price: float | None = None,
    voided: bool = False,
    deferred: bool = False,
    deleted: bool = False,
    selection_discount: float = 0.0,
):
    return {
        "voided": voided,
        "deleted": deleted,
        "deferred": deferred,
        "preDiscountPrice": pre_discount,
        "price": price if price is not None else pre_discount,
        "appliedDiscounts": (
            [{"discountAmount": selection_discount}] if selection_discount else []
        ),
    }


def _net_sales_for_day(out: dict, date: str) -> float:
    matches = [r for r in out["daily"] if r["date"] == date]
    assert len(matches) == 1, f"expected exactly 1 row for {date}, got {len(matches)}"
    return matches[0]["net_sales"]


def test_clean_check_no_adjustments():
    """$100 selection, no discount, no tip, no SC → net_sales = 100."""
    orders = [_make_order(
        "2026-05-01",
        check_amount=100.0,
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-01") == 100.0


def test_selection_level_discount_subtracts():
    """$100 item with $10 selection-discount → net_sales = 90."""
    orders = [_make_order(
        "2026-05-02",
        check_amount=90.0,
        selections=[_selection(pre_discount=100.0, price=90.0, selection_discount=10.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-02") == 90.0


def test_check_level_discount_subtracts():
    """$100 item, no item-level discount, $10 check-level discount → net_sales = 90."""
    orders = [_make_order(
        "2026-05-03",
        check_amount=90.0,
        check_discounts=[{"discountAmount": 10.0}],
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-03") == 90.0


def test_voided_selection_excluded():
    """Voided $30 selection alongside live $50 selection → net_sales = 50."""
    orders = [_make_order(
        "2026-05-04",
        check_amount=50.0,
        selections=[
            _selection(pre_discount=50.0),
            _selection(pre_discount=30.0, voided=True),
        ],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-04") == 50.0


def test_deferred_selection_excluded():
    """Gift card sale ($100, deferred) excluded; live $50 item kept → net_sales = 50."""
    orders = [_make_order(
        "2026-05-05",
        check_amount=150.0,
        selections=[
            _selection(pre_discount=50.0),
            _selection(pre_discount=100.0, deferred=True),
        ],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-05") == 50.0


def test_le_supreme_4top_with_auto_grat():
    """Realistic Le Suprême large-party check.

    4 entrees @ $50 + $80 wine bottle = $280 subtotal.
    Auto-grat 20% as service charge = $56.
    Toast's check.amount in some configs rolls SC in → $336.
    Toast Net Sales must be $280 (excludes SC).

    This test pins the inflation gap shut: amount=336, net_sales=280 →
    20% delta exactly matches the empirical pattern that motivated the
    fix in the first place.
    """
    orders = [_make_order(
        "2026-05-06",
        check_amount=336.0,
        service_charges=[{"amount": 56.0, "gratuity": True}],
        selections=[
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=80.0),
        ],
        guests=4,
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-06") == 280.0
    # Sanity: legacy `amount` field still tracks the inflated check total.
    row = [r for r in out["daily"] if r["date"] == "2026-05-06"][0]
    assert row["amount"] == 336.0
    assert row["gratuity"] == 56.0


def test_totals_dict_exposes_net_sales():
    """totals must include net_sales so downstream consumers don't fall back to 0.

    `_recompute_rc_totals` (used by merge_payloads) uses .get(k) or 0,
    so a missing key would silently zero out the rollup. Pin the key
    so a future refactor doesn't drop it.
    """
    orders = [_make_order(
        "2026-05-07",
        check_amount=100.0,
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    assert "net_sales" in out["totals"], (
        "totals dict must expose net_sales — _recompute_rc_totals depends on it"
    )
    assert out["totals"]["net_sales"] == 100.0
