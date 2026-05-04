"""Retention pruner for data/_validation/ and data/_validation_errors/."""
from __future__ import annotations

import time
from pathlib import Path


def prune_old_validation_files(data_dir: Path, keep_days: int = 30) -> int:
    """Remove files older than keep_days from _validation/ and
    _validation_errors/. Returns count removed."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for sub in ("_validation", "_validation_errors"):
        d = data_dir / sub
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--keep-days", type=int, default=30)
    args = ap.parse_args(argv)
    n = prune_old_validation_files(Path(args.data_dir), args.keep_days)
    print(f"pruned {n} files older than {args.keep_days} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
