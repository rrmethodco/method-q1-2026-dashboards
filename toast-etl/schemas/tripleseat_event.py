"""Tripleseat event row schema.

Built from the actual shape stored in data/<outlet>.json under
events.events[]. Field names mirror what tripleseat_sync.py writes;
date strings are kept as-is (Tripleseat emits "M/D/YYYY H:MM AM/PM"
format; we don't parse to datetime here — the consumer handles that
where needed).
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


# Status values observed in production. Tripleseat uppercases these.
# If new statuses appear, the schema-drift detector flags them via the
# `unknown_status` business rule.
KNOWN_STATUSES = {"PROSPECT", "DEFINITE", "TENTATIVE", "CANCELLED", "CLOSED",
                  "LOST", "BOOKED", "PENDING_AUTH"}


class TripleseatFinancials(SourceRow):
    """Nested financials block on a Tripleseat event.

    All amounts are Optional because Tripleseat occasionally omits the
    block (older events) or returns a partial set; default 0.0 is not
    set so missing means missing (not zero).
    """
    total: Optional[float] = None
    grand_total: Optional[float] = None
    food: Optional[float] = None
    beverage: Optional[float] = None
    rental: Optional[float] = None
    av: Optional[float] = None
    other: Optional[float] = None
    tax: Optional[float] = None
    gratuity: Optional[float] = None
    service_charge: Optional[float] = None
    actual_amount: Optional[float] = None
    fb_minimum: Optional[float] = None


class TripleseatEvent(SourceRow):
    """A Tripleseat event row.

    Note: `event_start`/`event_end`/`created_at` are kept as strings
    because Tripleseat uses "M/D/YYYY H:MM AM/PM" format which doesn't
    round-trip cleanly through Pydantic's datetime parser. Consumers
    that need datetime should parse explicitly.
    """
    _source_name = "tripleseat_event"

    event_id: int
    name: str
    status: str
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    guest_count: Optional[int] = Field(default=None, ge=0)
    event_type: Optional[str] = None
    segment: Optional[str] = None
    location_id: Optional[int] = None
    booking_id: Optional[int] = None
    rooms: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    lead_time_days: Optional[int] = None
    financials: Optional[TripleseatFinancials] = None
    booked_revenue: Optional[float] = None
    revenue_share: Optional[float] = Field(default=None, ge=0, le=1)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        if self.status not in KNOWN_STATUSES:
            errors.append(f"unknown_status: {self.status!r} "
                          f"(known: {sorted(KNOWN_STATUSES)})")
        # Sanity: F&B financials shouldn't exceed grand_total. Skip if either
        # is missing or zero.
        if self.financials and self.financials.grand_total and self.financials.grand_total > 0:
            food = self.financials.food or 0.0
            bev = self.financials.beverage or 0.0
            fb = food + bev
            if fb > self.financials.grand_total + 0.01:
                errors.append(f"fb_exceeds_grand_total: "
                              f"fb={fb:.2f} grand_total={self.financials.grand_total:.2f}")
        return errors
