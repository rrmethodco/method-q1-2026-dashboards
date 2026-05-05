"""Lock in the Net Sales computation in transform_orders().

Background
----------
Pre-2026-05-05, the dashboard's "Net Sales" KPI sourced from
`check.amount` on Toast's Orders API. Empirically that field runs
+20–30% above Toast's official "Net sales" line on the Sales Summary
report (it includes tips, non-gratuity service charges, and other items
the report excludes). Ross caught the gap when LSBR week ending 5/3
showed $143.3K on the dashboard vs. $125,673.04 in Toast direct.

The current `net_sales` formula in transform_orders matches Toast Web's
**Sales Summary "Net Sales" column** — what operators see when querying
the dashboard for P&L reconciliation. This DIVERGES from Toast's API spec
at apiOrdersNetSalesCalculation.html (which adds non-gratuity SCs and
uses nonTaxableDiscountAmount). The reason: empirical reconciliation
showed the API formula sits +4% above Toast UI; the UI formula gets us
to +3% (with the residual coming from business-day-rollover boundary).

  1. For each non-voided non-Gift-Card selection:
       sel_net = preDiscountPrice
                − sum(appliedDiscounts.discountAmount)
                − refundDetails.refundAmount
  2. Sum sel_net across selections = check_net
  3. check_net −= sum(check.appliedDiscounts.discountAmount)
  4. Sum check_net across the order's checks.

NO non-gratuity service charges added — Toast UI Sales Summary excludes
them (they appear under a separate "Service Charges" line in P&L).

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
    display_name: str | None = None,
    selection_discount: float = 0.0,
    selection_nontax_discount: float | None = None,
    refund_amount: float = 0.0,
):
    """Build a selection. Test exercises:
      - selection_discount: emits {discountAmount: X} only (legacy POS shape).
      - selection_nontax_discount: emits {nonTaxableDiscountAmount: X,
          discountAmount: X+1} so we can confirm the new field is preferred.
      - refund_amount: emits refundDetails.refundAmount (Toast POS 2.46+).
      - display_name: e.g. "Gift Card" to test displayName-based exclusion.
    """
    sel: dict = {
        "voided": voided,
        "deleted": deleted,
        "deferred": deferred,
        "preDiscountPrice": pre_discount,
        "price": price if price is not None else pre_discount,
        "appliedDiscounts": [],
    }
    if display_name is not None:
        sel["displayName"] = display_name
    if selection_nontax_discount is not None:
        # Emit BOTH discountAmount and nonTaxableDiscountAmount differently to
        # confirm the formula prefers nonTaxableDiscountAmount.
        sel["appliedDiscounts"] = [{
            "nonTaxableDiscountAmount": selection_nontax_discount,
            "discountAmount": selection_nontax_discount + 1.0,  # decoy
        }]
    elif selection_discount:
        sel["appliedDiscounts"] = [{"discountAmount": selection_discount}]
    if refund_amount:
        sel["refundDetails"] = {"refundAmount": refund_amount}
    return sel


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


# =============================================================================
# Tests for the EXACT Toast 5-step formula (PR #99 — penny-accurate reconciliation)
# https://doc.toasttab.com/doc/devguide/apiOrdersNetSalesCalculation.html
# =============================================================================


def test_full_discount_amount_subtracted_per_toast_ui():
    """Toast UI Sales Summary subtracts FULL `discountAmount` (taxable +
    non-taxable parts). The API spec says nonTaxableDiscountAmount only,
    but empirical reconciliation against Toast UI showed the full
    discountAmount matches (LSBR audit 2026-05-05).

    Helper emits BOTH fields with `discountAmount = nonTaxable + 1` as a
    decoy in the OPPOSITE direction. The correct (UI-aligned) formula
    picks `discountAmount = $11`, yielding net_sales = 100 - 11 = $89.
    """
    orders = [_make_order(
        "2026-05-08",
        check_amount=89.0,
        selections=[_selection(
            pre_discount=100.0,
            selection_nontax_discount=10.0,  # makes discountAmount=11.0
        )],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-08") == 89.0, (
        "Toast UI subtracts full discountAmount=11 (giving 89), "
        "not nonTaxableDiscountAmount=10 (giving 90)"
    )


def test_non_gratuity_service_charge_NOT_added_to_net_sales_per_toast_ui():
    """Toast UI Sales Summary "Net Sales" column EXCLUDES non-gratuity
    service charges (they appear under a separate "Service Charges" line
    in the P&L breakdown). API spec says to include them; empirical
    reconciliation showed Ross's $125,673 Toast UI number EXCLUDES them.

    $100 item + $5 non-gratuity SC + $20 auto-grat:
      Net Sales = $100 (both SCs excluded from this column).
    """
    orders = [_make_order(
        "2026-05-09",
        check_amount=125.0,
        service_charges=[
            {"chargeAmount": 5.0, "gratuity": False, "name": "Delivery Fee"},
            {"chargeAmount": 20.0, "gratuity": True, "name": "Auto-gratuity 20%"},
        ],
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-09") == 100.0


def test_service_charges_tracked_separately_in_gratuity_field():
    """SCs aren't added to net_sales but are still aggregated into the
    `gratuity` field (sum of all appliedServiceCharges.amount) for use in
    the Service Charges line of the dashboard's P&L breakdown.

    Note: the existing `gratuity` field collector reads `sc.amount`, not
    `sc.chargeAmount`. Test mirrors that field name.
    """
    orders = [_make_order(
        "2026-05-10",
        check_amount=125.0,
        service_charges=[
            {"amount": 5.0, "gratuity": False},
            {"amount": 20.0, "gratuity": True},
        ],
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    row = [r for r in out["daily"] if r["date"] == "2026-05-10"][0]
    # Total of all SCs (gratuity + non-gratuity) tracked in gratuity field for
    # the dashboard's Service Charges P&L line. Net Sales unaffected.
    assert row["net_sales"] == 100.0
    assert row["gratuity"] == 25.0  # sum of both SCs (5 + 20)


def test_gift_card_excluded_by_displayName():
    """Toast formula step 1: 'Exclude menu item selections where
    displayName is "Gift Card"'. Older Toast schemas may use the
    `deferred=true` flag instead. Both should result in exclusion.
    """
    orders = [_make_order(
        "2026-05-11",
        check_amount=200.0,
        selections=[
            _selection(pre_discount=50.0),  # included
            _selection(pre_discount=100.0, display_name="Gift Card"),  # excluded by displayName
            _selection(pre_discount=50.0, deferred=True),  # excluded by deferred (defensive)
        ],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-11") == 50.0


def test_selection_refund_subtracts_from_net_sales():
    """Toast POS 2.46+ surfaces refunds via `selection.refundDetails.refundAmount`.
    A $50 item with a $20 partial refund on the same day: net_sales = $30.
    """
    orders = [_make_order(
        "2026-05-12",
        check_amount=50.0,
        selections=[_selection(pre_discount=50.0, refund_amount=20.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-12") == 30.0


def test_service_charge_refund_does_not_affect_net_sales_per_ui():
    """Toast UI Net Sales doesn't include SCs, so SC refunds don't affect
    it either (they're tracked under the separate Service Charges P&L
    line). $100 item + $10 SC + $5 SC refund → Net Sales = $100.
    """
    orders = [_make_order(
        "2026-05-13",
        check_amount=110.0,
        service_charges=[{
            "chargeAmount": 10.0, "gratuity": False,
            "refundDetails": {"refundAmount": 5.0},
        }],
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-13") == 100.0


def test_combo_discount_handled_via_check_appliedDiscounts():
    """Combo discounts: the discount is registered at check level with
    `discountAmount` set; selection.preDiscountPrice stays at the
    original price. Net Sales subtracts the check-level discount.

    Two soups @ $8.99 each (preDiscountPrice = $17.98 total),
    combo discount = $2.98 → check.appliedDiscounts has one combo entry
    with discountAmount = $2.98. Net Sales = 17.98 - 2.98 = $15.
    """
    orders = [_make_order(
        "2026-05-14",
        check_amount=15.0,
        check_discounts=[{
            "menuItemSelectionGuid": "s2",
            "discountGuid": "combo",
            "discountAmount": 2.98,
        }],
        selections=[
            {"voided": False, "deleted": False, "preDiscountPrice": 8.99,
             "price": 8.99, "appliedDiscounts": []},
            {"voided": False, "deleted": False, "preDiscountPrice": 8.99,
             "price": 8.99, "appliedDiscounts": []},
        ],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-14") == round(17.98 - 2.98, 2)


def test_le_supreme_realistic_large_party_check():
    """Realistic Le Suprême large-party check with auto-grat + non-grat SC
    + partial refund. Per Toast UI semantics:
      - Item refunds DO reduce Net Sales (item-level)
      - SC refunds and SC additions both stay OUT of Net Sales (separate line)

    5 entrees @ $50 = $250.
    Wine bottle @ $80 (with $5 partial refund) = $75.
    Auto-grat 20% as SC = $66 (excluded — SC).
    Wellness fee = $9.90 (excluded — SC).

      Net Sales = $250 + $75 = $325
    """
    orders = [_make_order(
        "2026-05-15",
        check_amount=400.0,
        service_charges=[
            {"chargeAmount": 66.0, "gratuity": True, "name": "Auto-gratuity"},
            {"chargeAmount": 9.90, "gratuity": False, "name": "Wellness Fee",
             "refundDetails": {"refundAmount": 1.0}},
        ],
        selections=[
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=50.0),
            _selection(pre_discount=80.0, refund_amount=5.0),
        ],
        guests=5,
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-15") == 325.0


def test_discountAmount_used_directly_no_fallback_needed():
    """The Toast UI formula uses `discountAmount` directly. Older POS
    versions emit it; newer ones emit both `discountAmount` and
    `nonTaxableDiscountAmount`. Either way, formula reads discountAmount.
    """
    orders = [_make_order(
        "2026-05-16",
        check_amount=90.0,
        selections=[_selection(pre_discount=100.0, selection_discount=10.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-16") == 90.0
