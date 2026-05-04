// Alert dispatcher.
//
// Consumes alert events from drift / anomaly / retry agents, dedups
// (identical event within 60min suppressed), routes to Slack channel
// from SLACK_DASHBOARD_ALERTS_CHANNEL (default C0B1N51L9TN).
import { postAlert } from "../lib/slack.ts";
import type { AuditDecision } from "../lib/types.ts";

export interface AlertEvent {
  kind: "drift_breaking" | "anomaly" | "retry_exhausted";
  source: string;
  text: string;
}

const recentAlerts = new Map<string, number>();
const DEDUP_MS = 60 * 60 * 1000;

export async function dispatchAlerts(events: AlertEvent[]): Promise<AuditDecision[]> {
  const channel = Deno.env.get("SLACK_DASHBOARD_ALERTS_CHANNEL") || "C0B1N51L9TN";
  const ts = new Date().toISOString();
  const audits: AuditDecision[] = [];

  for (const ev of events) {
    const key = `${ev.kind}:${ev.source}:${ev.text.slice(0, 80)}`;
    const last = recentAlerts.get(key);
    if (last && Date.now() - last < DEDUP_MS) {
      audits.push({
        ts, agent: "alert_dispatcher", source: ev.source,
        decision: "deduplicated",
        details: { kind: ev.kind, key },
        action_taken: "suppressed (within 60min dedup window)",
      });
      continue;
    }
    try {
      await postAlert(channel, `[${ev.kind}] ${ev.source}: ${ev.text}`);
      recentAlerts.set(key, Date.now());
      audits.push({
        ts, agent: "alert_dispatcher", source: ev.source,
        decision: "slack_posted",
        details: { kind: ev.kind, channel },
        action_taken: `posted to ${channel}`,
      });
    } catch (e) {
      audits.push({
        ts, agent: "alert_dispatcher", source: ev.source,
        decision: "slack_post_failed",
        details: { error: String(e) },
        action_taken: "alert lost — needs manual triage",
      });
    }
  }
  return audits;
}
