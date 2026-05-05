"""End-to-end regression: validation runner output → transform_orders.

Catches the class of bug Ross hit 2026-05-05: the validation runner was
emitting datetime objects from model_dump(), which transform_orders
silently accepted into the existing string-typed code paths. Python
3.12's "'str' object cannot be interpreted as an integer" error fired
at the `datetime.replace("Z", "+00:00")` call inside _as_local_date —
because datetime.replace() expects an int year as positional arg #1.

This test pins the runner → consumer contract: the output of
run_validation() against ToastOrder must round-trip cleanly through
transform_orders without throwing.

If this test breaks, the fix is in toast-etl/validation/runner.py:
either keep mode="json" on model_dump or update every consumer to
handle native datetime objects.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from schemas.toast_order import ToastOrder  # noqa: E402
from toast_sync import transform_orders  # noqa: E402
from validation.runner import run_validation  # noqa: E402


def _real_toast_order(date: str = "2026-05-05") -> dict:
    """A realistic raw Toast /ordersBulk row — strings for ISO dates,
    nested checks + selections + applied discounts, etc."""
    return {
        "guid": "ord-1",
        "voided": False,
        "deleted": False,
        "openedDate": f"{date}T22:00:00.000Z",
        "closedDate": f"{date}T23:30:00.000Z",
        "numberOfGuests": 4,
        "checks": [
            {
                "guid": "chk-1",
                "voided": False,
                "deleted": False,
                "amount": 336.0,
                "tipAmount": 0.0,
                "openedDate": f"{date}T22:00:00.000Z",
                "paidDate": f"{date}T23:30:00.000Z",
                "appliedServiceCharges": [{"amount": 56.0, "gratuity": True}],
                "appliedDiscounts": [],
                "selections": [
                    {"guid": "s1", "preDiscountPrice": 50.0, "price": 50.0},
                    {"guid": "s2", "preDiscountPrice": 50.0, "price": 50.0},
                    {"guid": "s3", "preDiscountPrice": 50.0, "price": 50.0},
                    {"guid": "s4", "preDiscountPrice": 50.0, "price": 50.0},
                    {"guid": "s5", "preDiscountPrice": 80.0, "price": 80.0},
                ],
            }
        ],
    }


def test_runner_output_is_consumable_by_transform_orders(tmp_path):
    """Round-trip: real-shape Toast row → ToastOrder validation → transform_orders.

    Pre-fix this raised "'str' object cannot be interpreted as an integer"
    on Python 3.12 because datetime.replace was called with a str arg.
    Post-fix the validated row keeps ISO-string dates and transform_orders
    produces correct daily/monthly rollups.
    """
    raw = [_real_toast_order("2026-05-05")]

    v = run_validation(
        rows=raw, model_cls=ToastOrder, source="toast_order",
        outlets_touched=["lsbr"], data_dir=tmp_path,
        update_outlet_index=False,
    )
    assert v["rows_valid"] == 1, f"validation should accept this row, got {v}"

    # Confirm the contract: dates are strings, not datetime objects.
    rec = v["valid_rows"][0]
    assert isinstance(rec["openedDate"], str), \
        f"openedDate must be ISO string, got {type(rec['openedDate']).__name__}"
    assert isinstance(rec["checks"][0]["paidDate"], str), \
        "check.paidDate must be ISO string"

    # Now feed into transform_orders — this is the exact path toast_sync
    # uses (sync_outlet → run_validation → transform_orders).
    out = transform_orders(v["valid_rows"])

    # Must produce one daily row with correct net_sales (the LS large-party
    # auto-grat case from our unit tests: $336 amount, $280 net_sales).
    assert len(out["daily"]) == 1
    row = out["daily"][0]
    assert row["amount"] == 336.0
    assert row["net_sales"] == 280.0
    assert row["gratuity"] == 56.0
    assert row["orders"] == 1
    assert row["guests"] == 4


def test_transform_handles_pydantic_round_tripped_dates_directly():
    """Defensive: even if some other consumer changes the model_dump mode,
    transform_orders should still handle the case where dates land as
    ISO strings (the contract). This guards against regressions.
    """
    # Simulate the post-validation shape directly (no runner involved).
    pydantic_dumped = {
        "guid": "ord-1",
        "voided": False,
        "deleted": False,
        "openedDate": "2026-05-05T22:00:00Z",
        "closedDate": "2026-05-05T23:30:00Z",
        "numberOfGuests": 2,
        "checks": [{
            "guid": "chk-1",
            "voided": False,
            "deleted": False,
            "amount": 100.0,
            "tipAmount": 0.0,
            "openedDate": "2026-05-05T22:00:00Z",
            "paidDate": "2026-05-05T23:00:00Z",
            "appliedServiceCharges": [],
            "appliedDiscounts": [],
            "selections": [
                {"guid": "s1", "preDiscountPrice": 100.0, "price": 100.0},
            ],
        }],
    }
    out = transform_orders([pydantic_dumped])
    assert len(out["daily"]) == 1
    assert out["daily"][0]["net_sales"] == 100.0
