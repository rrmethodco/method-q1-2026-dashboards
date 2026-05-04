// Self-healing retry/repair agent.
//
// Polls recent workflow runs. For each that is `cancelled` or `failure`:
//   - If we haven't retried it more than 3 times in the past 30 min,
//     dispatch a retry.
//   - Otherwise, queue an alert event.
//
// Retry-budget state is persisted to Supabase Storage
// (`validation/_state/retry_repair.json`). Pre-fix the budget map was
// in-memory and reset on every Edge Function cold start, which meant
// a persistently-failing workflow would be re-dispatched on every
// 5-min tick instead of capping at 3 per 30min (integration review
// 2026-05-04).
import { SupabaseClient } from "@supabase/supabase-js";
import { listRecentRuns, dispatchWorkflow } from "../lib/github.ts";
import { readState, writeState } from "../lib/state.ts";
import type { AuditDecision } from "../lib/types.ts";

const WORKFLOWS = [
  "toast-sync.yml", "guest-sync.yml", "budget-sync.yml",
  "marginedge-sync.yml", "tripleseat-sync.yml", "forecast-sync.yml",
];
const RETRY_WINDOW_MS = 30 * 60 * 1000;
const MAX_RETRIES_PER_WINDOW = 3;
const AGENT = "retry_repair";

interface RetryState {
  // workflow file → list of dispatch timestamps (ms-since-epoch)
  recent_retries: Record<string, number[]>;
  // workflow file → last run id we acted on (so we don't re-dispatch
  // for the same already-handled failed run on subsequent ticks)
  last_handled_run: Record<string, number>;
}

interface RetryAlert {
  workflow: string;
  conclusion: string;
  reason: string;
}

export interface RetryResult {
  audits: AuditDecision[];
  alerts: RetryAlert[];
}

export async function runRetryRepair(supabase: SupabaseClient): Promise<RetryResult> {
  const audits: AuditDecision[] = [];
  const alerts: RetryAlert[] = [];
  const ts = new Date().toISOString();

  // Load persistent state. Prune retry timestamps older than the window.
  const state = await readState<RetryState>(supabase, AGENT, {
    recent_retries: {},
    last_handled_run: {},
  });
  const cutoff = Date.now() - RETRY_WINDOW_MS;
  const recentRetries: Record<string, number[]> = {};
  for (const [wf, ts_list] of Object.entries(state.recent_retries)) {
    const fresh = (ts_list || []).filter((t) => t >= cutoff);
    if (fresh.length > 0) recentRetries[wf] = fresh;
  }
  const lastHandled = { ...state.last_handled_run };

  for (const wf of WORKFLOWS) {
    let runs;
    try {
      runs = await listRecentRuns(wf, 3);
    } catch (e) {
      audits.push({
        ts, agent: "retry_repair", source: wf,
        decision: "list_failed",
        details: { error: String(e) },
        action_taken: "skipped this workflow this cycle",
      });
      continue;
    }
    const latest = runs[0];
    if (!latest) continue;
    if (latest.conclusion !== "cancelled" && latest.conclusion !== "failure") continue;

    // Idempotency: don't re-act on a run we already retried before.
    if (lastHandled[wf] === latest.id) continue;

    // Only act on terminal-failed runs that completed in the last hour
    const ageMs = Date.now() - new Date(latest.created_at).getTime();
    if (ageMs > 60 * 60 * 1000) continue;

    const retries = recentRetries[wf] || [];
    if (retries.length >= MAX_RETRIES_PER_WINDOW) {
      alerts.push({
        workflow: wf,
        conclusion: latest.conclusion!,
        reason: `exhausted retry budget (${retries.length}/${MAX_RETRIES_PER_WINDOW} in last 30min)`,
      });
      audits.push({
        ts, agent: "retry_repair", source: wf,
        decision: "retry_budget_exhausted",
        details: { retries: retries.length, conclusion: latest.conclusion },
        action_taken: "alert dispatched, no auto-heal",
      });
      lastHandled[wf] = latest.id;  // mark handled so we don't retry-alert next cycle
      continue;
    }

    try {
      await dispatchWorkflow(wf);
      recentRetries[wf] = [...retries, Date.now()];
      lastHandled[wf] = latest.id;
      audits.push({
        ts, agent: "retry_repair", source: wf,
        decision: "auto_retry_dispatched",
        details: {
          prior_conclusion: latest.conclusion,
          prior_run_id: latest.id,
        },
        action_taken: `dispatched ${wf} (retry ${retries.length + 1}/${MAX_RETRIES_PER_WINDOW})`,
      });
    } catch (e) {
      alerts.push({
        workflow: wf, conclusion: latest.conclusion!,
        reason: `retry dispatch failed: ${String(e)}`,
      });
    }
  }

  // Persist updated state.
  await writeState(supabase, AGENT, {
    recent_retries: recentRetries,
    last_handled_run: lastHandled,
  });

  return { audits, alerts };
}
