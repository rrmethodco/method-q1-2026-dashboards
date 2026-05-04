"""MarginEdge invoice row schema.

Built from marginedge_sync.py:255-310 (transform_order output). Field
names mirror what that function emits — `invoice_id`, `vendor_name`,
`product_id`, `quantity`, `unit_price`, etc. Per PR #88, the LIST→DETAIL
path can return an order in /orders that 404s on /orders/{id}; we
record those with empty line_items (the model accepts this case).
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


VALID_COGS_BUCKETS = {"food", "beer", "wine", "liquor", "na_beverages"}


class MarginEdgeLineItem(SourceRow):
    """A single line item on a MarginEdge invoice.

    Field names mirror marginedge_sync.py:transform_order line-item
    output (companyConceptProductId → product_id, vendorItemName →
    product_name, etc.).
    """
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    category: Optional[str] = None
    category_id: Optional[str] = None
    category_type: Optional[str] = None
    cogs_bucket: Optional[str] = None
    quantity: Optional[float] = Field(default=None, ge=0)
    unit_price: Optional[float] = Field(default=None, ge=0)
    extended: Optional[float] = Field(default=None, ge=0)


class MarginEdgeInvoice(SourceRow):
    """A MarginEdge invoice (one order). May have empty line_items if the
    /orders/{id} detail fetch 404'd (PR #88 skip-on-404 path)."""
    _source_name = "marginedge_invoice"

    invoice_id: str
    invoice_number: Optional[str] = None
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    total: float  # MarginEdge emits negative totals for credits/refunds; flagged via business rule
    status: Optional[str] = None
    line_items: list[MarginEdgeLineItem] = Field(default_factory=list)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        if self.total < 0:
            errors.append(f"negative_total: probable credit/refund (total={self.total:.2f})")
        # Line items sum should equal the invoice total within 1% — when
        # they diverge by more, MarginEdge has either dropped a line item
        # or mis-reported a unit cost. Skip when no line items (the
        # 404-on-detail path from PR #88) or when extended values are
        # missing on every line.
        if self.line_items:
            extendeds = [li.extended for li in self.line_items if li.extended is not None]
            if extendeds:
                li_sum = sum(extendeds)
                # Use abs(total) so credits/refunds (negative totals) still get
                # checked. Skip when total is exactly 0 (PREPROCESSING placeholders).
                if self.total != 0:
                    drift = abs(li_sum - self.total) / abs(self.total)
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
