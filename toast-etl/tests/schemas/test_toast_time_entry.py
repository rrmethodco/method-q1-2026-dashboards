"""Tests for ToastTimeEntry schema.

Built from toast_sync.py:476-540. Each row is a single shift clock-in/out.
"""
import pytest
from pydantic import ValidationError
from schemas.toast_time_entry import ToastTimeEntry


def test_minimal_valid_entry():
    raw = {
        "guid": "te-1",
        "deleted": False,
        "businessDate": "20260424",
        "regularHours": 8.0,
        "overtimeHours": 0.0,
        "hourlyWage": 18.50,
        "inDate": "2026-04-24T15:00:00.000Z",
        "outDate": "2026-04-24T23:00:00.000Z",
        "employeeReference": {"guid": "emp-1"},
        "jobReference": {"guid": "job-bartender"},
    }
    e = ToastTimeEntry.model_validate(raw)
    assert e.regularHours == 8.0
    assert e.businessDate == "20260424"
    assert e.validate_business_rules() == []


def test_overtime_business_rule():
    """OT > 0 is normal; OT > 40h in a single shift is a sanity check."""
    raw = {
        "guid": "ot-bug", "deleted": False, "businessDate": "20260424",
        "regularHours": 0.0, "overtimeHours": 50.0, "hourlyWage": 18.5,
        "inDate": "2026-04-24T15:00:00.000Z",
        "outDate": "2026-04-25T15:00:00.000Z",
        "employeeReference": {"guid": "emp"}, "jobReference": {"guid": "job"},
    }
    e = ToastTimeEntry.model_validate(raw)
    errs = e.validate_business_rules()
    assert any("overtime_implausible" in s for s in errs)


def test_clockout_before_clockin_fails():
    raw = {
        "guid": "neg", "deleted": False, "businessDate": "20260424",
        "regularHours": 1.0, "overtimeHours": 0.0, "hourlyWage": 18.5,
        "inDate": "2026-04-24T20:00:00.000Z",
        "outDate": "2026-04-24T19:00:00.000Z",
        "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
    }
    e = ToastTimeEntry.model_validate(raw)
    assert any("clockout_before_clockin" in s for s in e.validate_business_rules())


def test_negative_hours_fails():
    with pytest.raises(ValidationError):
        ToastTimeEntry.model_validate({
            "guid": "n", "deleted": False, "businessDate": "20260424",
            "regularHours": -1.0, "overtimeHours": 0.0, "hourlyWage": 18.5,
            "inDate": "2026-04-24T15:00:00.000Z",
            "outDate": "2026-04-24T23:00:00.000Z",
            "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
        })


def test_business_date_format_must_be_yyyymmdd():
    with pytest.raises(ValidationError):
        ToastTimeEntry.model_validate({
            "guid": "fmt", "deleted": False,
            "businessDate": "2026-04-24",  # wrong: should be 20260424
            "regularHours": 1.0, "overtimeHours": 0.0, "hourlyWage": 18.5,
            "inDate": "2026-04-24T15:00:00.000Z",
            "outDate": "2026-04-24T16:00:00.000Z",
            "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
        })
