"""helixo-2 daily_forecasts row schema."""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


class Helixo2Forecast(SourceRow):
    """A single daily-forecast row sourced from helixo-2's
    daily_forecasts table (ai_suggested_revenue + ai_suggested_covers
    + ai_confidence). Method does not retrain — passthrough."""
    _source_name = "helixo2_forecast"

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    net_sales: float = Field(ge=0)
    guests: Optional[int] = Field(default=None, ge=0)
    orders: Optional[int] = Field(default=None, ge=0)
    ai_confidence: Optional[float] = Field(default=None, ge=0, le=1)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # An outlet with $0 net_sales forecast AND ai_confidence > 0.7 is
        # the failure pattern observed for unmapped outlets — helixo-2
        # confidently predicts 0 because it has no signal. Flag it.
        if (self.net_sales == 0 and self.ai_confidence is not None
                and self.ai_confidence > 0.7):
            errors.append(f"zero_revenue_high_confidence: "
                          f"sales=0 conf={self.ai_confidence}")
        return errors
