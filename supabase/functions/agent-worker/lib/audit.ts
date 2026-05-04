// Append-only audit log writer.
//
// Stored as audit/agent_decisions.jsonl in the audit bucket. Each
// agent invocation appends one JSONL line per decision.
//
// Concurrency: Edge Functions are single-instance per invocation, but
// pg_cron may overlap. We append by READ → APPEND → WRITE which has
// a small race window; acceptable for Phase A.1 (audit loss is low
// impact). If overlap becomes an issue, switch to a Postgres table.

import { SupabaseClient } from "@supabase/supabase-js";
import type { AuditDecision } from "./types.ts";

const AUDIT_PATH = "agent_decisions.jsonl";

export async function appendAudit(
  supabase: SupabaseClient,
  decisions: AuditDecision[],
): Promise<void> {
  if (decisions.length === 0) return;
  const newLines = decisions.map((d) => JSON.stringify(d)).join("\n") + "\n";

  const { data: existing, error: readErr } = await supabase.storage
    .from("audit").download(AUDIT_PATH);
  let combined = newLines;
  if (!readErr && existing) {
    const prior = await existing.text();
    combined = prior + newLines;
  }

  const { error: writeErr } = await supabase.storage
    .from("audit").upload(AUDIT_PATH, combined, {
      contentType: "application/x-ndjson",
      upsert: true,
    });
  if (writeErr) {
    console.error("audit write failed:", writeErr.message);
  }
}
