// Persistent state for cross-invocation memory.
//
// Edge Functions are stateless — module-level Maps reset on every cold
// start. For state that MUST persist across invocations (Slack dedup
// timestamps, retry budget counters), read from / write back to a JSON
// blob in the `validation` bucket under a `_state/` prefix.
//
// Reads + writes happen at agent boundaries (one read at start, one
// write at end). The bucket is private (service-role only).

import { SupabaseClient } from "@supabase/supabase-js";

const BUCKET = "validation";
const PREFIX = "_state";

export async function readState<T>(
  supabase: SupabaseClient,
  agent: string,
  defaultValue: T,
): Promise<T> {
  const path = `${PREFIX}/${agent}.json`;
  const { data, error } = await supabase.storage.from(BUCKET).download(path);
  if (error || !data) return defaultValue;
  try {
    return JSON.parse(await data.text()) as T;
  } catch {
    return defaultValue;
  }
}

export async function writeState<T>(
  supabase: SupabaseClient,
  agent: string,
  state: T,
): Promise<void> {
  const path = `${PREFIX}/${agent}.json`;
  const { error } = await supabase.storage.from(BUCKET).upload(
    path,
    JSON.stringify(state, null, 2),
    { contentType: "application/json", upsert: true },
  );
  if (error) {
    console.error(`state write failed for ${agent}:`, error.message);
  }
}

// Helper: prune entries older than `maxAgeMs` from a `Record<string,number>`
// where values are timestamps (milliseconds since epoch). Used by both
// alert dispatcher (dedup) and retry/repair (retry budget) to keep the
// state file small.
export function pruneStale(
  m: Record<string, number>,
  maxAgeMs: number,
): Record<string, number> {
  const cutoff = Date.now() - maxAgeMs;
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(m)) {
    if (v >= cutoff) out[k] = v;
  }
  return out;
}
