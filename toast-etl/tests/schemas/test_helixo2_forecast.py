"""Tests for Helixo2Forecast schema."""
import pytest
from pydantic import ValidationError
from schemas.helixo2_forecast import Helixo2Forecast


def test_minimal_valid_row():
    raw = {"date": "2026-05-04", "net_sales": 7500.00,
           "guests": 120, "orders": None, "ai_confidence": 0.93}
    f = Helixo2Forecast.model_validate(raw)
    assert f.net_sales == 7500.00
    assert f.validate_business_rules() == []


def test_confidence_out_of_range_fails():
    with pytest.raises(ValidationError):
        Helixo2Forecast.model_validate({
            "date": "2026-05-04", "net_sales": 100,
            "guests": 5, "orders": None, "ai_confidence": 1.5,
        })


def test_zero_revenue_with_high_confidence_warned():
    """Forecast = $0 with ai_confidence > 0.7 is suspicious — likely an
    unmapped outlet falling through to a default value."""
    raw = {"date": "2026-05-04", "net_sales": 0,
           "guests": 0, "orders": None, "ai_confidence": 0.95}
    f = Helixo2Forecast.model_validate(raw)
    errs = f.validate_business_rules()
    assert any("zero_revenue_high_confidence" in s for s in errs)


def test_zero_revenue_low_confidence_ok():
    """Closed-day forecast (legitimate $0 with low confidence) shouldn't fire."""
    raw = {"date": "2026-12-25", "net_sales": 0,
           "guests": 0, "orders": None, "ai_confidence": 0.3}
    f = Helixo2Forecast.model_validate(raw)
    assert f.validate_business_rules() == []
