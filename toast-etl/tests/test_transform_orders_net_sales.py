"""Lock in the Net Sales computation in transform_orders().

Background
----------
Pre-2026-05-05, the dashboard's "Net Sales" KPI sourced from
`check.amount` on Toast's Orders API. Empirically that field runs
+20–30% above Toast's official "Net sales" line on the Sales Summary
report (it includes tips, non-gratuity service charges, and other items
the report excludes). Ross caught the gap when LSBR week ending 5/3
showed $143.3K on the dashboard vs. $125,673.04 in Toast direct.

The current `net_sales` formula in transform_orders matches Toast's
exact 5-step calculation per
https://doc.toasttab.com/doc/devguide/apiOrdersNetSalesCalculation.html

  1. For each menu item selection (excluding voided + Gift Card):
       sel_net = preDiscountPrice
                − sum(appliedDiscounts.nonTaxableDiscountAmount)
                − refundDetails.refundAmount
  2. Sum sel_net across selections = check_net
  3. Add non-gratuity service charges:
       check_net += sum(appliedServiceCharges.chargeAmount where !gratuity)
                  − sum(appliedServiceCharges.refundDetails.refundAmount where !gratuity)
  4. Subtract check-level discounts:
       check_net −= sum(check.appliedDiscounts.nonTaxableDiscountAmount)
  5. Sum check_net across the order's checks.

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


def test_nontaxable_discount_amount_is_preferred_over_discount_amount():
    """Toast's documented formula uses `nonTaxableDiscountAmount`, not
    `discountAmount`. The taxable portion of a discount doesn't reduce
    Net Sales — it reduces tax. Pre-#99 we were summing `discountAmount`,
    which over-subtracted on discounts that had a taxable component.

    Helper emits BOTH fields with `discountAmount = nonTaxable + 1` as a
    decoy. The correct formula picks `nonTaxableDiscountAmount = $10`,
    yielding net_sales = 100 - 10 = $90.
    """
    orders = [_make_order(
        "2026-05-08",
        check_amount=89.0,  # post-discount check amount irrelevant to formula
        selections=[_selection(
            pre_discount=100.0,
            selection_nontax_discount=10.0,  # discountAmount becomes 11.0 (decoy)
        )],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-08") == 90.0, (
        "Must use nonTaxableDiscountAmount=10 (giving 90), not discountAmount=11 (giving 89)"
    )


def test_non_gratuity_service_charge_adds_to_net_sales():
    """Toast Net Sales formula step 3: ADD non-gratuity service charges.

    A delivery fee or mandatory non-gratuity service charge is paid by
    the guest and kept by the restaurant — it's revenue. Auto-gratuity
    configured with `gratuity: true` (a tip pool) is excluded.

    $100 item + $5 non-gratuity SC + $20 auto-grat (gratuity SC):
      Net Sales = 100 + 5 = $105 (auto-grat excluded)
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
    assert _net_sales_for_day(out, "2026-05-09") == 105.0


def test_service_charge_chargeAmount_is_preferred_over_amount():
    """Toast docs specify `ServiceCharge.chargeAmount`. Older Toast POS
    versions emit only `amount`. Formula must prefer `chargeAmount` when
    present, fall back to `amount` otherwise.
    """
    orders = [_make_order(
        "2026-05-10",
        check_amount=110.0,
        service_charges=[
            # chargeAmount=10 (truth), amount=99 (decoy from older schema)
            {"chargeAmount": 10.0, "amount": 99.0, "gratuity": False},
        ],
        selections=[_selection(pre_discount=100.0)],
    )]
    out = transform_orders(orders)
    assert _net_sales_for_day(out, "2026-05-10") == 110.0  # 100 + 10, not 100 + 99


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


def test_service_charge_refund_subtracts_from_net_sales():
    """Non-gratuity SC refunds reduce Net Sales (per Toast docs:
    'Net Sales figures decrease by the amount of the refund. ... this
    includes ... service charges').
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
    # 100 (item) + 10 (SC) - 5 (SC refund) = 105
    assert _net_sales_for_day(out, "2026-05-13") == 105.0


def test_combo_discount_handled_via_check_appliedDiscounts():
    """Combo discounts: the discount is registered at check level with
    `nonTaxableDiscountAmount` set; selection.preDiscountPrice stays at
    the original price. Net Sales subtracts the check-level discount.

    Two soups @ $8.99 each (preDiscountPrice = $17.98 total),
    combo discount = $2.98 → check.appliedDiscounts has one combo entry
    with nonTaxableDiscountAmount = $2.98. Net Sales = 17.98 - 2.98 = $15.
    """
    orders = [_make_order(
        "2026-05-14",
        check_amount=15.0,
        check_discounts=[{
            "menuItemSelectionGuid": "s2",
            "discountGuid": "combo",
            "nonTaxableDiscountAmount": 2.98,
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


def test_le_supreme_with_non_gratuity_sc_and_refund():
    """Realistic Le Suprême large-party check with both auto-grat AND a
    non-gratuity service charge AND a partial refund.

    5 entrees @ $50 = $250.
    Wine bottle @ $80 (with $5 partial refund).
    Auto-grat 20% as service charge = $66 (gratuity, excluded).
    Mandatory wellness fee 3% as non-gratuity SC = $9.90 (with $1 refund).

      Net Sales = 250 (entrees)
                + 75 (wine: 80 - 5 refund)
                + 9.90 (wellness fee)
                - 1.00 (wellness fee refund)
                = $333.90
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
    assert _net_sales_for_day(out, "2026-05-15") == round(250 + 75 + 9.90 - 1.00, 2)


def test_legacy_discountAmount_only_falls_back_correctly():
    """For Toast POS versions older than the nonTaxableDiscountAmount
    rollout, the formula must fall back to discountAmount. Confirm the
    fallback triggers when nonTaxableDiscountAmount is absent (None).
    """
    orders = [_make_order(
        "2026-05-16",
        check_amount=90.0,
        # appliedDiscounts has discountAmount only (no nonTaxableDiscountAmount)
        selections=[_selection(pre_discount=100.0, selection_discount=10.0)],
    )]
    out = transform_orders(orders)
    # Falls back to discountAmount=10 since nonTaxableDiscountAmount missing
    assert _net_sales_for_day(out, "2026-05-16") == 90.0
