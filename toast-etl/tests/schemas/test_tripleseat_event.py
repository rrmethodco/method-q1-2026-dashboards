"""Tests for TripleseatEvent + TripleseatFinancials schemas.

Field names match the real shape in data/<outlet>.json (verified 2026-05-04).
"""
import pytest
from pydantic import ValidationError
from schemas.tripleseat_event import TripleseatEvent


def test_minimal_valid_event():
    raw = {
        "event_id": 1, "name": "Smith Wedding",
        "status": "DEFINITE",
        "event_start": "6/15/2026 5:30 PM",
        "guest_count": 120,
    }
    e = TripleseatEvent.model_validate(raw)
    assert e.event_id == 1
    assert e.guest_count == 120
    assert e.validate_business_rules() == []


def test_full_event_with_financials():
    raw = {
        "event_id": 2, "name": "Wedding",
        "status": "DEFINITE",
        "event_start": "6/15/2026 5:30 PM",
        "event_end": "6/15/2026 11:00 PM",
        "guest_count": 100, "event_type": "Wedding",
        "segment": "Social", "location_id": 30297,
        "booking_id": 12345, "rooms": ["Vessel Buyout"],
        "created_at": "2/26/2026 10:44 AM",
        "lead_time_days": 110,
        "financials": {
            "total": 18000, "grand_total": 22000,
            "food": 8000, "beverage": 4000,
            "rental": 5000, "av": 1000, "other": 0,
            "tax": 2200, "gratuity": 1800, "service_charge": 0,
            "actual_amount": 0, "fb_minimum": 12000,
        },
        "booked_revenue": 22000.0, "revenue_share": 1.0,
    }
    e = TripleseatEvent.model_validate(raw)
    assert e.financials.grand_total == 22000
    assert e.validate_business_rules() == []


def test_unknown_status_warns():
    raw = {
        "event_id": 3, "name": "x", "status": "WEIRD-NEW-STATUS",
    }
    e = TripleseatEvent.model_validate(raw)
    assert any("unknown_status" in s for s in e.validate_business_rules())


def test_fb_exceeds_grand_total_warns():
    raw = {
        "event_id": 4, "name": "x", "status": "DEFINITE",
        "financials": {
            "grand_total": 1000,
            "food": 800, "beverage": 500,  # 1300 > 1000
        },
    }
    e = TripleseatEvent.model_validate(raw)
    assert any("fb_exceeds_grand_total" in s for s in e.validate_business_rules())


def test_negative_guest_count_fails():
    with pytest.raises(ValidationError):
        TripleseatEvent.model_validate({
            "event_id": 5, "name": "x", "status": "DEFINITE",
            "guest_count": -1,
        })


def test_revenue_share_out_of_range_fails():
    with pytest.raises(ValidationError):
        TripleseatEvent.model_validate({
            "event_id": 6, "name": "x", "status": "DEFINITE",
            "revenue_share": 1.5,
        })
