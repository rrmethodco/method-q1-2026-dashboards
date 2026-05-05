// Cross-source reconciler agent.
//
// Catches the class of bug Ross caught manually on 2026-05-05: the
// dashboard's Net Sales KPI was inflated +20-30% over Toast's official
// "Net sales" report because the ETL was sourcing from `check.amount`
// (which empirically includes tips, service charges, etc.) instead of
// computing Net Sales from selection-level data.
//
// Two reconciliation checks per outlet, last 30 days:
//
//   1. INTERNAL: order_details.amount / order_details.net_sales ratio.
//      Expected ratio depends on outlet (driven by tip + service-charge
//      mix), but a healthy LSBR-style outlet sits around 1.20. We alert
//      if the ratio exits a sanity band [0.90, 1.50], which signals
//      either the new net_sales math broke OR check.amount started
//      including something we don't subtract (new Toast SC type, etc.).
//
//   2. EXTERNAL: order_details.net_sales vs sales_summary.net_sales on
//      the same date. These two are computed from different Toast data
//      paths (Orders API selections vs Sales Summary report import).
//      A persistent drift > 5% means one of the sources got stale or
//      misaligned — exactly the failure mode that motivated this agent.
//
// The internal check is dormant until net_sales is populated on enough
// daily rows (post-backfill). The external check only fires for outlets
// that have sales_summary baseline data (currently LSBR; will expand
// once the Sales Summary auto-pull lands).
//
// Audits: one entry per outlet × check pair (whether normal or alerting).
// Alerts: one event per breached threshold, routed via alert_dispatcher
// to the same Slack channel as drift/anomaly.

import type { AuditDecision } from "../lib/types.ts";

const PAGES_BASE = "https://rrmethodco.github.io/method-q1-2026-dashboards/data";
const OUTLETS = ["lsbr", "mulherins", "kampers", "lowland", "vessel",
  "anthology", "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin"];

// Sanity bounds for amount/net_sales ratio. Below 0.90 means net_sales is
// somehow LARGER than amount (impossible if the formulas are right —
// indicates a sign error or schema regression). Above 1.50 means the
// gap between the two has blown out beyond a reasonable tip+SC overlay
// (typical LSBR ratio is 1.20-1.30 historically).
const RATIO_LOWER = 0.90;
const RATIO_UPPER = 1.50;

// Cross-source drift threshold: 5% sustained delta across the lookback
// window between two independently-computed Net Sales values means
// something is misaligned. Tighter than the in-period anomaly threshold
// because we're comparing aggregates over 30 days.
const CROSS_SOURCE_DRIFT = 0.05;

// Minimum coverage before the internal check is meaningful. Pre-backfill
// rows lack net_sales entirely — we wait until at least 7 days of the
// 30-day window have populated values before comparing.
const MIN_NET_SALES_DAYS = 7;

const LOOKBACK_DAYS = 30;

export interface ReconcilerAlert {
  outlet: string;
  kind: "internal_ratio_breach" | "external_source_drift" | "stale_baseline";
  text: string;
}

export interface ReconcilerResult {
  audits: AuditDecision[];
  alerts: ReconcilerAlert[];
}

interface DailyTotals {
  date: string;
  amount: number;
  net_sales: number;
  has_net_sales: boolean;
}

function lastNDays(n: number): string[] {
  const out: string[] = [];
  const today = new Date();
  for (let i = 1; i <= n; i++) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

// Sum daily across all RC keys. Returns one row per date with both the
// legacy `amount` and Toast-aligned `net_sales`.
// deno-lint-ignore no-explicit-any
function aggregateOrderDetailsDaily(payload: any, dateRange: Set<string>): DailyTotals[] {
  const orderDetails = payload?.order_details;
  if (!orderDetails || typeof orderDetails !== "object") return [];
  const byDate: Record<string, DailyTotals> = {};
  for (const rcKey of Object.keys(orderDetails)) {
    const rcDaily = orderDetails[rcKey]?.daily;
    if (!Array.isArray(rcDaily)) continue;
    for (const row of rcDaily) {
      if (!row || typeof row.date !== "string") continue;
      if (!dateRange.has(row.date)) continue;
      const slot = byDate[row.date] ?? {
        date: row.date, amount: 0, net_sales: 0, has_net_sales: false,
      };
      const amt = Number(row.amount);
      const ns = row.net_sales != null ? Number(row.net_sales) : null;
      if (Number.isFinite(amt)) slot.amount += amt;
      if (ns != null && Number.isFinite(ns)) {
        slot.net_sales += ns;
        if (ns !== 0) slot.has_net_sales = true;
      }
      byDate[row.date] = slot;
    }
  }
  return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
}

// Sum sales_summary.daily (Toast-imported Net Sales baseline) across RCs.
// deno-lint-ignore no-explicit-any
function aggregateSalesSummaryDaily(payload: any, dateRange: Set<string>): { date: string; net_sales: number }[] {
  const ss = payload?.sales_summary;
  if (!ss || typeof ss !== "object") return [];
  const byDate: Record<string, { date: string; net_sales: number }> = {};
  for (const rcKey of Object.keys(ss)) {
    const rcDaily = ss[rcKey]?.daily;
    if (!Array.isArray(rcDaily)) continue;
    for (const row of rcDaily) {
      if (!row || typeof row.date !== "string") continue;
      if (!dateRange.has(row.date)) continue;
      const slot = byDate[row.date] ?? { date: row.date, net_sales: 0 };
      const ns = Number(row.net_sales);
      if (Number.isFinite(ns)) slot.net_sales += ns;
      byDate[row.date] = slot;
    }
  }
  return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
}

export async function runCrossSourceReconciler(): Promise<ReconcilerResult> {
  const audits: AuditDecision[] = [];
  const alerts: ReconcilerAlert[] = [];
  const ts = new Date().toISOString();
  const dateRange = new Set(lastNDays(LOOKBACK_DAYS));

  for (const outlet of OUTLETS) {
    // deno-lint-ignore no-explicit-any
    let payload: Record<string, any>;
    try {
      const r = await fetch(`${PAGES_BASE}/${outlet}.json`, { cache: "no-cache" });
      if (!r.ok) {
        audits.push({
          ts, agent: "cross_source_reconciler", source: outlet,
          decision: "fetch_failed",
          details: { http_status: r.status },
          action_taken: "skipped this tick",
        });
        continue;
      }
      payload = await r.json();
    } catch (e) {
      audits.push({
        ts, agent: "cross_source_reconciler", source: outlet,
        decision: "fetch_error",
        details: { error: String(e) },
        action_taken: "skipped this tick",
      });
      continue;
    }

    const odDaily = aggregateOrderDetailsDaily(payload, dateRange);
    const ssDaily = aggregateSalesSummaryDaily(payload, dateRange);

    // ====================================================================
    // CHECK 1: internal ratio (amount / net_sales)
    // ====================================================================
    const odWithNS = odDaily.filter((r) => r.has_net_sales);
    if (odWithNS.length < MIN_NET_SALES_DAYS) {
      // Not enough post-fix coverage yet. Note in audit but don't alert.
      audits.push({
        ts, agent: "cross_source_reconciler", source: outlet,
        decision: "internal_check_skipped_insufficient_coverage",
        details: {
          days_with_net_sales: odWithNS.length,
          min_required: MIN_NET_SALES_DAYS,
          lookback_days: LOOKBACK_DAYS,
        },
        action_taken: "deferred until backfill populates net_sales on more rows",
      });
    } else {
      const totalAmt = odWithNS.reduce((s, r) => s + r.amount, 0);
      const totalNS = odWithNS.reduce((s, r) => s + r.net_sales, 0);
      const ratio = totalNS > 0 ? totalAmt / totalNS : null;
      if (ratio == null || !Number.isFinite(ratio)) {
        audits.push({
          ts, agent: "cross_source_reconciler", source: outlet,
          decision: "internal_check_zero_net_sales",
          details: { totalAmt, totalNS, days: odWithNS.length },
          action_taken: "zero or non-finite Net Sales — manual check warranted",
        });
      } else if (ratio < RATIO_LOWER || ratio > RATIO_UPPER) {
        const text = `amount/net_sales ratio = ${ratio.toFixed(2)} ` +
          `(amount $${totalAmt.toFixed(0)} / net_sales $${totalNS.toFixed(0)} ` +
          `over ${odWithNS.length} days). Expected band [${RATIO_LOWER}, ${RATIO_UPPER}]. ` +
          `Likely ETL regression — net_sales formula or check.amount semantics changed.`;
        alerts.push({ outlet, kind: "internal_ratio_breach", text });
        audits.push({
          ts, agent: "cross_source_reconciler", source: outlet,
          decision: "internal_ratio_breach",
          details: { ratio, totalAmt, totalNS, days: odWithNS.length,
                     band: [RATIO_LOWER, RATIO_UPPER] },
          action_taken: "queued for Slack alert",
        });
      } else {
        audits.push({
          ts, agent: "cross_source_reconciler", source: outlet,
          decision: "internal_ratio_ok",
          details: { ratio, totalAmt, totalNS, days: odWithNS.length },
          action_taken: "no alert",
        });
      }
    }

    // ====================================================================
    // CHECK 2: external — order_details.net_sales vs sales_summary.net_sales
    // ====================================================================
    if (ssDaily.length === 0) {
      // No sales_summary baseline. Skip silently — once the auto-pull
      // lands this'll start running.
    } else {
      // Find dates where BOTH sources have data and order_details has net_sales populated.
      const odMap: Record<string, DailyTotals> = {};
      for (const r of odDaily) odMap[r.date] = r;
      const overlap: { date: string; od: number; ss: number }[] = [];
      for (const ss of ssDaily) {
        const od = odMap[ss.date];
        if (!od || !od.has_net_sales) continue;
        overlap.push({ date: ss.date, od: od.net_sales, ss: ss.net_sales });
      }

      if (overlap.length < MIN_NET_SALES_DAYS) {
        audits.push({
          ts, agent: "cross_source_reconciler", source: outlet,
          decision: "external_check_skipped_insufficient_overlap",
          details: { overlap_days: overlap.length, min_required: MIN_NET_SALES_DAYS },
          action_taken: "deferred until both sources cover >=7 of last 30 days",
        });

        // Stale-baseline check: if sales_summary's most recent date is
        // > 7 days behind today, flag it. Once the auto-pull is live this
        // shouldn't happen — alerts here motivate fixing the upstream.
        const ssLatest = ssDaily[ssDaily.length - 1]?.date;
        if (ssLatest) {
          const ageDays = Math.floor(
            (Date.now() - new Date(ssLatest + "T12:00:00Z").getTime()) / 86400000,
          );
          if (ageDays > 7) {
            const text = `sales_summary baseline is ${ageDays} days stale ` +
              `(latest ${ssLatest}). Refresh the Toast Sales Summary CSV import ` +
              `or accelerate the auto-pull rollout.`;
            alerts.push({ outlet, kind: "stale_baseline", text });
            audits.push({
              ts, agent: "cross_source_reconciler", source: outlet,
              decision: "stale_baseline",
              details: { ss_latest: ssLatest, age_days: ageDays },
              action_taken: "queued for Slack alert",
            });
          }
        }
      } else {
        const totalOd = overlap.reduce((s, r) => s + r.od, 0);
        const totalSs = overlap.reduce((s, r) => s + r.ss, 0);
        if (totalSs <= 0) {
          audits.push({
            ts, agent: "cross_source_reconciler", source: outlet,
            decision: "external_check_zero_baseline",
            details: { totalOd, totalSs, days: overlap.length },
            action_taken: "sales_summary contributed zero — likely import regression",
          });
        } else {
          const drift = (totalOd - totalSs) / totalSs;
          if (Math.abs(drift) > CROSS_SOURCE_DRIFT) {
            const text = `cross-source drift = ${(drift * 100).toFixed(1)}% ` +
              `(order_details $${totalOd.toFixed(0)} vs sales_summary $${totalSs.toFixed(0)} ` +
              `over ${overlap.length} overlapping days). Threshold ±${(CROSS_SOURCE_DRIFT * 100).toFixed(0)}%. ` +
              `One of the two Net Sales feeds is stale or misaligned.`;
            alerts.push({ outlet, kind: "external_source_drift", text });
            audits.push({
              ts, agent: "cross_source_reconciler", source: outlet,
              decision: "external_source_drift",
              details: { drift, totalOd, totalSs, days: overlap.length,
                         threshold: CROSS_SOURCE_DRIFT },
              action_taken: "queued for Slack alert",
            });
          } else {
            audits.push({
              ts, agent: "cross_source_reconciler", source: outlet,
              decision: "external_drift_ok",
              details: { drift, totalOd, totalSs, days: overlap.length },
              action_taken: "no alert",
            });
          }
        }
      }
    }
  }

  return { audits, alerts };
}
