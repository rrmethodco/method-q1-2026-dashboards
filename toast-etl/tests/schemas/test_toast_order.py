"""Tests for ToastOrder schema.

Built from current Toast /ordersBulk row shape as observed at
toast_sync.py:730-810. Goal: every row CURRENTLY produced by a
healthy sync MUST validate. Strictness comes later via business rules.
"""
import pytest
from datetime import datetime
from schemas.toast_order import ToastOrder, ToastCheck


def test_minimal_valid_order():
    raw = {
        "guid": "abc-123",
        "openedDate": "2026-04-22T19:00:00.000Z",
        "closedDate": "2026-04-22T20:30:00.000Z",
        "voided": False,
        "deleted": False,
        "numberOfGuests": 2,
        "checks": [{
            "guid": "chk-1",
            "voided": False,
            "deleted": False,
            "amount": 87.50,
            "tipAmount": 17.50,
            "openedDate": "2026-04-22T19:00:00.000Z",
            "paidDate": "2026-04-22T20:30:00.000Z",
            "selections": [],
            "appliedDiscounts": [],
            "appliedServiceCharges": [],
        }],
    }
    o = ToastOrder.model_validate(raw)
    assert o.guid == "abc-123"
    assert o.checks[0].amount == 87.50
    assert o.validate_business_rules() == []


def test_voided_order_validates_but_zero_amount_ok():
    """Voided orders are emitted by Toast with amount=0; must validate."""
    raw = {
        "guid": "v-1", "voided": True, "deleted": False,
        "openedDate": "2026-04-22T19:00:00.000Z",
        "closedDate": "2026-04-22T19:01:00.000Z",
        "checks": [{"guid": "v-c", "voided": True, "amount": 0,
                    "tipAmount": 0, "openedDate": "2026-04-22T19:00:00.000Z"}],
    }
    o = ToastOrder.model_validate(raw)
    assert o.voided is True
    assert o.validate_business_rules() == []


def test_paid_before_opened_fails_business_rule():
    raw = {
        "guid": "bad", "voided": False, "deleted": False,
        "openedDate": "2026-04-22T20:00:00.000Z",
        "closedDate": "2026-04-22T20:30:00.000Z",
        "checks": [{
            "guid": "c", "voided": False, "amount": 50, "tipAmount": 10,
            "openedDate": "2026-04-22T20:00:00.000Z",
            "paidDate": "2026-04-22T19:00:00.000Z",  # paid before opened — bug
        }],
    }
    o = ToastOrder.model_validate(raw)
    errs = o.validate_business_rules()
    assert any("paid_before_opened" in e for e in errs)


def test_negative_amount_fails():
    raw = {
        "guid": "neg", "voided": False, "deleted": False,
        "openedDate": "2026-04-22T19:00:00.000Z",
        "checks": [{"guid": "c", "voided": False, "amount": -10,
                    "tipAmount": 0, "openedDate": "2026-04-22T19:00:00.000Z"}],
    }
    with pytest.raises(Exception):
        ToastOrder.model_validate(raw)


def test_missing_required_check_fields_fails():
    raw = {
        "guid": "miss", "voided": False, "deleted": False,
        "openedDate": "2026-04-22T19:00:00.000Z",
        "checks": [{}],  # missing amount, tipAmount, etc.
    }
    with pytest.raises(Exception):
        ToastOrder.model_validate(raw)
