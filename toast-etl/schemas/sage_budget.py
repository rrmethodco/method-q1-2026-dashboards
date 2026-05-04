"""Sage Intacct budget row schema.

Built from budget_sync.py output → data/<outlet>.json budget.daily[].
Only revenue + covers are budgeted at the daily level. Higher-level
budget lines (labor, COGS, opex) live in a separate (non-daily)
rollup that this schema doesn't cover — adding them would be a
separate task once the corresponding rollup data lands.
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


class SageBudgetLine(SourceRow):
    """A single daily budget row from Sage Intacct."""
    _source_name = "sage_budget"

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    net_sales: float = Field(ge=0)
    guests: Optional[int] = Field(default=None, ge=0)
    orders: Optional[int] = Field(default=None, ge=0)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # Sanity: avg-spend per guest sanity check when both are present.
        # An outlet with > $200/guest budgeted is implausible for FSR;
        # fine-dining max is ~$150. Surface as a soft signal.
        if self.guests and self.guests > 0:
            avg_spend = self.net_sales / self.guests
            if avg_spend > 200:
                errors.append(f"avg_spend_implausible: "
                              f"${avg_spend:.2f}/guest "
                              f"(net_sales=${self.net_sales}, guests={self.guests})")
        return errors
