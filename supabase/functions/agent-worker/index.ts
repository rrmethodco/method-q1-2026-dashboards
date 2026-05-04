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
import { runRetryRepair } from "./agents/retry_repair.ts";
import { dispatchAlerts, type AlertEvent } from "./agents/alert_dispatcher.ts";
import { writeBannerStates } from "./agents/banner_writer.ts";
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
  const events: AlertEvent[] = [];

  // 1. Drift detector
  try {
    const drift = await runDriftDetector(supabase);
    allAudits.push(...drift.audits);
    for (const a of drift.alerts) {
      events.push({ kind: "drift_breaking", source: a.source, text: a.reasoning });
    }
    result.agents_invoked.push(`drift_detector: ${drift.audits.length} audits, ${drift.alerts.length} alerts`);
  } catch (e) {
    result.errors.push(`drift_detector: ${String(e)}`);
  }

  // 2. Anomaly detector (shadow until 2026-05-18)
  let anomalyShadowed = true;
  try {
    const anomaly = await runAnomalyDetector();
    anomalyShadowed = anomaly.shadowed;
    allAudits.push(...anomaly.audits);
    if (!anomaly.shadowed) {
      for (const a of anomaly.alerts) {
        events.push({
          kind: "anomaly", source: a.outlet,
          text: `${a.metric} on ${a.date} = ${a.value.toFixed(0)} (z=${a.z_score.toFixed(1)}, expected ${a.expected_mean.toFixed(0)} ±${a.expected_std.toFixed(0)})`,
        });
      }
    }
    result.agents_invoked.push(`anomaly_detector: ${anomaly.audits.length} audits, ${anomaly.alerts.length} alerts (shadowed=${anomaly.shadowed})`);
  } catch (e) {
    result.errors.push(`anomaly_detector: ${String(e)}`);
  }

  // 3. Retry/repair (state persisted to validation/_state/retry_repair.json)
  try {
    const retry = await runRetryRepair(supabase);
    allAudits.push(...retry.audits);
    for (const a of retry.alerts) {
      events.push({ kind: "retry_exhausted", source: a.workflow, text: a.reason });
    }
    result.agents_invoked.push(`retry_repair: ${retry.audits.length} audits, ${retry.alerts.length} alerts`);
  } catch (e) {
    result.errors.push(`retry_repair: ${String(e)}`);
  }

  // 4. Alert dispatcher (dedup state persisted to validation/_state/alert_dispatcher.json)
  if (events.length > 0) {
    try {
      const dispAudits = await dispatchAlerts(supabase, events);
      allAudits.push(...dispAudits);
      result.agents_invoked.push(`alert_dispatcher: ${dispAudits.length} routed`);
    } catch (e) {
      result.errors.push(`alert_dispatcher: ${String(e)}`);
    }
  }

  // 5. Banner state writer
  try {
    const bannerCount = await writeBannerStates(supabase);
    result.agents_invoked.push(`banner_writer: ${bannerCount} outlets`);
  } catch (e) {
    result.errors.push(`banner_writer: ${String(e)}`);
  }

  // Persist all audits
  await appendAudit(supabase, allAudits);

  return new Response(JSON.stringify(result, null, 2), {
    status: result.errors.length > 0 ? 500 : 200,
    headers: { "content-type": "application/json" },
  });
});
