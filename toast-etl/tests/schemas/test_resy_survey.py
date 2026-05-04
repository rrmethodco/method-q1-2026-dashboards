"""Tests for ResySurvey schema.

Critical: post-2026-04-17 surveys have null score buckets (food/service/
atmos/sentiment/recommend). Model must accept these — they're real data.
The schema-drift agent (Task 22) detects + classifies the issue; the
model itself doesn't crash on it.
"""
import pytest
from pydantic import ValidationError
from schemas.resy_survey import ResySurvey


def test_minimal_pre_drift_row():
    raw = {
        "date": "2026-04-15", "overall": 100, "sentiment": 100,
        "service": 100, "food": None, "atmos": None,
        "server": "Claire", "recommend": 10, "covers": 5,
        "dow": 2, "hour": 18,
    }
    s = ResySurvey.model_validate(raw)
    assert s.recommend == 10
    assert s.validate_business_rules() == []


def test_post_drift_row_validates():
    """Post-2026-04-17 shape: all 5 score buckets null. Must validate."""
    raw = {
        "date": "2026-04-29", "overall": 100, "sentiment": None,
        "service": None, "food": None, "atmos": None,
        "server": "Claire", "recommend": None, "covers": 2,
        "dow": 2, "hour": 22,
    }
    s = ResySurvey.model_validate(raw)
    errs = s.validate_business_rules()
    # Soft-signal: business rule warns but doesn't reject.
    assert any("all_score_buckets_null" in e for e in errs)


def test_recommend_out_of_range_fails():
    with pytest.raises(ValidationError):
        ResySurvey.model_validate({
            "date": "2026-04-15", "overall": 100, "recommend": 11,
            "covers": 1, "dow": 0, "hour": 18,
        })


def test_overall_out_of_range_fails():
    with pytest.raises(ValidationError):
        ResySurvey.model_validate({
            "date": "2026-04-15", "overall": 150, "recommend": 9,
            "covers": 1, "dow": 0, "hour": 18,
        })


def test_invalid_date_format_fails():
    with pytest.raises(ValidationError):
        ResySurvey.model_validate({
            "date": "April 15, 2026", "overall": 100, "recommend": 10,
            "covers": 1, "dow": 0, "hour": 18,
        })
