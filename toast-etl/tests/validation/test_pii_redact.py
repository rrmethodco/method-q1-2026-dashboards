"""Tests for PII redaction."""
from validation.pii_redact import redact_pii


def test_redacts_email():
    row = {"id": 1, "user": {"email": "ross@methodco.com", "name": "Ross"}}
    out = redact_pii(row)
    assert out["user"]["email"] == "[REDACTED:email]"


def test_redacts_phone():
    row = {"reservation": {"contact_phone": "+1-215-555-1212"}}
    out = redact_pii(row)
    assert out["reservation"]["contact_phone"] == "[REDACTED:phone]"


def test_redacts_full_name():
    row = {"user": {"full_name": "Ross Richardson"}}
    out = redact_pii(row)
    assert out["user"]["full_name"] == "[REDACTED:name]"


def test_preserves_non_pii():
    row = {"id": 1, "amount": 50.00, "date": "2026-05-04"}
    out = redact_pii(row)
    assert out == row


def test_handles_nested_lists():
    row = {"items": [{"user": {"email": "a@b.com"}}, {"user": {"email": "c@d.com"}}]}
    out = redact_pii(row)
    assert out["items"][0]["user"]["email"] == "[REDACTED:email]"
    assert out["items"][1]["user"]["email"] == "[REDACTED:email]"
