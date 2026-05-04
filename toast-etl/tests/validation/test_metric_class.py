"""Tests for metric_class loader."""
from validation.metric_class import classify_metric, classify_failure


def test_financial_metric_hard_fails():
    assert classify_metric("net_sales") == "hard_fail"
    assert classify_metric("cogs_pct") == "hard_fail"


def test_soft_signal_annotates():
    assert classify_metric("nps") == "annotate"
    assert classify_metric("dwell_time_min") == "annotate"


def test_unknown_metric_defaults_to_annotate():
    """Unknown metric → safe default = annotate (don't hard-fail surprises)."""
    assert classify_metric("totally_made_up_metric") == "annotate"


def test_failure_pattern_classifies_to_auto_heal():
    assert classify_failure("http_429") == "auto_heal"
    assert classify_failure("workflow_cancelled_concurrency") == "auto_heal"


def test_unknown_failure_defaults_to_alert():
    """Unknown failure pattern → don't auto-heal silently; alert."""
    assert classify_failure("some_new_error_we_havent_seen") == "alert"
