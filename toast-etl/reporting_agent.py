"""Reporting agent — generates LLM-narrated per-outlet weekly snapshots.

Run nightly from .github/workflows/agentic-reporting.yml after the four
data-sync workflows commit fresh data. For each outlet:

  1. Reads the outlet's payload from data/<outlet>.json
  2. Computes the latest completed business week (Mon–Sun) hard numbers
  3. Sends those numbers to Anthropic Sonnet with a structured prompt
     that returns per-outlet Wins / Concerns / Action Items
  4. Writes data/_snapshots/<outlet>_<we>.json
  5. Writes data/_snapshots/_index.json so the dashboard can list all
     available snapshots
  6. Commits all changes via the workflow's auto-commit step

The dashboard's renderSnapshotSection() reads _snapshots/<outlet>_<we>.json
when present and falls back to the rule-based generator otherwise.

Cost: ~11 outlets × ~3K input tokens × $3/1M = $0.10/run × 30 runs/mo = ~$3/mo.

Auth: ANTHROPIC_API_KEY GH Actions secret. Same key as the Edge Function's
drift_detector. If unset, the script logs and exits 0 (graceful degradation —
dashboard falls back to rule-based snapshot).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic

MODEL = "claude-sonnet-4-5"  # latest stable Sonnet
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_DIR = DATA_DIR / "_snapshots"

# Outlets to brief. Mirrors agent-worker/agents/banner_writer.ts so the
# coverage stays in sync. Add new outlet → add here AND in banner_writer.ts.
OUTLETS = [
    "lsbr", "mulherins", "kampers", "lowland", "vessel", "anthology",
    "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin",
]


def latest_completed_week(today: date | None = None) -> tuple[date, date, str]:
    """Return (Mon, Sun, week-ending-label) for the most recent completed week.

    A "completed week" is the most recent Mon–Sun where Sunday is in the past.
    If today is Monday, that means yesterday's Sunday closes the prior week.
    Example: today = Tue 2026-05-05 → completed week = Mon 4/27 → Sun 5/3.
    """
    today = today or date.today()
    # Find the most recent Sunday strictly before today.
    days_since_sunday = (today.weekday() + 1) % 7  # Mon=0, Sun=6 → +1 mod 7 → 1, 2, ..., 0
    last_sunday = today - timedelta(days=days_since_sunday or 7)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday, last_sunday.isoformat()


def _sum_daily(daily_rows: list[dict], start: str, end: str, fields: tuple[str, ...]) -> dict[str, float]:
    """Sum named fields across daily rows whose date is in [start, end]."""
    out = {f: 0.0 for f in fields}
    for r in daily_rows or []:
        if start <= (r.get("date") or "") <= end:
            for f in fields:
                v = r.get(f)
                if v is not None:
                    out[f] += float(v)
    return out


def _outlet_week_metrics(payload: dict[str, Any], wb_start: str, wb_end: str) -> dict[str, Any]:
    """Aggregate one outlet's hard numbers for the given week window.

    Returns a dict shaped for the LLM prompt: revenue, labor, COGS, guests,
    discounts, ticket time, plus week-over-week + YoY deltas computed from
    prior weeks.
    """
    od = payload.get("order_details") or {}
    # Sum across all RCs on each day, then sum to week.
    by_date: dict[str, dict[str, float]] = {}
    for _rc, b in od.items():
        for r in b.get("daily", []) or []:
            d = r.get("date") or ""
            slot = by_date.setdefault(d, {"net_sales": 0.0, "amount": 0.0,
                                          "discount": 0.0, "guests": 0.0,
                                          "orders": 0.0})
            slot["net_sales"] += float(r.get("net_sales") or 0)
            slot["amount"]    += float(r.get("amount") or 0)
            slot["discount"]  += float(r.get("discount") or 0)
            slot["guests"]    += float(r.get("guests") or 0)
            slot["orders"]    += float(r.get("orders") or 0)

    cur = {"net_sales": 0.0, "amount": 0.0, "discount": 0.0, "guests": 0.0, "orders": 0.0}
    days_with_revenue = 0
    for d, m in by_date.items():
        if wb_start <= d <= wb_end:
            for k, v in m.items():
                cur[k] += v
            if m["net_sales"] > 0:
                days_with_revenue += 1

    # Prior-week (1 week back) and same-week-last-year (52 weeks back).
    pw_start = (date.fromisoformat(wb_start) - timedelta(days=7)).isoformat()
    pw_end   = (date.fromisoformat(wb_end)   - timedelta(days=7)).isoformat()
    yoy_start = (date.fromisoformat(wb_start) - timedelta(days=364)).isoformat()
    yoy_end   = (date.fromisoformat(wb_end)   - timedelta(days=364)).isoformat()

    prior = {"net_sales": 0.0, "guests": 0.0, "discount": 0.0, "orders": 0.0}
    yoy   = {"net_sales": 0.0, "guests": 0.0}
    for d, m in by_date.items():
        if pw_start <= d <= pw_end:
            for k in prior: prior[k] += m.get(k, 0.0)
        if yoy_start <= d <= yoy_end:
            for k in yoy: yoy[k] += m.get(k, 0.0)

    # Labor (outlet-wide, no RC dim)
    lab = (payload.get("labor") or {}).get("daily") or []
    cur_lab = _sum_daily(lab, wb_start, wb_end,
                         ("regular_hours", "overtime_hours", "regular_cost",
                          "overtime_cost", "total_cost"))
    cur_lab["total_hours"] = cur_lab["regular_hours"] + cur_lab["overtime_hours"]
    pw_lab  = _sum_daily(lab, pw_start, pw_end,
                         ("regular_hours", "overtime_hours", "total_cost"))
    pw_lab["total_hours"] = pw_lab["regular_hours"] + pw_lab["overtime_hours"]

    # COGS (MarginEdge weekly_rollup if present)
    cogs_block = payload.get("cogs") or {}
    cur_cogs = 0.0; pw_cogs = 0.0
    for r in cogs_block.get("weekly_rollup") or []:
        ws = r.get("week_start") or ""
        if ws == wb_start:
            cur_cogs = float(r.get("cogs_total") or r.get("total") or 0)
        elif ws == pw_start:
            pw_cogs = float(r.get("cogs_total") or r.get("total") or 0)

    # Guest experience (Resy surveys if present)
    surveys = (payload.get("guest") or {}).get("surveys") or []
    cur_surveys = [s for s in surveys if wb_start <= (s.get("date_completed") or "") <= wb_end]
    nps_scores = [int(s.get("nps")) for s in cur_surveys
                  if s.get("nps") is not None and str(s.get("nps")).lstrip("-").isdigit()]
    nps = (sum(nps_scores) / len(nps_scores)) if nps_scores else None

    return {
        "current_week": {
            "net_sales": round(cur["net_sales"], 2),
            "guests": int(cur["guests"]),
            "orders": int(cur["orders"]),
            "discount": round(cur["discount"], 2),
            "discount_pct": round(cur["discount"] / cur["net_sales"] * 100, 2)
                            if cur["net_sales"] else 0,
            "labor_cost": round(cur_lab["total_cost"], 2),
            "labor_hours": round(cur_lab["total_hours"], 1),
            "labor_pct_of_sales": round(cur_lab["total_cost"] / cur["net_sales"] * 100, 2)
                                  if cur["net_sales"] else 0,
            "cogs": round(cur_cogs, 2),
            "cogs_pct_of_sales": round(cur_cogs / cur["net_sales"] * 100, 2)
                                 if cur["net_sales"] else 0,
            "avg_check": round(cur["net_sales"] / cur["orders"], 2) if cur["orders"] else 0,
            "avg_guest_spend": round(cur["net_sales"] / cur["guests"], 2)
                               if cur["guests"] else 0,
            "days_with_revenue": days_with_revenue,
            "nps": nps,
            "nps_n": len(nps_scores),
        },
        "prior_week": {
            "net_sales": round(prior["net_sales"], 2),
            "guests": int(prior["guests"]),
            "labor_cost": round(pw_lab["total_cost"], 2),
            "cogs": round(pw_cogs, 2),
        },
        "year_over_year": {
            "net_sales": round(yoy["net_sales"], 2),
            "guests": int(yoy["guests"]),
        },
    }


def _build_prompt(outlet_name: str, outlet_property: str,
                  wb_start: str, wb_end: str, metrics: dict) -> str:
    """Construct the per-outlet briefing prompt for Sonnet.

    The prompt enforces JSON output so the dashboard can render it without
    parsing free-form prose. Schema is enforced via instruction + an
    example block; we still defensively parse + validate downstream.
    """
    return f"""You are an experienced restaurant operations advisor briefing Ross Richardson, EVP Finance & Accounting at Method Co. Your audience is a financial operator who manages 10 hotels and 11 F&B concepts; he reads dozens of these briefings every Monday morning.

Outlet: {outlet_name} ({outlet_property})
Week ending: {wb_end} (Mon {wb_start} through Sun {wb_end})

Hard numbers for this week vs. prior week vs. same week last year:

{json.dumps(metrics, indent=2)}

Generate a concise weekly briefing as STRICT JSON matching this schema:

{{
  "headline": "<one sentence — the single most important takeaway, leading with the number>",
  "wins": [
    {{"text": "<one sentence per win, lead with the metric>", "metric": "<which KPI>", "magnitude": "<dollar or % delta>"}}
  ],
  "concerns": [
    {{"text": "<one sentence per concern, lead with the metric>", "metric": "<which KPI>", "magnitude": "<dollar or % delta>", "severity": "low|medium|high"}}
  ],
  "action_items": [
    {{"action": "<imperative verb-led action>", "owner": "GM|F&B Director|Chef|Service Lead|Marketing|—", "by_when": "<when, e.g. 'this week', 'before Friday'>", "rationale": "<one short clause>"}}
  ],
  "outlook": "<one sentence on next week's risk or opportunity given current pacing>"
}}

Rules — operator-style, no fluff:
- Maximum 3 wins, 3 concerns, 3 action items. Rank by magnitude/impact.
- Lead with numbers. "Net sales \\$45K, +12% WoW" not "Sales were strong."
- Wins/concerns must be derived from THIS week's metrics vs. prior week or YoY. Don't editorialize.
- Action items must be owned and time-bounded. If you can't name an owner, use "—".
- USALI for hotel logic doesn't apply here (these are F&B concepts) — use restaurant ops framing.
- Don't repeat numbers between sections. If something's a concern, don't also list as a win.
- Method Co values: gritty, customer-centric, gets it done. Tone matches Ross's: direct, operator-style, no throat-clearing.
- If a KPI is null/zero (e.g., NPS missing), don't fabricate. Say "N/A" or omit.
- Output ONLY the JSON object. No prose before or after.
"""


def _call_anthropic(client: anthropic.Anthropic, prompt: str) -> dict:
    """Send the prompt to Sonnet and parse the JSON response.

    Defensive parse: Sonnet sometimes wraps JSON in markdown fences or
    adds prefatory prose despite the "ONLY the JSON object" instruction.
    Strip fences and trim before json.loads.
    """
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop the first line (```json or ```) and the last (```)
        text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
    # If there's leading prose before the JSON object, find the first {
    if not text.startswith("{"):
        idx = text.find("{")
        if idx >= 0:
            text = text[idx:]
    return json.loads(text)


def generate_for_outlet(client: anthropic.Anthropic, outlet_id: str,
                        wb_start: str, wb_end: str) -> dict | None:
    """Generate one outlet's briefing. Returns None on missing data."""
    payload_path = DATA_DIR / f"{outlet_id}.json"
    if not payload_path.exists():
        sys.stderr.write(f"[{outlet_id}] no data/{outlet_id}.json — skipping\n")
        return None
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    metrics = _outlet_week_metrics(payload, wb_start, wb_end)
    if metrics["current_week"]["net_sales"] == 0:
        sys.stderr.write(f"[{outlet_id}] no revenue in {wb_start}..{wb_end} — skipping\n")
        return None

    name = payload.get("name") or outlet_id
    prop = payload.get("property") or ""
    prompt = _build_prompt(name, prop, wb_start, wb_end, metrics)

    sys.stdout.write(f"[{outlet_id}] briefing Sonnet ({MODEL})...\n")
    briefing = _call_anthropic(client, prompt)

    # Wrap with metadata so downstream consumers (dashboard, email) can
    # show source-of-truth + provenance + the numbers themselves.
    return {
        "outlet_id": outlet_id,
        "outlet_name": name,
        "property": prop,
        "week_start": wb_start,
        "week_end": wb_end,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "model": MODEL,
        "metrics": metrics,
        "briefing": briefing,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate per-outlet LLM weekly snapshots.")
    p.add_argument("--outlet", help="generate just this outlet (default: all)")
    p.add_argument("--week-end",
                   help="week ending date (YYYY-MM-DD Sunday); default = latest completed week")
    p.add_argument("--dry-run", action="store_true",
                   help="print briefings to stdout instead of writing files")
    args = p.parse_args(argv)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.stderr.write("ANTHROPIC_API_KEY not set — exiting 0 (dashboard will fall "
                         "back to rule-based snapshot)\n")
        return 0

    if args.week_end:
        wb_end = date.fromisoformat(args.week_end)
        wb_start = wb_end - timedelta(days=6)
    else:
        wb_start, wb_end_d, _ = latest_completed_week()
        wb_end = wb_end_d
    wb_start_iso = wb_start.isoformat()
    wb_end_iso = wb_end.isoformat()

    outlets = [args.outlet] if args.outlet else OUTLETS
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic(api_key=api_key)

    results: list[dict] = []
    failed: list[str] = []
    for outlet_id in outlets:
        try:
            snap = generate_for_outlet(client, outlet_id, wb_start_iso, wb_end_iso)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[{outlet_id}] briefing failed: {e}\n")
            failed.append(outlet_id)
            continue
        if snap is None:
            continue
        results.append(snap)
        out_path = SNAPSHOT_DIR / f"{outlet_id}_{wb_end_iso}.json"
        if args.dry_run:
            sys.stdout.write(f"[{outlet_id}] would write {out_path}:\n")
            sys.stdout.write(json.dumps(snap, indent=2) + "\n")
        else:
            out_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False),
                                encoding="utf-8")
            sys.stdout.write(f"[{outlet_id}] wrote {out_path}\n")
            # Also inject the briefing into the outlet's main payload so the
            # dashboard can render it without a separate fetch. Keep the
            # historical snapshot file too for the morning briefing email +
            # the per-outlet snapshot index.
            outlet_path = DATA_DIR / f"{outlet_id}.json"
            if outlet_path.exists():
                try:
                    payload = json.loads(outlet_path.read_text(encoding="utf-8"))
                    payload["briefing"] = {
                        "week_end": snap["week_end"],
                        "week_start": snap["week_start"],
                        "generated_at": snap["generated_at"],
                        "model": snap["model"],
                        **(snap.get("briefing") or {}),
                    }
                    tmp = outlet_path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
                    tmp.replace(outlet_path)
                    sys.stdout.write(f"[{outlet_id}] injected briefing into {outlet_path.name}\n")
                except (json.JSONDecodeError, OSError) as e:
                    sys.stderr.write(f"[{outlet_id}] briefing inject failed: {e}\n")

    # Index file: dashboard reads this to know which snapshots exist.
    if not args.dry_run and results:
        index_path = SNAPSHOT_DIR / "_index.json"
        existing = {}
        if index_path.exists():
            try:
                existing = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        for snap in results:
            key = snap["outlet_id"]
            existing.setdefault(key, []).append({
                "week_end": snap["week_end"],
                "generated_at": snap["generated_at"],
                "headline": (snap.get("briefing") or {}).get("headline", ""),
                "path": f"_snapshots/{snap['outlet_id']}_{snap['week_end']}.json",
            })
            # Keep only the last 12 weeks per outlet (avoid unbounded growth)
            existing[key] = sorted(
                existing[key], key=lambda r: r["week_end"], reverse=True,
            )[:12]
        index_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        sys.stdout.write(f"updated {index_path} ({len(results)} new entries)\n")

    sys.stdout.write(f"\nGenerated {len(results)} briefings, "
                     f"{len(failed)} failed: {failed or 'none'}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
