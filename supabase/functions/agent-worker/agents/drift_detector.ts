// Schema-drift detector agent.
//
// For each source:
//   1. Read latest validation summary from validation bucket
//   2. Read stored schema from validation/_schemas/<source>.json (or seed if absent)
//   3. Compare sample rows against stored schema
//   4. If diff: ask Claude to classify (stable / additive_non_breaking / breaking)
//   5. additive_non_breaking → auto-update stored schema, log audit
//      breaking → leave stored schema untouched, return alert event for
//                 the alert_dispatcher to surface
//      stable → no-op

import { SupabaseClient } from "@supabase/supabase-js";
import { classifyDrift } from "../lib/anthropic.ts";
import type { AuditDecision, ValidationSummary } from "../lib/types.ts";

const SOURCES = ["toast_order", "toast_time_entry", "resy_survey",
  "marginedge_invoice", "tripleseat_event", "helixo2_forecast", "sage_budget"];

export interface DriftResult {
  audits: AuditDecision[];
  alerts: { source: string; classification: string; reasoning: string }[];
}

export async function runDriftDetector(
  supabase: SupabaseClient,
): Promise<DriftResult> {
  const audits: AuditDecision[] = [];
  const alerts: DriftResult["alerts"] = [];
  const ts = new Date().toISOString();

  for (const source of SOURCES) {
    // Get latest validation summary file for this source
    const { data: files } = await supabase.storage
      .from("validation").list(source, {
        limit: 100,
        sortBy: { column: "name", order: "desc" },
      });
    if (!files || files.length === 0) continue;
    const latestFile = files[0].name;

    const { data: summaryBlob } = await supabase.storage
      .from("validation").download(`${source}/${latestFile}`);
    if (!summaryBlob) continue;
    const summary: ValidationSummary = JSON.parse(await summaryBlob.text());

    // Sample row keys from warnings + errors (these are the actual rows
    // that came through, just with warning flags or errors).
    const sampleKeys = new Set<string>();
    for (const w of summary.warnings_sample) for (const k of w.row_keys) sampleKeys.add(k);
    for (const e of summary.errors_sample)   for (const k of e.row_keys) sampleKeys.add(k);

    // Read stored schema
    const schemaPath = `_schemas/${source}.json`;
    const { data: schemaBlob, error: schemaErr } = await supabase.storage
      .from("validation").download(schemaPath);
    let storedKeys: string[];
    if (schemaErr || !schemaBlob) {
      // Seed from current sample
      storedKeys = [...sampleKeys].sort();
      await supabase.storage.from("validation").upload(
        schemaPath,
        JSON.stringify({ keys: storedKeys, seeded_at: ts }, null, 2),
        { contentType: "application/json", upsert: true },
      );
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "seeded_initial_schema",
        details: { keys: storedKeys },
        action_taken: `stored new _schemas/${source}.json`,
      });
      continue;
    }
    const stored = JSON.parse(await schemaBlob.text());
    storedKeys = stored.keys || [];

    // Quick diff
    const observed = [...sampleKeys].sort();
    const added = observed.filter((k) => !storedKeys.includes(k));
    const removed = storedKeys.filter((k) => !observed.includes(k));
    if (added.length === 0 && removed.length === 0 && summary.rows_warned === 0) {
      continue; // stable
    }

    // LLM classification
    const samples = [...summary.warnings_sample.slice(0, 3),
                     ...summary.errors_sample.slice(0, 3)]
      .map((s) => Object.fromEntries(s.row_keys.map((k) => [k, "..."])));
    let cls;
    try {
      cls = await classifyDrift({
        source, storedSchemaKeys: storedKeys, observedRowSamples: samples,
      });
    } catch (e) {
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "classifier_error",
        details: { error: String(e) },
        action_taken: "skipped this cycle; will retry next run",
      });
      continue;
    }

    if (cls.classification === "additive_non_breaking") {
      const newKeys = [...new Set([...storedKeys, ...added])].sort();
      await supabase.storage.from("validation").upload(
        schemaPath,
        JSON.stringify({ keys: newKeys, updated_at: ts, prior: stored }, null, 2),
        { contentType: "application/json", upsert: true },
      );
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "additive_non_breaking_auto_applied",
        details: { added, reasoning: cls.reasoning },
        action_taken: `stored schema bumped to include: ${added.join(", ")}`,
      });
    } else if (cls.classification === "breaking") {
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "breaking_drift_detected",
        details: {
          added, removed,
          changed_types: cls.changed_types,
          reasoning: cls.reasoning,
        },
        action_taken: "alert dispatched; stored schema NOT updated",
      });
      alerts.push({
        source, classification: "breaking", reasoning: cls.reasoning,
      });
    }
  }

  return { audits, alerts };
}
