"""Tests for validation file retention pruner."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from validation.retention import prune_old_validation_files


def _make_file(p: Path, age_days: int):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ran_at": "x"}))
    age = datetime.now(timezone.utc) - timedelta(days=age_days)
    ts = age.timestamp()
    os.utime(p, (ts, ts))


def test_prunes_files_older_than_30_days(tmp_path):
    val = tmp_path / "_validation"
    _make_file(val / "src1_old.json", 40)
    _make_file(val / "src2_keep.json", 10)
    _make_file(val / "src3_keep.json", 29)
    removed = prune_old_validation_files(tmp_path, keep_days=30)
    assert removed == 1
    assert not (val / "src1_old.json").exists()
    assert (val / "src2_keep.json").exists()
    assert (val / "src3_keep.json").exists()


def test_idempotent_when_nothing_to_prune(tmp_path):
    val = tmp_path / "_validation"
    _make_file(val / "fresh.json", 1)
    assert prune_old_validation_files(tmp_path, keep_days=30) == 0
