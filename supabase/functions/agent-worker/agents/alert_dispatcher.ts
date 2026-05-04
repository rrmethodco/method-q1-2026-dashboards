// Alert dispatcher.
//
// Consumes alert events from drift / anomaly / retry agents, dedups
// (identical event within 60min suppressed), routes to Slack channel
// from SLACK_DASHBOARD_ALERTS_CHANNEL (default C0B1N51L9TN).
//
// Dedup state is persisted to Supabase Storage (`validation/_state/
// alert_dispatcher.json`). Pre-fix the dedup map was in-memory and
// reset on every Edge Function cold start, which made every
// persistent drift alert fire to Slack on every 5-min tick instead
// of once per 60min window (integration review 2026-05-04).
import { SupabaseClient } from "@supabase/supabase-js";
import { postAlert } from "../lib/slack.ts";
import { readState, writeState, pruneStale } from "../lib/state.ts";
import type { AuditDecision } from "../lib/types.ts";

export interface AlertEvent {
  kind: "drift_breaking" | "anomaly" | "retry_exhausted";
  source: string;
  text: string;
}

interface DispatcherState {
  recent_alerts: Record<string, number>; // dedup-key → last-posted-ms
}

const DEDUP_MS = 60 * 60 * 1000;
const AGENT = "alert_dispatcher";

export async function dispatchAlerts(
  supabase: SupabaseClient,
  events: AlertEvent[],
): Promise<AuditDecision[]> {
  const channel = Deno.env.get("SLACK_DASHBOARD_ALERTS_CHANNEL") || "C0B1N51L9TN";
  const ts = new Date().toISOString();
  const audits: AuditDecision[] = [];

  // Read persistent dedup state. Prune entries older than DEDUP_MS so
  // the file stays small (typically <2KB).
  const state = await readState<DispatcherState>(supabase, AGENT, {
    recent_alerts: {},
  });
  const recent = pruneStale(state.recent_alerts, DEDUP_MS);

  for (const ev of events) {
    const key = `${ev.kind}:${ev.source}:${ev.text.slice(0, 80)}`;
    const last = recent[key];
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
      recent[key] = Date.now();
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

  // Persist updated dedup state for the next cron tick.
  await writeState(supabase, AGENT, { recent_alerts: recent });

  return audits;
}
