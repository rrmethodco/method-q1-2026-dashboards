"""Tests for SageBudgetLine schema."""
import pytest
from pydantic import ValidationError
from schemas.sage_budget import SageBudgetLine


def test_minimal_valid_line():
    raw = {"date": "2026-05-04", "net_sales": 8500.00,
           "guests": 120, "orders": 80}
    b = SageBudgetLine.model_validate(raw)
    assert b.net_sales == 8500.00
    assert b.validate_business_rules() == []


def test_guests_optional():
    """Some outlets don't budget guests/orders at daily level — must validate."""
    raw = {"date": "2026-05-04", "net_sales": 8500.00,
           "guests": None, "orders": None}
    b = SageBudgetLine.model_validate(raw)
    assert b.guests is None
    assert b.validate_business_rules() == []


def test_negative_budget_fails():
    with pytest.raises(ValidationError):
        SageBudgetLine.model_validate({
            "date": "2026-05-04", "net_sales": -100,
            "guests": None, "orders": None,
        })


def test_avg_spend_implausible_warned():
    """$1000 net / 4 guests = $250/guest — suspicious."""
    raw = {"date": "2026-05-04", "net_sales": 1000,
           "guests": 4, "orders": None}
    b = SageBudgetLine.model_validate(raw)
    errs = b.validate_business_rules()
    assert any("avg_spend_implausible" in s for s in errs)


def test_zero_guests_no_avg_spend_check():
    """Closed-day budget (0 sales, 0 guests) shouldn't trigger avg-spend rule."""
    raw = {"date": "2026-12-25", "net_sales": 0,
           "guests": 0, "orders": 0}
    b = SageBudgetLine.model_validate(raw)
    assert b.validate_business_rules() == []


def test_invalid_date_format_fails():
    with pytest.raises(ValidationError):
        SageBudgetLine.model_validate({
            "date": "May 4, 2026", "net_sales": 1000,
            "guests": None, "orders": None,
        })
