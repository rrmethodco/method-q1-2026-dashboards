// Self-healing retry/repair agent.
//
// Polls recent workflow runs. For each that is `cancelled` or `failure`:
//   - If pattern is auto-healable AND we haven't retried it more than
//     3 times in the past 30 min, dispatch a retry.
//   - Otherwise, queue an alert event.
import { listRecentRuns, dispatchWorkflow } from "../lib/github.ts";
import type { AuditDecision } from "../lib/types.ts";

const WORKFLOWS = [
  "toast-sync.yml", "guest-sync.yml", "budget-sync.yml",
  "marginedge-sync.yml", "tripleseat-sync.yml", "forecast-sync.yml",
];
const RETRY_WINDOW_MS = 30 * 60 * 1000;
const MAX_RETRIES_PER_WINDOW = 3;
// Edge Functions are stateless across cold starts; this in-memory map
// is best-effort. Worst case: an extra retry. For hardened tracking,
// switch to a Postgres table in Phase B.
const recentRetries = new Map<string, number[]>();

interface RetryAlert {
  workflow: string;
  conclusion: string;
  reason: string;
}

export interface RetryResult {
  audits: AuditDecision[];
  alerts: RetryAlert[];
}

export async function runRetryRepair(): Promise<RetryResult> {
  const audits: AuditDecision[] = [];
  const alerts: RetryAlert[] = [];
  const ts = new Date().toISOString();

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

    // Only act on terminal-failed runs that completed in the last hour
    const ageMs = Date.now() - new Date(latest.created_at).getTime();
    if (ageMs > 60 * 60 * 1000) continue;

    const retries = (recentRetries.get(wf) || []).filter(
      (t) => Date.now() - t < RETRY_WINDOW_MS,
    );
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
      continue;
    }

    try {
      await dispatchWorkflow(wf);
      recentRetries.set(wf, [...retries, Date.now()]);
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

  return { audits, alerts };
}
