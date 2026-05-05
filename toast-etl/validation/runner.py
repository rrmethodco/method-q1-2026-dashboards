"""Validation runner — pipes raw rows through Pydantic models, writes
a per-run summary file the agent worker consumes.

Returns a dict with both the summary (for caller logging) and the
valid_rows list (the caller uses these going forward; invalid rows
are dropped from the data payload but logged in _validation_errors).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Type

from .pii_redact import redact_pii


def run_validation(
    rows: list[dict],
    model_cls: Type,
    source: str,
    outlets_touched: list[str],
    data_dir: Path,
    schema_version: str = "v1",
    update_outlet_index: bool = True,
) -> dict[str, Any]:
    """Validate raw rows against model_cls.

    Args:
        rows: raw dict rows from the sync
        model_cls: Pydantic model class with model_validate()
                   and validate_business_rules()
        source: short identifier (e.g. "toast_order", "resy_survey")
        outlets_touched: list of outlet ids this run wrote to
        data_dir: project data/ dir
        schema_version: bump when the model is materially changed
        update_outlet_index: if True, write a `_validation_index` block
                             into each outlet's payload (so the dashboard
                             can surface the latest run without a
                             directory listing)

    Returns:
        dict with summary stats AND valid_rows for the caller to use
    """
    valid_rows: list[dict] = []
    errors: list[dict] = []
    warnings: list[dict] = []

    for i, row in enumerate(rows):
        try:
            m = model_cls.model_validate(row)
        except Exception as e:
            errors.append({
                "row_offset": i,
                "code": "model_validation_error",
                "message": str(e)[:500],
                "row_keys": sorted(row.keys()) if isinstance(row, dict) else [],
                "row_redacted": redact_pii(row) if isinstance(row, dict) else None,
            })
            continue
        rule_errs = m.validate_business_rules()
        if rule_errs:
            warnings.append({
                "row_offset": i,
                "rules": rule_errs,
                "row_keys": sorted(row.keys()) if isinstance(row, dict) else [],
            })
        # mode="json" serializes datetime fields back to ISO strings (matching
        # the raw shape from the source API). The default model_dump() returns
        # native datetime objects, which the downstream consumers in
        # toast_sync.py / marginedge_sync.py / tripleseat_sync.py /
        # resy_os_scraper.py / forecast_engine.py / budget_sync.py all break on:
        # they call .replace("Z", "+00:00") and fromisoformat(...) on what they
        # expect to be ISO strings. Python 3.12 reports the datetime.replace()
        # signature mismatch as "'str' object cannot be interpreted as an
        # integer" (because datetime.replace expects an int year as positional
        # arg #1), which silently broke the toast-sync nightly from PR #91
        # merge (2026-05-04) until Ross caught downstream symptoms 2026-05-05.
        valid_rows.append(m.model_dump(mode="json"))

    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ts = ran_at.replace(":", "").replace("-", "").replace("+0000", "Z")

    summary = {
        "source": source,
        "schema_version": schema_version,
        "ran_at": ran_at,
        "rows_in": len(rows),
        "rows_valid": len(valid_rows),
        "rows_invalid": len(errors),
        "rows_warned": len(warnings),
        "outlets_touched": outlets_touched,
        "errors_sample": errors[:10],
        "warnings_sample": warnings[:10],
    }

    val_dir = data_dir / "_validation"
    val_dir.mkdir(parents=True, exist_ok=True)
    out_path = val_dir / f"{source}_{ts}.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    if errors:
        err_dir = data_dir / "_validation_errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        err_path = err_dir / f"{source}_{ts}.json"
        err_path.write_text(json.dumps({
            "source": source, "ran_at": ran_at,
            "all_errors": errors,
        }, indent=2, default=str), encoding="utf-8")

    if update_outlet_index:
        for oid in outlets_touched:
            outlet_path = data_dir / f"{oid}.json"
            if not outlet_path.exists():
                continue
            try:
                payload = json.loads(outlet_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            payload.setdefault("_validation_index", {})[source] = {
                "ran_at": ran_at,
                "rows_in": len(rows),
                "rows_valid": len(valid_rows),
                "rows_invalid": len(errors),
                "rows_warned": len(warnings),
            }
            tmp = outlet_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(outlet_path)

    return {
        **summary,
        "valid_rows": valid_rows,
        "summary_path": str(out_path),
    }
