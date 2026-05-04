"""Toast /ordersBulk row schema.

Built from the consumer at toast_sync.py:726-810. Toast emits orders
with one or more checks; checks have selections (line items),
discounts, and service charges. We require enough fields to compute:
  - net_sales (sum of check.amount)
  - covers (order.numberOfGuests OR check.customer.guestCount)
  - tip + gratuity
  - discount $
  - ticket time (paidDate - openedDate)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import Field, field_validator

from ._base import SourceRow


class ToastSelection(SourceRow):
    """A line item on a check. Permissive — Toast adds new fields here often."""
    guid: Optional[str] = None
    appliedDiscounts: list[dict] = Field(default_factory=list)


class ToastCheck(SourceRow):
    guid: str
    voided: bool = False
    deleted: bool = False
    amount: float = Field(ge=0, description="Pre-tax subtotal — must be >= 0")
    tipAmount: float = Field(default=0, ge=0)
    openedDate: datetime
    paidDate: Optional[datetime] = None
    closedDate: Optional[datetime] = None
    selections: list[ToastSelection] = Field(default_factory=list)
    appliedDiscounts: list[dict] = Field(default_factory=list)
    appliedServiceCharges: list[dict] = Field(default_factory=list)
    customer: Optional[dict] = None

    @field_validator("amount", "tipAmount", mode="before")
    @classmethod
    def _coerce_numeric(cls, v):
        # Toast occasionally emits "amount": "87.50" as a string
        if isinstance(v, str):
            return float(v)
        return v


class ToastOrder(SourceRow):
    _source_name = "toast_order"

    guid: str
    voided: bool = False
    deleted: bool = False
    openedDate: datetime
    closedDate: Optional[datetime] = None
    numberOfGuests: int = Field(default=0, ge=0)
    checks: list[ToastCheck]

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # Skip business-rule checks on voided/deleted orders — Toast often
        # leaves zeroed-out fields on these.
        if self.voided or self.deleted:
            return errors
        for i, c in enumerate(self.checks):
            if c.paidDate and c.paidDate < c.openedDate:
                errors.append(f"check[{i}]: paid_before_opened "
                              f"(paid={c.paidDate.isoformat()}, "
                              f"opened={c.openedDate.isoformat()})")
            if self.closedDate and self.closedDate < self.openedDate:
                errors.append(f"order: closed_before_opened "
                              f"(closed={self.closedDate.isoformat()}, "
                              f"opened={self.openedDate.isoformat()})")
        return errors
