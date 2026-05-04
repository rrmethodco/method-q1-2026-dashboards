"""Toast /labor/v1/timeEntries row schema.

Built from toast_sync.py:476-540. Each row is one shift.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import Field, field_validator

from ._base import SourceRow


class ToastEmployeeRef(SourceRow):
    """Employee reference — just the GUID we look up via Toast's /employees catalog."""
    guid: str


class ToastJobRef(SourceRow):
    """Job/role reference — GUID we look up via Toast's /jobs catalog."""
    guid: str


class ToastTimeEntry(SourceRow):
    """A single Toast time entry (shift clock-in/out + paid hours)."""
    _source_name = "toast_time_entry"

    guid: str
    deleted: bool = False
    # Toast emits businessDate as a YYYYMMDD digit string. The transform
    # at toast_sync.py:480 fails if len != 8 — model the same constraint.
    businessDate: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    regularHours: float = Field(ge=0)
    overtimeHours: float = Field(ge=0)
    hourlyWage: float = Field(ge=0)
    inDate: datetime
    outDate: Optional[datetime] = None
    employeeReference: ToastEmployeeRef
    jobReference: ToastJobRef

    @field_validator("regularHours", "overtimeHours", "hourlyWage", mode="before")
    @classmethod
    def _coerce_numeric(cls, v):
        if isinstance(v, str):
            return float(v)
        return v

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        if self.deleted:
            return errors
        # Sanity: a single clock-in shouldn't claim more than 40 OT hours.
        if self.overtimeHours > 40:
            errors.append(f"overtime_implausible: ot={self.overtimeHours}")
        # Sanity: clockout before clockin (Toast occasionally emits these
        # for manual entries — we don't trust them).
        if self.outDate and self.outDate < self.inDate:
            errors.append(f"clockout_before_clockin: in={self.inDate.isoformat()} "
                          f"out={self.outDate.isoformat()}")
        # Sanity: total hours should be plausible vs span between in/out.
        if self.outDate:
            span_hours = (self.outDate - self.inDate).total_seconds() / 3600
            total = self.regularHours + self.overtimeHours
            # Allow 20% slack (breaks); if we report way more hours than
            # the span allows, something is off.
            if total > span_hours * 1.2 + 0.5:
                errors.append(f"hours_exceed_span: total={total} span={span_hours:.2f}")
        return errors
