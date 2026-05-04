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
    def model_dump(self):
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
