// Banner state writer.
//
// Each invocation, summarize the current state of validation summaries
// per outlet and write a JSON file the dashboard can fetch.
import { SupabaseClient } from "@supabase/supabase-js";
import type { BannerState, ValidationSummary } from "../lib/types.ts";

const SOURCES = ["toast_order", "toast_time_entry", "resy_survey",
  "marginedge_invoice", "tripleseat_event", "helixo2_forecast", "sage_budget"];
const OUTLETS = ["lsbr", "mulherins", "kampers", "lowland", "vessel",
  "anthology", "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin"];

export async function writeBannerStates(supabase: SupabaseClient): Promise<number> {
  const ts = new Date().toISOString();
  // Pre-fetch latest summary per source
  const latestPerSource = new Map<string, ValidationSummary>();
  for (const s of SOURCES) {
    const { data: files } = await supabase.storage.from("validation").list(s, {
      limit: 1,
      sortBy: { column: "name", order: "desc" },
    });
    if (!files || files.length === 0) continue;
    const { data: blob } = await supabase.storage
      .from("validation").download(`${s}/${files[0].name}`);
    if (!blob) continue;
    latestPerSource.set(s, JSON.parse(await blob.text()));
  }

  let written = 0;
  for (const outlet of OUTLETS) {
    let worst: BannerState["worst_class"] = "ok";
    const issues: string[] = [];
    for (const [src, summary] of latestPerSource) {
      if (!summary.outlets_touched.includes(outlet)) continue;
      const ageHrs = (Date.now() - new Date(summary.ran_at).getTime()) / 3_600_000;
      if (summary.rows_invalid > 0) {
        worst = "err";
        issues.push(`${src}: ${summary.rows_invalid} invalid rows`);
      } else if (ageHrs > 26 && worst !== "err") {
        worst = "warn";
        issues.push(`${src}: ${ageHrs.toFixed(1)}h stale`);
      } else if (summary.rows_warned > 0 && worst === "ok") {
        worst = "warn";
      }
    }
    const banner: BannerState = {
      outlet,
      worst_class: worst,
      message: issues.join("; ") || "All sources current.",
      updated_at: ts,
    };
    await supabase.storage.from("banner").upload(
      `${outlet}.json`,
      JSON.stringify(banner, null, 2),
      { contentType: "application/json", upsert: true },
    );
    written++;
  }
  return written;
}
