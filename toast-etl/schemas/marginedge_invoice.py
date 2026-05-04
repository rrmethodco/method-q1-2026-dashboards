"""MarginEdge invoice row schema.

Built from marginedge_sync.py transform_order output. Per PR #88, the
LIST→DETAIL path can return an order in /orders that 404s on /orders/{id};
we record those with empty line_items (the model accepts this case).
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


VALID_COGS_BUCKETS = {"food", "beer", "wine", "liquor", "na_beverages"}


class MarginEdgeLineItem(SourceRow):
    """A single line item on a MarginEdge invoice.

    Fields map directly from transform_order output. product_name and
    category are optional to handle incomplete API responses.
    """
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    category: Optional[str] = None
    category_id: Optional[str] = None
    category_type: Optional[str] = None
    cogs_bucket: Optional[str] = None
    quantity: float = Field(ge=0)
    unit_price: float = Field(ge=0)
    extended: float = Field(ge=0)


class MarginEdgeInvoice(SourceRow):
    """A MarginEdge invoice (one order). May have empty line_items if the
    /orders/{id} detail fetch 404'd (PR #88 skip-on-404 path).

    Fields align with transform_order output schema:
    - invoice_id: orderId from MarginEdge
    - date: invoiceDate or createdDate (YYYY-MM-DD)
    - vendor_name: supplier name
    - total: orderTotal
    - line_items: list of line items (may be empty on 404)
    """
    _source_name = "marginedge_invoice"

    invoice_id: str
    invoice_number: Optional[str] = None
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    total: float = Field(ge=0)
    status: Optional[str] = None
    line_items: list[MarginEdgeLineItem] = Field(default_factory=list)

    def validate_business_rules(self) -> list[str]:
        """Validate cross-field invariants for invoice data quality.

        Checks:
        1. line_items extended sum should equal invoice total within 1%.
           Larger mismatches indicate dropped line items or cost errors.
        2. cogs_bucket values must be in the known set. Unknown buckets
           prevent proper cost rollups and indicate schema drift.
        """
        errors: list[str] = []

        # Line items sum should equal the invoice total within 1% — when
        # they diverge by more, MarginEdge has either dropped a line item
        # or mis-reported a unit cost. Skip when no line items (the
        # 404-on-detail path from PR #88).
        if self.line_items:
            li_sum = sum(li.extended for li in self.line_items)
            if self.total > 0:
                drift = abs(li_sum - self.total) / self.total
                if drift > 0.01:
                    errors.append(f"line_items_sum_mismatch: "
                                  f"total={self.total:.2f} li_sum={li_sum:.2f} "
                                  f"drift={drift*100:.1f}%")

        # Unknown cogs_bucket → can't roll up properly. Warn.
        for i, li in enumerate(self.line_items):
            if li.cogs_bucket and li.cogs_bucket not in VALID_COGS_BUCKETS:
                errors.append(f"unknown_cogs_bucket: line[{i}] "
                              f"bucket={li.cogs_bucket!r}")

        return errors
