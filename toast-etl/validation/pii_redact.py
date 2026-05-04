"""PII redaction for validation error samples.

Intentionally conservative: redacts known sensitive field names regardless
of value content. We don't try to heuristically detect PII in other fields.
"""
from __future__ import annotations

PII_FIELDS = {
    "email": "email",
    "user_email": "email",
    "contact_email": "email",
    "phone": "phone",
    "contact_phone": "phone",
    "phone_number": "phone",
    "full_name": "name",
    "first_name": "name",
    "last_name": "name",
    "guest_name": "name",
    # Names that show up in our actual data sources (Resy surveys → server;
    # Google reviews → author/reviewer_name; Tripleseat → booking_contact /
    # account_name). Staff names are technically employment data, not
    # consumer PII, but redaction keeps validation error samples clean.
    "server": "name",
    "author": "name",
    "author_name": "name",
    "customer_name": "name",
    "display_name": "name",
    "reviewer_name": "name",
    "booking_contact": "name",
    "address": "address",
    "street": "address",
    "credit_card": "cc",
    "card_number": "cc",
    "ssn": "ssn",
}


def redact_pii(obj):
    """Recursively redact known PII field names. Returns a NEW object."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            tag = PII_FIELDS.get(k.lower())
            if tag is not None:
                out[k] = f"[REDACTED:{tag}]"
            else:
                out[k] = redact_pii(v)
        return out
    if isinstance(obj, list):
        return [redact_pii(x) for x in obj]
    return obj
