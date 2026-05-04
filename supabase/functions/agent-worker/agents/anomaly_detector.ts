// Anomaly detector agent.
//
// For each (outlet × metric × DOW) tuple, computes rolling 8-week mean+std
// and flags if today's value lies beyond ±3σ. Phase A.1: SHADOW MODE for
// the first 14 days (logs to audit only; no Slack push). The alert_dispatcher
// honors the shadow flag.
//
// Reads outlet daily history from data/<outlet>.json via GH Pages public URL
// (the dashboard already publishes the same JSON the agent needs).

import type { AuditDecision } from "../lib/types.ts";

const PAGES_BASE = "https://rrmethodco.github.io/method-q1-2026-dashboards/data";
const OUTLETS = ["lsbr", "mulherins", "kampers", "lowland", "vessel",
  "anthology", "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin"];
const METRICS = ["amount", "guests"] as const;
// 14-day shadow window: until 2026-05-18, anomalies only log to audit;
// no Slack push. Tune via env if needed.
const SHADOW_UNTIL_ISO = Deno.env.get("ANOMALY_SHADOW_UNTIL") ?? "2026-05-18T00:00:00Z";

interface AnomalyAlert {
  outlet: string;
  metric: string;
  date: string;
  value: number;
  expected_mean: number;
  expected_std: number;
  z_score: number;
}

export interface AnomalyResult {
  audits: AuditDecision[];
  alerts: AnomalyAlert[];
  shadowed: boolean;
}

export async function runAnomalyDetector(): Promise<AnomalyResult> {
  const audits: AuditDecision[] = [];
  const alerts: AnomalyAlert[] = [];
  const ts = new Date().toISOString();
  const shadowed = ts < SHADOW_UNTIL_ISO;

  for (const outlet of OUTLETS) {
    // deno-lint-ignore no-explicit-any
    let payload: Record<string, any>;
    try {
      const r = await fetch(`${PAGES_BASE}/${outlet}.json`, { cache: "no-cache" });
      if (!r.ok) continue;
      payload = await r.json();
    } catch {
      continue;
    }

    // deno-lint-ignore no-explicit-any
    const od = (payload as any).order_details?.main?.daily;
    if (!Array.isArray(od) || od.length < 60) continue; // need 8w of data

    for (const metric of METRICS) {
      const byDow: Record<number, number[]> = { 0: [], 1: [], 2: [], 3: [], 4: [], 5: [], 6: [] };
      for (const row of od) {
        const d = new Date(row.date + "T12:00:00Z");
        const dow = d.getUTCDay();
        const v = Number(row[metric]);
        if (Number.isFinite(v)) byDow[dow].push(v);
      }

      // Yesterday's value (if present) — that's the new observation
      const yesterday = od[od.length - 1];
      const yDate = new Date(yesterday.date + "T12:00:00Z");
      const yDow = yDate.getUTCDay();
      const yVal = Number(yesterday[metric]);
      if (!Number.isFinite(yVal)) continue;

      const history = byDow[yDow].slice(-9, -1); // 8 prior same-DOW occurrences
      if (history.length < 4) continue;
      const mean = history.reduce((s, v) => s + v, 0) / history.length;
      const variance = history.reduce((s, v) => s + (v - mean) ** 2, 0) / history.length;
      const std = Math.sqrt(variance);
      if (std === 0) continue;
      const z = (yVal - mean) / std;
      if (Math.abs(z) > 3) {
        alerts.push({
          outlet, metric, date: yesterday.date, value: yVal,
          expected_mean: mean, expected_std: std, z_score: z,
        });
        audits.push({
          ts, agent: "anomaly_detector", source: outlet,
          decision: shadowed ? "anomaly_shadow_logged" : "anomaly_alerted",
          details: { metric, value: yVal, mean, std, z, shadowed },
          action_taken: shadowed
            ? `logged only (shadow mode until ${SHADOW_UNTIL_ISO})`
            : "queued for Slack alert",
        });
      }
    }
  }

  return { audits, alerts, shadowed };
}
