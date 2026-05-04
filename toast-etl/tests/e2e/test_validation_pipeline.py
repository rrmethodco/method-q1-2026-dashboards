"""End-to-end smoke test of the validation pipeline.

Simulates: a sync writes some good and bad rows, validation runner
emits a _validation/ summary file, the dashboard's _validation_index
on the outlet payload reflects the run.

Does NOT exercise the Edge Function (that's covered by manual smoke
in Tasks 18-25). Just verifies the data-side contract end-to-end.
"""
import json
import sys
from pathlib import Path

# Make the toast-etl package importable
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from schemas.toast_order import ToastOrder
from validation.runner import run_validation


def test_full_pipeline(tmp_path):
    # Set up a fake outlet payload
    outlet_dir = tmp_path / "data"
    outlet_dir.mkdir()
    outlet_path = outlet_dir / "smoketown.json"
    outlet_path.write_text(json.dumps({
        "outlet_id": "smoketown",
        "order_details": {"main": {"daily": []}},
    }))

    # Synthesize 3 good orders + 1 bad
    raw_orders = [
        {
            "guid": f"g-{i}", "voided": False, "deleted": False,
            "openedDate": "2026-05-04T19:00:00Z",
            "closedDate": "2026-05-04T20:00:00Z",
            "numberOfGuests": 2,
            "checks": [{"guid": f"c-{i}", "voided": False, "amount": 50,
                        "tipAmount": 10,
                        "openedDate": "2026-05-04T19:00:00Z",
                        "paidDate": "2026-05-04T20:00:00Z"}],
        }
        for i in range(3)
    ] + [
        {
            "guid": "bad", "voided": False, "deleted": False,
            "openedDate": "2026-05-04T19:00:00Z",
            "checks": [{"guid": "bad-c", "voided": False,
                        "amount": -5,  # negative — fails Field(ge=0)
                        "tipAmount": 0,
                        "openedDate": "2026-05-04T19:00:00Z"}],
        }
    ]

    out = run_validation(
        rows=raw_orders, model_cls=ToastOrder, source="toast_order",
        outlets_touched=["smoketown"], data_dir=outlet_dir,
    )

    # Validation summary file exists
    summary_files = list((outlet_dir / "_validation").glob("toast_order_*.json"))
    assert len(summary_files) == 1
    summary = json.loads(summary_files[0].read_text())
    assert summary["rows_in"] == 4
    assert summary["rows_valid"] == 3
    assert summary["rows_invalid"] == 1

    # Validation errors file exists
    err_files = list((outlet_dir / "_validation_errors").glob("toast_order_*.json"))
    assert len(err_files) == 1

    # Outlet payload now has _validation_index
    payload = json.loads(outlet_path.read_text())
    assert "_validation_index" in payload
    idx = payload["_validation_index"]["toast_order"]
    assert idx["rows_valid"] == 3
    assert idx["rows_invalid"] == 1

    # Caller can still use valid_rows
    assert len(out["valid_rows"]) == 3
