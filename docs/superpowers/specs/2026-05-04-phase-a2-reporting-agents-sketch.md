# Phase A.2 — Reporting Agents (Spec Sketch)

> **Status:** SKETCH ONLY. Not yet brainstormed/approved by Ross. Drafted overnight 2026-05-04 as a thinking-ahead artifact so we can hit the ground running tomorrow if Ross wants to start A.2.

## Intent

Replace the rule-based Weekly Snapshot generator with focused LLM agents per domain. Each agent reads the validated data Phase A.1 now produces (and the schemas it validates against) and writes its section of the snapshot — with cited claims and drill-down links — instead of a hardcoded if/else tree.

## Why now (Phase A.2 not later)

Phase A.1 makes the data trustworthy. Phase A.2 makes the *narrative* trustworthy. The current `renderSnapshotSection` in `Method_Co_FB_Performance_Dashboard.html` (lines 2480-2700) generates Wins / Concerns / Action Items via a long chain of `if (revPctChange < -10)` rule-based logic. That has known failure modes:

- Generic phrasing ("Net sales down -13.7% WoW") that doesn't surface the operational *why*
- Misses anomalies that don't match a hardcoded threshold
- Can't cross-reference (e.g., "COGS spike Sat correlates with the $12K Sysco receiving error you flagged Friday")
- No way to ask follow-up questions

LLM agents trained on the validated data can do all four.

## Goals (locked from Project A scope; Ross approved during 2026-05-04 brainstorm)

1. **Sales agent** — writes the Net Sales / Covers / RevPASH / Avg Guest Spend section. Cites: Toast `order_details`, helixo-2 forecast, Sage budget. Surfaces: WoW + STLY deltas, period-over-period drift, mix shift (PMIX category vs prior).
2. **Labor agent** — writes Labor $ / Hours / OT / SPMH / FOH-BOH balance section. Cites: Toast `labor.daily`, position rollup. Surfaces: OT spikes by position, SPMH outliers, prime cost % vs target.
3. **COGS agent** — writes Food Cost / Beer / Wine / Liquor / NA Beverage section. Cites: MarginEdge invoices, line items by `cogs_bucket`. Surfaces: vendor mix shifts, ideal-vs-actual gaps, line-items-sum-mismatch warnings (already flagged by Phase A.1 validation).
4. **Guest agent** — writes NPS / Promoters / Detractors / Reviews section. Cites: Resy surveys, Google reviews. Surfaces: trending themes from comments (5 most-mentioned), score deltas, server outliers.
5. **Action Items composer** — reads ALL FOUR domain agents' output + cross-references against operational rules (Method's `core values`, severity thresholds) → writes the prescriptive Action Items list. Each item gets a tag: `Operations`, `RevenueOps`, `People`, `Vendor`.
6. **GM email author** — composes a 200-300 word daily digest combining the above. Cites: every claim has a [link to dashboard tab/period].

## Non-goals (this phase)

- Operator-facing chat ("Why was Saturday COGS up?") — that's A.3
- Forecast methodology overhaul — A.4
- Real-time alerting changes (A.1 already handles this)

## Architecture

**Where do the agents run?**
- Same Supabase Edge Function as A.1 (one new function: `agent-worker-snapshot`)
- Triggered weekly (Sunday 10am ET) via pg_cron, AND on-demand via dashboard button
- Reads validated data from Supabase Storage (`validation/` bucket — populated by Phase A.1)
- Writes generated snapshot content to a new bucket `snapshots/` as JSON keyed by `{outlet}_{week_end}`
- Dashboard reads the snapshot JSON and renders it in the existing layout

**How does each agent stay focused?**
- Sub-prompts per domain: each agent gets ONLY the data slice it needs (Sales agent doesn't see Resy data)
- Pydantic-style output schemas: each agent must return JSON matching a defined shape (e.g., `{wins: [...], concerns: [...], metrics: {...}}`)
- Citation contract: every claim must include a `data_ref` field pointing to the source row/aggregate

**Cost ceiling:**
- 11 outlets × 1 weekly snapshot × 6 agents (4 domain + composer + email) = 66 agent calls/week
- ~2k tokens/call avg with Sonnet ≈ ~$10/week ≈ $40/mo
- Add daily email author = ~7 outlets × 1 call/day × 30 days = 210 calls/mo at ~$1k tokens = ~$5/mo
- **Total: ~$45/mo** for the reporting layer

## Data contracts

### Sales agent input

```typescript
interface SalesAgentInput {
  outlet_id: string;
  week_end: string;             // YYYY-MM-DD
  current_week: {
    daily: Daily[];             // 7 days from order_details.main.daily
    pmix?: PMIXEntry[];         // optional product-mix breakdown
  };
  prior_week: { daily: Daily[] };
  same_week_ly: { daily: Daily[] };
  forecast: { daily: Forecast[] };  // helixo-2
  budget: { daily: BudgetLine[] };  // Sage
  validation_status: ValidationIndex;  // is the data trustworthy?
}
```

### Sales agent output (JSON-forced)

```typescript
interface SalesAgentOutput {
  metrics: {
    net_sales: KPIBlock;
    covers: KPIBlock;
    revpash?: KPIBlock;
    avg_guest_spend?: KPIBlock;
    // each KPIBlock: { value: number, vs_forecast_pct, vs_stly_pct, vs_budget_pct, ... }
  };
  wins: NarrativeItem[];
  concerns: NarrativeItem[];
}

interface NarrativeItem {
  text: string;            // human-facing, ≤140 chars
  data_ref: {              // citation
    source: "toast" | "helixo2" | "sage";
    period: string;        // e.g., "WE 2026-05-04"
    metric: string;
    value: number;
  };
  severity: "info" | "warn" | "urgent";
}
```

(Same shape for labor / cogs / guest agents, with their respective metric sets.)

### Action Items composer input

All 4 domain agents' outputs + Method's operational rules YAML (e.g. `cogs_pct > 32% → flag urgent`).

### Action Items composer output

```typescript
interface ActionItem {
  text: string;            // ≤200 chars; starts with verb
  category: "Operations" | "RevenueOps" | "People" | "Vendor";
  urgency: "info" | "high" | "urgent";
  cites: NarrativeItem[];  // links to the domain agent claims that justify this item
}
```

## Risk + mitigation

| Risk | Mitigation |
|---|---|
| LLM hallucinates a metric that isn't in the data | Pydantic-style output schema + reject any output where `data_ref.value` doesn't match the actual data; auto-retry once with stricter prompt |
| Generated narrative drifts from Method's voice / brand standards | Brand prompt block referencing the core values + tone guide; run output through a brand-check pass before saving |
| Agent calls cost balloon | Cap weekly run; cache per `(outlet, week_end)`; only invalidate cache on data changes |
| LLM goes down / Anthropic outage | Fall back to current rule-based generator (don't remove it; keep both for 90 days, then deprecate after confidence built) |
| Wins/Concerns include something operationally wrong | Operator can flag via 👎 button on the snapshot; fed back into the agent's prompt as a few-shot example |

## Phased rollout (within A.2)

1. **Week 1:** Build Sales agent only. Run side-by-side with current rule-based generator. Compare outputs across 3 outlets.
2. **Week 2:** Add Labor + COGS agents. Same side-by-side.
3. **Week 3:** Add Guest agent + Action Items composer.
4. **Week 4:** Add GM email author + automatic daily send (subject to Ross's go-ahead).
5. **Week 5:** Deprecate rule-based generator (or keep as fallback).

## Open decisions (would resolve in brainstorm)

1. **One agent per domain, or one master prompt?** — Master simpler; per-agent more focused. Recommend per-agent.
2. **Cache invalidation:** what triggers a snapshot regen? Every new sync run? Daily? Weekly + on-demand?
3. **Where does the brand-voice spec live?** — `config/brand_voice.yml` analogous to `metric_classes.yml`?
4. **Does the GM email author run nightly, weekly, or only Mondays?** Ross's preference.
5. **Confidence display:** Should the dashboard show a "confidence" indicator on agent-generated content (similar to `ai_confidence` on forecasts)?

## Estimated effort

3-4 sprints. Roughly:
- Sprint 1: Schemas for agent I/O + Sales agent + caching (1 sprint)
- Sprint 2: Labor + COGS + Guest agents + Action Items composer (1 sprint)
- Sprint 3: GM email author + daily send pipeline + brand-voice config (1 sprint)
- Sprint 4: Side-by-side validation + cutover (1 sprint, partly soak time)

---

**Next step:** brainstorm cycle to formalize this sketch into a proper spec, lock open decisions, and get Ross's approval.
