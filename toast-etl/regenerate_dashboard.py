#!/usr/bin/env python3
"""Regenerate the dashboard HTML's inline DATA object from data/*.json.

Run from the repo root (or with --data-dir/--html pointing to them):
    python3 toast-etl/regenerate_dashboard.py \
        --data-dir data \
        --html Method_Co_FB_Performance_Dashboard.html

What it does
------------
1. Parses the current `const DATA = {...};` block out of the HTML.
2. For every `<outlet_id>.json` under --data-dir, overwrites
   `DATA.outlets.<outlet_id>.order_details` with the freshly synced payload.
3. Leaves everything else untouched: sales_summary (2+ years of Toast Sales
   Summary history that order-level sync can't reconstruct), product_mix,
   revenue_centers, sources, id/name/property, and DATA.portfolio.
4. Rewrites the `const DATA = ...;` line in place and writes the HTML back.

This is deliberately NOT a full dashboard rebuild — it's a surgical refresh of
the one slice the nightly Toast order-details sync owns. Everything else has a
different cadence (sales_summary = Sales Summary API or periodic CSV import;
product_mix = Product Mix endpoint).

Exit codes
----------
0 — HTML rewritten (or already matched, no-op).
1 — input file missing, parse error, or output write failed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Matches the single inline DATA declaration. The HTML currently has it as
# `const DATA = {"portfolio": ..., "outlets": ...};` on one long line. We
# capture the JSON object between the first `{` and the matching `};`.
_DATA_RE = re.compile(r"const\s+DATA\s*=\s*(\{.*?\});", re.DOTALL)


def load_html_data(html_path: Path) -> tuple[str, dict[str, Any], tuple[int, int]]:
    """Return (html_text, parsed_data_obj, (match_start, match_end))."""
    text = html_path.read_text(encoding="utf-8")
    m = _DATA_RE.search(text)
    if not m:
        raise RuntimeError(
            f"could not find `const DATA = {{...}};` in {html_path}"
        )
    blob = m.group(1)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        # If the inline DATA isn't strict JSON, it's a structural problem we
        # want to surface — not silently swallow.
        raise RuntimeError(f"DATA block is not valid JSON: {e}") from e
    return text, data, (m.start(1), m.end(1))


# Map data/<file>.json basenames → DATA.outlets.<key> when they differ.
# Toast sync writes JSON files keyed by `outlet_id` from TOAST_OUTLETS config,
# but the dashboard's inline DATA uses different short keys for some outlets.
# Add new aliases here when adding new outlets that have a name mismatch.
_OUTLET_ALIAS = {
    "hiroki_det":    "hirokisan",
    "hiroki_phl":    "hiroki",
    "rosemary_rose": "rosemaryrose",
    "little_wing":   "littlewing",
}


def patch_order_details(
    data: dict[str, Any],
    data_dir: Path,
    verbose: bool = True,
) -> tuple[int, list[str]]:
    """Overwrite DATA.outlets.<oid>.order_details from data/<oid>.json files.

    Returns (num_outlets_updated, list_of_skipped_outlet_ids).
    """
    outlets = data.get("outlets") or {}
    if not isinstance(outlets, dict):
        raise RuntimeError("DATA.outlets is not an object")

    updated = 0
    skipped: list[str] = []
    for json_path in sorted(data_dir.glob("*.json")):
        file_oid = json_path.stem
        if file_oid.startswith("_"):
            # skip markers like _synced_at.json if ever added
            continue
        # Translate file basename to dashboard's outlet key if aliased.
        oid = _OUTLET_ALIAS.get(file_oid, file_oid)
        if oid not in outlets:
            if verbose:
                print(f"  [skip] data/{file_oid}.json — no matching outlet in HTML (tried '{oid}')", file=sys.stderr)
            skipped.append(file_oid)
            continue
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  [skip] data/{oid}.json — parse error: {e}", file=sys.stderr)
            skipped.append(oid)
            continue
        fresh_od = payload.get("order_details")
        if not isinstance(fresh_od, dict):
            print(f"  [skip] data/{oid}.json — no order_details dict", file=sys.stderr)
            skipped.append(oid)
            continue
        outlets[oid]["order_details"] = fresh_od
        # flag the source as connected so the HTML chip renders green
        src = outlets[oid].setdefault("sources", {})
        src["order_details"] = True
        if verbose:
            rcs = ", ".join(fresh_od.keys())
            print(f"  [ok]   {oid} — order_details updated ({rcs})")
        updated += 1
    return updated, skipped


def recompute_portfolio_rollup(data: dict[str, Any]) -> None:
    """Keep portfolio block consistent with current outlets (trivial metadata)."""
    outlets = data.get("outlets") or {}
    data["portfolio"] = {
        "name": data.get("portfolio", {}).get("name") or "Method Co F&B Portfolio",
        "outlets_with_data": sorted(
            oid for oid, o in outlets.items() if not o.get("placeholder")
        ),
        "outlets_total": len(outlets),
    }


def write_html(
    html_path: Path,
    html_text: str,
    data: dict[str, Any],
    span: tuple[int, int],
) -> None:
    """Replace the captured DATA blob with json.dumps(data) and write file."""
    new_blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    start, end = span
    new_text = html_text[:start] + new_blob + html_text[end:]
    # atomic write via .tmp
    tmp = html_path.with_suffix(html_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(html_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Regenerate dashboard HTML from data/*.json")
    p.add_argument("--data-dir", default="data", help="dir containing <outlet>.json files")
    p.add_argument("--html", default="Method_Co_FB_Performance_Dashboard.html", help="dashboard HTML path")
    p.add_argument("--quiet", action="store_true", help="suppress per-outlet progress")
    args = p.parse_args(argv)

    html_path = Path(args.html)
    data_dir = Path(args.data_dir)
    if not html_path.exists():
        print(f"ERROR: html not found at {html_path}", file=sys.stderr)
        return 1
    if not data_dir.exists():
        print(f"ERROR: data dir not found at {data_dir}", file=sys.stderr)
        return 1

    try:
        html_text, data, span = load_html_data(html_path)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"HTML: {html_path}   DATA span: {span[1]-span[0]:,} chars")
    print(f"Patching order_details from {data_dir}/*.json...")
    updated, skipped = patch_order_details(data, data_dir, verbose=not args.quiet)
    print(f"Updated {updated} outlet(s); skipped {len(skipped)}")

    recompute_portfolio_rollup(data)
    write_html(html_path, html_text, data, span)
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
