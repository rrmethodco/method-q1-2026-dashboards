// agent-worker — Edge Function entry point.
//
// Triggered by pg_cron every 5 minutes. Each invocation:
//   1. Reads the latest data/_validation/*.json files (synced to a
//      Supabase Storage bucket by the GH Actions workflows)
//   2. Routes through the agent loops (drift, anomaly, retry, alert)
//   3. Writes back banner state + appends to audit log
//
// Phase A.1 — drift detector + anomaly detector wired (Tasks 20-22).
// retry/repair + alert dispatcher + banner writer added in Tasks 23-25.
import { createClient } from "@supabase/supabase-js";
import { appendAudit } from "./lib/audit.ts";
import { runDriftDetector } from "./agents/drift_detector.ts";
import { runAnomalyDetector } from "./agents/anomaly_detector.ts";
import type { AuditDecision } from "./lib/types.ts";

interface AgentWorkerResult {
  status: "ok" | "error";
  ran_at: string;
  agents_invoked: string[];
  errors: string[];
}

Deno.serve(async (_req: Request): Promise<Response> => {
  const ranAt = new Date().toISOString();
  const result: AgentWorkerResult = {
    status: "ok",
    ran_at: ranAt,
    agents_invoked: [],
    errors: [],
  };

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !supabaseKey) {
    result.status = "error";
    result.errors.push("missing supabase env (URL or SERVICE_ROLE_KEY)");
    return new Response(JSON.stringify(result), { status: 500 });
  }
  const supabase = createClient(supabaseUrl, supabaseKey);

  const allAudits: AuditDecision[] = [];

  // Drift detector (Anthropic-classified schema diffs)
  try {
    const drift = await runDriftDetector(supabase);
    allAudits.push(...drift.audits);
    result.agents_invoked.push(
      `drift_detector: ${drift.audits.length} audits, ${drift.alerts.length} alerts`,
    );
  } catch (e) {
    result.errors.push(`drift_detector: ${String(e)}`);
  }

  // Anomaly detector (per-DOW ±3σ, shadow mode for 14d)
  try {
    const anomaly = await runAnomalyDetector();
    allAudits.push(...anomaly.audits);
    result.agents_invoked.push(
      `anomaly_detector: ${anomaly.audits.length} audits, ${anomaly.alerts.length} alerts (shadowed=${anomaly.shadowed})`,
    );
  } catch (e) {
    result.errors.push(`anomaly_detector: ${String(e)}`);
  }

  // Persist all audits
  await appendAudit(supabase, allAudits);

  return new Response(JSON.stringify(result, null, 2), {
    status: result.errors.length > 0 ? 500 : 200,
    headers: { "content-type": "application/json" },
  });
});
