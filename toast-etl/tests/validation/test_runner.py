"""Tests for the validation runner."""
import json
from pathlib import Path
from validation.runner import run_validation


class FakeRow:
    """Stand-in Pydantic model for the runner test (avoids coupling
    the test to any specific source)."""
    def __init__(self, data):
        self.data = data
        if data.get("bad"):
            raise ValueError(f"row invalid: {data}")
    @classmethod
    def model_validate(cls, data):
        return cls(data)
    def model_dump(self, *, mode: str = "python"):
        # Accept (and ignore) `mode` so this stub matches real Pydantic
        # signature when the runner passes mode="json" for datetime → ISO
        # string serialization (added 2026-05-05 to fix the toast-sync regression).
        return self.data
    def validate_business_rules(self):
        if self.data.get("warn"):
            return [f"warn_flag: {self.data['warn']}"]
        return []


def test_runner_writes_validation_file(tmp_path):
    rows = [{"id": 1}, {"id": 2}, {"id": 3, "warn": "low"},
            {"id": 4, "bad": True}]
    out = run_validation(
        rows=rows, model_cls=FakeRow, source="test_src",
        outlets_touched=["lsbr"], data_dir=tmp_path,
    )
    assert out["rows_in"] == 4
    assert out["rows_valid"] == 3   # bad row excluded
    assert out["rows_invalid"] == 1
    assert out["rows_warned"] == 1
    files = list((tmp_path / "_validation").glob("test_src_*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text())
    assert written["source"] == "test_src"


def test_runner_returns_validated_rows(tmp_path):
    rows = [{"id": 1}, {"id": 2, "bad": True}]
    out = run_validation(
        rows=rows, model_cls=FakeRow, source="t", outlets_touched=[],
        data_dir=tmp_path,
    )
    assert len(out["valid_rows"]) == 1
    assert out["valid_rows"][0]["id"] == 1


def test_runner_writes_outlet_index(tmp_path):
    """When `update_outlet_index=True` (default), runner adds a
    `_validation_index.<source>` block to data/<outlet>.json so the
    dashboard's validation panel can fetch it without a directory listing."""
    # Seed an outlet payload
    outlet_path = tmp_path / "smoketown.json"
    outlet_path.write_text(json.dumps({"outlet_id": "smoketown"}))
    rows = [{"id": 1}, {"id": 2, "bad": True}]
    run_validation(
        rows=rows, model_cls=FakeRow, source="t",
        outlets_touched=["smoketown"], data_dir=tmp_path,
    )
    payload = json.loads(outlet_path.read_text())
    assert "_validation_index" in payload
    idx = payload["_validation_index"]["t"]
    assert idx["rows_valid"] == 1
    assert idx["rows_invalid"] == 1


def test_runner_writes_error_samples(tmp_path):
    """When there are invalid rows, a _validation_errors/<source>_<ts>.json
    file is also written with the full error list."""
    rows = [{"id": 1, "bad": True}, {"id": 2, "bad": True}]
    run_validation(
        rows=rows, model_cls=FakeRow, source="errsrc",
        outlets_touched=[], data_dir=tmp_path,
    )
    err_files = list((tmp_path / "_validation_errors").glob("errsrc_*.json"))
    assert len(err_files) == 1
    err_data = json.loads(err_files[0].read_text())
    assert len(err_data["all_errors"]) == 2


def test_runner_no_error_file_when_clean(tmp_path):
    rows = [{"id": 1}, {"id": 2}]
    run_validation(
        rows=rows, model_cls=FakeRow, source="cleansrc",
        outlets_touched=[], data_dir=tmp_path,
    )
    err_dir = tmp_path / "_validation_errors"
    assert not err_dir.exists() or len(list(err_dir.glob("cleansrc_*.json"))) == 0


def test_runner_serializes_datetime_to_iso_string(tmp_path):
    """Regression: PR #91's runner emitted datetime objects from model_dump(),
    which broke every consumer (toast_sync, marginedge_sync, tripleseat_sync,
    forecast_engine, budget_sync, resy_os_scraper) — they all call
    .replace("Z", "+00:00") + fromisoformat() on what they expect to be ISO
    strings. Python 3.12 reports the type clash as "'str' object cannot be
    interpreted as an integer" because datetime.replace() expects an int year
    in arg position 1. The fix uses model_dump(mode="json") to keep dates as
    ISO strings — matching the raw shape from the source API.

    Pinning the behavior here so future refactors of runner.py can't silently
    break the entire ETL again.
    """
    from datetime import datetime, timezone
    from pydantic import BaseModel
    class WithDate(BaseModel):
        id: int
        opened: datetime

        @classmethod
        def model_validate(cls, data):
            return cls(**data)

        def validate_business_rules(self):
            return []

    rows = [{"id": 1, "opened": "2026-05-05T13:00:00.000Z"}]
    out = run_validation(
        rows=rows, model_cls=WithDate, source="dt_test",
        outlets_touched=[], data_dir=tmp_path,
    )
    assert len(out["valid_rows"]) == 1
    opened = out["valid_rows"][0]["opened"]
    # Must be a string, not a datetime — downstream consumers depend on this.
    assert isinstance(opened, str), (
        f"datetime fields must serialize to ISO strings via model_dump(mode='json'), "
        f"got {type(opened).__name__}"
    )
    # Must round-trip through fromisoformat the way toast_sync._as_local_date does.
    parsed = datetime.fromisoformat(opened.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.year == 2026 and parsed.month == 5 and parsed.day == 5
