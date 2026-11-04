// Shared pieces of the data-quality report: types, the check icons, the score ring, status chips and two small charts.
import {
  BarChart3, CalendarX2, CircleDashed, Clock, Columns3, Copy, Link2, PlugZap, RotateCcw, Rows3, Ruler, Scale, Timer, Type, type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Mapping } from "@/components/Databases";

export type CardStatus = "pass" | "fail" | "waiting" | "not_set";
export interface CheckCard {
  key: string; label: string; status: CardStatus; severity: "warning" | "critical" | null;
  what?: string; value?: string; normal?: string; detail?: string; hint?: string | null; at?: string | null;
}
export interface AiTable {
  table: string; verdict: "healthy" | "needs attention" | "broken"; headline: string; explanation: string;
  findings: { check: string; title: string; detail: string }[]; likely_causes: { cause: string; confidence: string }[];
  impact: string; next_steps: string[]; fine: string[];
}
export interface TableSummary {
  table: string; name: string; schema: string; database: string; pipeline: string | null; mapping: Mapping | null;
  connection: { name: string; kind: "sqlserver" | "snowflake" | "sqlite"; status: string } | null;
  status: "ok" | "fail" | "learning"; severity: "warning" | "critical" | null; score: number | null;
  checked_at: string | null; last_load_at: string | null; rows_today: number | null; rows_total: number | null; rows_normal: number | null;
  rows_delta: number | null; freshness_hours: number | null; duplicate_rate: number | null; worst_null: { column: string; rate: number } | null;
  cards: CheckCard[]; failing: number; passing: number; incident: { id: number; title: string; severity: string } | null;
  ai: Partial<AiTable> | null;
}
export interface Column {
  name: string; type: string; null_rate: number | null; empty_rate: number | null; null_normal: number | null; sensitive: boolean;
  required: boolean; key: boolean; invalid_rate: number | null; negative_rate: number | null; out_of_range_rate: number | null;
  stats: { min?: number; max?: number; avg?: number; stddev?: number } | null; mean: number | null; mean_normal: number | null;
  top_share: number | null; top_share_normal: number | null;
}
export interface TableDetail extends Omit<TableSummary, "ai"> {
  ai: AiTable | null; columns: Column[];
  schema_change: { added?: string[]; removed?: string[]; retyped?: { column: string; from: string; to: string }[]; baseline_captured_at?: string } | null;
  profiled_rows: number | null; duplicates: { rows: number | null; groups: number | null; rate: number | null; empty_key_rows: number | null; key: string[] };
  partitions: { recent: number | null; before: number | null };
  rules: { name: string; status: string; severity: string | null; message: string; rate: number | null }[];
  references: { reference: string; status: string; message: string; rate: number | null }[];
  history: { at: string; rows: number | null; total: number | null; fresh: number | null; dup: number | null }[];
  loads: { at: string; rows: number }[];
  pipeline_stats: { runs_7d?: number; failed_7d?: number; retries_7d?: number; last_run_failed?: number | null; last_duration_min?: number | null;
    normal_duration_min?: number | null; hours_since_last_load?: number | null };
  pipeline_runs: { at: string; status: string; rows: number | null; duration_min: number | null; run_id: string | null }[];
  analysis: { summary?: string; model?: string | null; source?: string; note?: string } | null; report_at: string | null;
}
export interface Live { state: "checking" | "analysing" | "idle"; tables?: string[]; reasons?: string[]; at?: string }
export interface Overview { tables: TableSummary[]; live: Live | null; report_at: string | null;
  analysis: { summary?: string; model?: string | null; source?: string; note?: string } | null }

export const CHECK_ICON: Record<string, LucideIcon> = {
  row_count: Rows3, null_spike: CircleDashed, duplicates: Copy, freshness: Clock, schema_drift: Columns3, value_range: Ruler,
  invalid_type: Type, distribution_shift: BarChart3, referential_integrity: Link2, business_rule: Scale, run_too_slow: Timer,
  run_failed: RotateCcw, missing_partitions: CalendarX2, source_available: PlugZap,
};

export const tone = (c: Pick<CheckCard, "status" | "severity">) =>
  c.status === "fail" ? (c.severity === "critical" ? "crit" : "warn") : c.status === "pass" ? "good" : "idle";

export function StatusChip({ c, className }: { c: Pick<CheckCard, "status" | "severity">; className?: string }) {
  const t = tone(c);
  const label = c.status === "pass" ? "Passed" : c.status === "fail" ? (c.severity === "critical" ? "Failing" : "Needs a look") : c.status === "not_set" ? "Not set up" : "Waiting for data";
  return <span className={cn("dq-chip", `dq-${t}`, className)}><i aria-hidden />{label}</span>;
}

export function ScoreRing({ score, size = 64, stroke = 6, label = true }: { score: number | null; size?: number; stroke?: number; label?: boolean }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = score ?? 0;
  const color = score == null ? "var(--ink-3)" : v >= 90 ? "var(--good)" : v >= 70 ? "var(--warning)" : "var(--critical)";
  return (
    <span className="relative inline-grid shrink-0 place-items-center" style={{ width: size, height: size }} aria-label={score == null ? "Not scored yet" : `Quality score ${v} of 100`}>
      <svg width={size} height={size} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={`${(v / 100) * c} ${c}`} className="dq-ring" />
      </svg>
      {label && <span className="dq-num absolute font-bold text-ink" style={{ fontSize: size * 0.3 }}>{score ?? "–"}</span>}
    </span>
  );
}

export const pct = (v: number | null | undefined, d = 1) => (v == null ? "–" : `${(v * 100).toFixed(v > 0 && v < 0.001 ? 3 : d)}%`);
export const n0 = (v: number | null | undefined) => (v == null ? "–" : Math.round(v).toLocaleString());
export const nShort = (v: number | null | undefined) => {
  if (v == null) return "–";
  const a = Math.abs(v);
  return a >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : a >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : a >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : Math.round(v).toLocaleString();
};
export const hours = (h: number | null | undefined) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)} min` : h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} days`);
export const dur = (m: number | null | undefined) => (m == null ? "–" : m < 90 ? `${m.toFixed(m < 10 ? 1 : 0)} min` : `${(m / 60).toFixed(1)} h`);

/** Bars over time with a dashed "normal" line: rows per check, or a pipeline's run durations. */
export function Bars({ values, normal, colors, labels, height = 120, unit = "" }: {
  values: (number | null)[]; normal?: number | null; colors?: string[]; labels?: string[]; height?: number; unit?: string;
}) {
  const max = Math.max(1, normal ?? 0, ...values.map((v) => v ?? 0)) * 1.12;
  const w = 100 / Math.max(values.length, 1);
  return (
    <div className="relative" style={{ height }}>
      <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="absolute inset-0 size-full" aria-hidden>
        {[0.25, 0.5, 0.75].map((g) => <line key={g} x1="0" x2="100" y1={height * g} y2={height * g} stroke="var(--line)" strokeWidth="1" vectorEffect="non-scaling-stroke" />)}
        {values.map((v, i) => {
          const h = v == null ? 0 : Math.max(1.5, (v / max) * height);
          return <rect key={i} x={i * w + w * 0.18} width={w * 0.64} y={height - h} height={h} rx="0.6"
            fill={colors?.[i] ?? (i === values.length - 1 ? "var(--accent)" : "color-mix(in srgb, var(--accent) 45%, var(--surface-3))")} />;
        })}
        {normal != null && <line x1="0" x2="100" y1={height - (normal / max) * height} y2={height - (normal / max) * height}
          stroke="var(--ink-2)" strokeWidth="1.25" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />}
      </svg>
      <div className="absolute inset-0 flex">
        {values.map((v, i) => <span key={i} className="h-full flex-1" title={`${labels?.[i] ?? ""}${labels?.[i] ? ": " : ""}${v == null ? "no value" : `${v.toLocaleString()}${unit}`}`} />)}
      </div>
    </div>
  );
}
