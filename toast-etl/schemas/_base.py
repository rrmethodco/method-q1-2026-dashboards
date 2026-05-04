"""Shared base for all source-row Pydantic models.

Every source row gets a Pydantic V2 model that:
1. Declares required + optional fields with types
2. Declares value bounds (e.g. amount >= 0)
3. Implements validate_business_rules() for cross-field invariants

The validation runner (toast-etl/validation/runner.py) pipes raw rows
through these models and writes a _validation/<source>_<ts>.json file
per run so the agent worker can detect drift, anomalies, and failures.
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict


class SourceRow(BaseModel):
    """Base for all source-row models.

    Subclasses MUST override _source_name. Subclasses SHOULD override
    validate_business_rules() if cross-field invariants apply.
    """
    model_config = ConfigDict(
        # Permissive ingestion: don't crash on extra Resy fields, etc.
        # If we silently dropped them and a critical field showed up
        # under an unexpected name, the schema-drift detector would
        # eventually flag it (Phase A.1 scope) — but during validation
        # we want to be tolerant.
        extra="allow",
        # Coerce JSON strings to int/float when shape is unambiguous.
        # Toast and MarginEdge both occasionally return numeric fields
        # as strings.
        str_strip_whitespace=True,
        validate_assignment=False,
    )

    _source_name: str = "unknown"

    def validate_business_rules(self) -> list[str]:
        """Return list of human-readable error strings.

        Empty list = all invariants hold. Subclasses override to add
        cross-field checks. Caller (validation runner) decides whether
        to fail or annotate based on the source's metric class.
        """
        return []
