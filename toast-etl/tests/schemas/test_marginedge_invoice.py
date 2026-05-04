"""Tests for MarginEdgeInvoice + MarginEdgeLineItem schemas."""
import pytest
from pydantic import ValidationError
from schemas.marginedge_invoice import MarginEdgeInvoice


def test_minimal_valid_invoice():
    """Minimal invoice with one line item. Line item sum matches total."""
    raw = {
        "invoice_id": "inv-123",
        "date": "2026-05-04",
        "vendor_name": "Sysco Detroit",
        "total": 50.40,
        "line_items": [
            {
                "product_name": "Tomatoes",
                "quantity": 24,
                "unit_price": 2.10,
                "extended": 50.40,
                "cogs_bucket": "food",
                "category": "Produce",
            },
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.total == 50.40
    assert len(inv.line_items) == 1
    assert inv.validate_business_rules() == []


def test_no_line_items_ok():
    """404-on-detail path (PR #88): order recorded with no line items."""
    raw = {
        "invoice_id": "inv-na",
        "date": "2026-05-04",
        "vendor_name": "Vendor",
        "total": 100.00,
        "line_items": [],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.line_items == []
    assert inv.validate_business_rules() == []


def test_line_item_sum_mismatch_business_rule():
    """If line items sum != invoice total within 1%, business-rule warning."""
    raw = {
        "invoice_id": "mismatch",
        "date": "2026-05-04",
        "vendor_name": "v",
        "total": 100.00,
        "line_items": [
            {
                "product_name": "p",
                "quantity": 1,
                "unit_price": 50,
                "extended": 50.00,
                "cogs_bucket": "food",
            },
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    errs = inv.validate_business_rules()
    assert any("line_items_sum_mismatch" in e for e in errs)


def test_negative_total_fails():
    """Negative total should fail Pydantic validation (ge=0)."""
    with pytest.raises(ValidationError):
        MarginEdgeInvoice.model_validate({
            "invoice_id": "neg",
            "date": "2026-05-04",
            "vendor_name": "v",
            "total": -10,
            "line_items": [],
        })


def test_invalid_cogs_bucket_warned():
    """Unknown cogs_bucket triggers business-rule warning."""
    raw = {
        "invoice_id": "wb",
        "date": "2026-05-04",
        "vendor_name": "v",
        "total": 50,
        "line_items": [
            {
                "product_name": "p",
                "quantity": 1,
                "unit_price": 50,
                "extended": 50,
                "cogs_bucket": "kitchen_supplies",  # not a valid bucket
            },
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    errs = inv.validate_business_rules()
    assert any("unknown_cogs_bucket" in e for e in errs)
