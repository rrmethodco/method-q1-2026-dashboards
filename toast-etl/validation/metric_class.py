"""Loader for config/metric_classes.yml.

Returns:
  classify_metric(name)  -> "hard_fail" | "annotate" (default annotate)
  classify_failure(name) -> "auto_heal" | "alert"    (default alert)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("missing dependency: pip install pyyaml")


CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "metric_classes.yml"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def classify_metric(metric_name: str) -> str:
    """Returns 'hard_fail' or 'annotate'. Default: 'annotate'."""
    cfg = _load()
    for group_name, group in cfg.items():
        if not isinstance(group, dict):
            continue
        if metric_name in (group.get("metrics") or []):
            return group.get("class", "annotate")
    return "annotate"


def classify_failure(pattern: str) -> str:
    """Returns 'auto_heal' or 'alert'. Default: 'alert'."""
    cfg = _load()
    for group_name, group in cfg.items():
        if not isinstance(group, dict):
            continue
        if pattern in (group.get("patterns") or []):
            cls = group.get("class", "")
            if cls == "auto_heal":
                return "auto_heal"
    return "alert"
