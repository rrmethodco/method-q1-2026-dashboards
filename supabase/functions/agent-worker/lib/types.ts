// Shared types for the agent worker.

export interface ValidationSummary {
  source: string;
  schema_version: string;
  ran_at: string;
  rows_in: number;
  rows_valid: number;
  rows_invalid: number;
  rows_warned: number;
  outlets_touched: string[];
  errors_sample: ValidationError[];
  warnings_sample: ValidationWarning[];
}

export interface ValidationError {
  row_offset: number;
  code: string;
  message: string;
  row_keys: string[];
}

export interface ValidationWarning {
  row_offset: number;
  rules: string[];
  row_keys: string[];
}

export interface BannerState {
  outlet: string;
  worst_class: "ok" | "warn" | "err";
  message: string;
  updated_at: string;
}

export type AuditAgent =
  | "drift_detector"
  | "anomaly_detector"
  | "retry_repair"
  | "alert_dispatcher"
  | "banner_writer"
  | "cross_source_reconciler";

export interface AuditDecision {
  ts: string;
  agent: AuditAgent;
  source: string;
  decision: string;
  details: Record<string, unknown>;
  action_taken: string;
}
