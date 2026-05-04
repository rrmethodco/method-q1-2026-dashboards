"""Tests for MarginEdgeInvoice + MarginEdgeLineItem schemas.

Field names mirror marginedge_sync.py transform_order output —
see schemas/marginedge_invoice.py for the canonical reference.
"""
import pytest
from pydantic import ValidationError
from schemas.marginedge_invoice import MarginEdgeInvoice


def test_minimal_valid_invoice():
    raw = {
        "invoice_id": "inv-123", "invoice_number": "INV-9001",
        "date": "2026-05-04", "vendor_id": "v-1",
        "vendor_name": "Sysco Detroit", "total": 1842.50,
        "status": "POSTED",
        "line_items": [
            {"product_id": "p-1", "product_name": "Tomatoes",
             "quantity": 24, "unit_price": 76.77, "extended": 1842.50,
             "cogs_bucket": "food", "category": "Produce",
             "category_id": "cat-prod", "category_type": "FOOD"},
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.total == 1842.50
    assert len(inv.line_items) == 1
    assert inv.validate_business_rules() == []


def test_no_line_items_ok():
    """404-on-detail path (PR #88): order recorded with no line items."""
    raw = {
        "invoice_id": "inv-na", "date": "2026-05-04",
        "vendor_name": "Vendor", "total": 100.00, "line_items": [],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.line_items == []
    assert inv.validate_business_rules() == []


def test_line_item_sum_mismatch_business_rule():
    """If line items extended sum != invoice total within 1%, business-rule warning."""
    raw = {
        "invoice_id": "mismatch", "date": "2026-05-04", "vendor_name": "v",
        "total": 100.00,
        "line_items": [
            {"product_id": "p", "product_name": "p", "quantity": 1,
             "unit_price": 50, "extended": 50.00, "cogs_bucket": "food"},
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    errs = inv.validate_business_rules()
    assert any("line_items_sum_mismatch" in e for e in errs)


def test_negative_total_fails():
    with pytest.raises(ValidationError):
        MarginEdgeInvoice.model_validate({
            "invoice_id": "neg", "date": "2026-05-04", "vendor_name": "v",
            "total": -10, "line_items": [],
        })


def test_invalid_cogs_bucket_warned():
    raw = {
        "invoice_id": "wb", "date": "2026-05-04", "vendor_name": "v",
        "total": 50,
        "line_items": [
            {"product_id": "p", "product_name": "p", "quantity": 1,
             "unit_price": 50, "extended": 50,
             "cogs_bucket": "kitchen_supplies"},
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    errs = inv.validate_business_rules()
    assert any("unknown_cogs_bucket" in e for e in errs)


def test_real_data_invoice_validates():
    """Sanity: a row shaped like marginedge_sync.py:255-310 actually emits."""
    raw = {
        "invoice_id": "216841082",
        "invoice_number": None,
        "date": "2026-04-30",
        "vendor_id": "0",
        "vendor_name": None,
        "total": 0.0,
        "status": "PREPROCESSING",
        "line_items": [],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.invoice_id == "216841082"
    assert inv.line_items == []
    # Empty line items + PREPROCESSING status should produce no business-rule errors.
    assert inv.validate_business_rules() == []
