export type Severity = "critical" | "warning" | "info";
export type Status = Severity | "ok" | "unknown";

export interface ProjectCard {
  key: string;
  display_name: string;
  description: string;
  role: "viewer" | "owner";
  status: Status;
  open_incidents: number;
  /** not yet acknowledged: what the badges count */
  needs_attention?: number;
  acknowledged?: number;
  attention_severity?: Severity | null;
  resolved_today?: number;
  health: number | null;
  last_cycle_at: string | null;
  kind?: "scrape_runs" | "feeds" | "adf" | null;
  service?: "Data Factory" | "Synapse" | "Logic Apps" | null;
}

/** What the bot found in Azure with this person's own sign-in. */
export interface Discovery {
  status: "running" | "done" | "error";
  message: string;
  at: string;
  found?: number;
  projects?: string[];
  created?: string[];
  counts?: Record<string, number>;
}

export interface Me {
  user: { email: string; name: string; admin: boolean };
  projects: ProjectCard[];
  discovery?: Discovery | null;
  prefs?: { dock?: Record<string, string[]>; nav_layout?: "dock" | "side" | null; tour_done_at?: string | null; tour_version?: number | null; style?: "modern" | "classic" | null };
}

export interface Health {
  status: string;
  version: string;
  auth_mode: "dev" | "entra";
  bot: { last_cycle_at: string | null; minutes_since_last_cycle: number | null; stale: boolean };
}

export interface RunRow {
  /** this run's time against its pipeline's normal (ADF / Synapse / Logic Apps) */
  vs_normal?: import("@/components/RuntimeVsNormal").VsNormal | null;
  id: number;
  trigger_id: string;
  site: string;
  trigger_status: string;
  run_id: string | null;
  pipeline: string | null;
  run_status: string | null;
  requested_at: string;
  started_at: string | null;
  ended_at: string | null;
  duration_s: number | null;
  rows_loaded: number | null;
  error: string | null;
}

export interface IncidentBrief {
  id: number;
  title: string;
  severity: Severity;
  status: "open" | "acknowledged" | "resolved";
  scope: string;
  scope_label: string;
  checks: string[];
  likely_cause: string;
  summary: string;
  analysis_source: "rules" | "ai";
  confidence: string;
  opened_at: string;
  updated_at: string;
  last_seen_at?: string | null;
  acknowledged_at?: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  resolved_by?: string | null;
  resolution?: string | null;
  comments?: number;
  last_comment?: IncidentComment | null;
}

export interface IncidentComment { id: number; at: string; by: string; text: string }

export interface Notifier {
  channel: string;
  mode: "live" | "dry_run" | "off";
  teams_channel: string;
  flow_url_ref: string | null;
  email_owners_on: string[];
}

export interface SiteCard {
  site: string;
  status: Status;
  failing: { check: string; severity: Severity; message: string }[];
  last_run: RunRow;
  last_rows: number | null;
  expected: { low: number; high: number; median: number; basis: string } | null;
  sparkline: number[];
  incident_id: number | null;
}

export interface Overview {
  project: { key: string; display_name: string; description: string; role: string };
  status: Status;
  health: number | null;
  kpis: {
    runs_today: number;
    failed_today: number;
    rows_today: number;
    open_incidents: number;
    critical_open: number;
    checks: number;
    checks_failing: number;
  };
  bot: { last_cycle_at: string | null; last_cycle_status: string | null; last_error: string | null; schedule: string };
  sites: SiteCard[];
  tables: { output: string; status: string; message: string }[];
  active_incidents: IncidentBrief[];
  recent_events: { at: string; kind: string; message: string; incident_id: number; title: string; severity: Severity }[];
  activity: { date: string; runs: number; failed: number; rows: number }[];
  notifier: Notifier;
  ai: { enabled: boolean; provider: string | null; model: string | null; framework: string };
}

export interface FailingCheck {
  check: string;
  severity: Severity;
  message: string;
  actual: number | null;
  expected_low: number | null;
  expected_high: number | null;
  at: string;
  details: Record<string, any>;
}

export interface AlertRow {
  id: number;
  event: string;
  status: "pending" | "sent" | "dry_run" | "failed" | "muted";
  channel: string;
  attempts: number;
  last_error: string | null;
  created_at: string;
  sent_at: string | null;
  incident_id: number | null;
  title: string;
  severity: Severity;
  payload: Record<string, any>;
}

export interface IncidentDetail extends Omit<IncidentBrief, "comments"> {
  kind?: "scrape_runs" | "feeds" | "adf";
  impact: string;
  suggested_action: string;
  evidence: { label: string; detail: string; tool: string; data?: Record<string, unknown> }[];
  site: string | null;
  run: RunRow | null;
  failing_checks: FailingCheck[];
  events: { at: string; kind: string; message: string; data: Record<string, any> }[];
  tool_calls: { at: string; agent: string; tool: string; args: any; result: any; duration_ms: number; error: string | null }[];
  notifications: AlertRow[];
  comments: IncidentComment[];
  charts: { metric: string; field: string; label: string; output?: string }[];
  remediation?: import("@/components/Remediation").RemediationPlan | null;
}

export interface SeriesPoint {
  t: string;
  value: number;
  run: string;
  weekday: string;
  low: number | null;
  high: number | null;
  median: number | null;
}

export interface TableView {
  id: string;
  table: string;
  key: string[];
  group_by: string;
  loaded_at: string;
  required_fields: string[];
  schema: {
    accepted: { name: string; type: string }[] | null;
    accepted_at: string | null;
    live: { name: string; type: string }[];
    live_error: string | null;
    diff: { added: string[]; removed: string[]; retyped: { column: string; from: string; to: string }[] };
    drifted: boolean;
  };
  sites: { site: string; rows: number | null; null_rates: Record<string, number>; duplicate_rate: number | null; at: string | null }[];
}

export interface MuteRow {
  id: number;
  scope: string;
  reason: string;
  until: string;
  created_by: string;
  created_at: string;
  active: boolean;
}
