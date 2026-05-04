"""Resy OS survey row schema.

Built from resy_os_scraper.py transform_resy_survey_row output.
The 5 score buckets (food, service, atmos, sentiment, recommend) are
all Optional because Resy schema drift on/around 2026-04-17 nulled
them — the schema-drift detector (Task 22) flags this as a known
condition; the model accepts the rows so they still ingest.
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


class ResySurvey(SourceRow):
    """A Resy OS guest survey row. All 5 score buckets are Optional to
    tolerate the post-2026-04-17 schema drift; business rule flags
    when all 5 come back null."""
    _source_name = "resy_survey"

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    overall: Optional[int] = Field(default=None, ge=0, le=100)
    sentiment: Optional[int] = Field(default=None, ge=0, le=100)
    service: Optional[int] = Field(default=None, ge=0, le=100)
    food: Optional[int] = Field(default=None, ge=0, le=100)
    atmos: Optional[int] = Field(default=None, ge=0, le=100)
    server: Optional[str] = None
    recommend: Optional[int] = Field(default=None, ge=0, le=10,
                                     description="NPS-scale 0-10 promoter score")
    covers: Optional[int] = Field(default=None, ge=0)
    dow: Optional[int] = Field(default=None, ge=0, le=6)
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    text: Optional[list[dict]] = None  # free-text comments

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # Schema-drift signal: all 5 score buckets null = the keyword
        # router missed everything. The schema-drift agent uses this
        # signal too. This is an annotation, not a hard fail — Resy
        # IS giving us SOMETHING (overall + server + covers).
        score_buckets = [self.food, self.service, self.atmos,
                         self.sentiment, self.recommend]
        if all(b is None for b in score_buckets):
            errors.append("all_score_buckets_null: probable Resy schema drift "
                          "(see resy_os_scraper.py _DRIFT_SAMPLES + "
                          "Task 22 drift detector)")
        return errors
