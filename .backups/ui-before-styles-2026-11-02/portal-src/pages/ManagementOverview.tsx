// Management overview: one page that answers "is our data arriving, how reliable are the pipelines, what's broken and
// since when, which tables are behind, and is it getting better or worse?" Printable for a weekly report.
import {
  AlertTriangle, ArrowDownRight, ArrowUpRight, CheckCircle2, Clock, Database, FileText, Gauge, ListChecks, OctagonAlert, Printer, ShieldAlert,
  ShieldCheck, Siren, Timer, Workflow, XCircle,
} from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ChartCard, Notice } from "@/components/Cards";
import { SeverityPill, StatusChip } from "@/components/Status";
import { EnvBadge } from "@/components/TableLoads";
import { Button } from "@/components/ui/button";
import { Segmented, Skeleton } from "@/components/ui/primitives";
import { ago, dateTime, duration, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface Day { date: string; succeeded: number; failed: number; rows: number }
interface Mgmt {
  project: { key: string; name: string; kind: string; description: string };
  period_days: number; generated_at: string;
  status: { level: "ok" | "warning" | "critical"; headline: string; reasons: string[] };
  kpis: {
    success_rate: number | null; success_rate_prev: number | null; runs: number; failed: number; runs_prev: number; failed_prev: number;
    tables: number; tables_on_time: number; tables_behind: number; rows_7d: number; rows_prev_7d: number; rows_scope: string;
    open_critical: number; open_warning: number; being_handled?: number; incidents_opened: number; incidents_opened_prev: number;
    mttr_hours: number | null; fixed: number; last_full_success_at: string | null;
  };
  daily: Day[];
  failure_reasons: { reason: string; count: number; last: string; steps: string[]; pipelines: string[] }[];
  pipelines: { name: string; runs: number; failed: number; success_rate: number | null; last_run_at: string | null; last_status: string | null;
    failing_streak: number; last_success_at: string | null; median_duration_s: number | null; trigger: string | null;
    state?: string; folder?: string | null; steps?: number | null; type?: string;
    triggers?: { name: string; kind: string; started: boolean; every_hours: number | null }[] }[];
  tables: { name: string; table: string; env: string | null; pipeline: string; state: "ok" | "unusual" | "late" | "issue";
    last_at: string; age_hours: number; rows: number | null; normal: number | null; incident: { id: number; title: string } | null;
    quality?: { status: "ok" | "fail" | "learning"; score: number | null; failing: string[] } | null }[];
  health?: { score: number | null; parts: { pipelines: number | null; delivery: number | null; quality: number | null }; weights: Record<string, number> };
  quality?: { enabled: boolean; status: { unavailable?: boolean; message?: string; last_success_at?: string } | null;
    summary: { tables: number; failing: number; checks: number; checks_failing: number; score: number | null } } | null;
  incidents: { id: number; title: string; severity: string; status: string; opened_at: string; age_hours: number; likely_cause: string; acknowledged_by: string | null }[];
}

function useBox<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(240, e!.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function ticks(max: number, n = 4) {
  if (max <= 0) return [0, 1];
  const raw = max / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw)!;
  return Array.from({ length: Math.ceil(max / step) + 1 }, (_, i) => i * step);
}
const short = (v: number) => (v >= 1e6 ? `${+(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${+(v / 1e3).toFixed(1)}k` : `${v}`);
const dayLabel = (d: string) => new Date(`${d}T12:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });

// ── the one number: data health score (0-100), with what it's made of ──
function HealthGauge({ health }: { health: NonNullable<Mgmt["health"]> }) {
  const v = health.score;
  const r = 34, c = 2 * Math.PI * r;
  const color = v == null ? "var(--ink-3)" : v >= 90 ? "var(--good)" : v >= 70 ? "var(--warning)" : "var(--critical)";
  const parts = [["Pipelines", health.parts.pipelines], ["Delivery", health.parts.delivery], ["Data quality", health.parts.quality]] as const;
  return (
    <div className="flex items-center gap-4 rounded-2xl bg-surface/70 p-3 pr-4 shadow-[inset_0_0_0_1px_var(--line)] light:bg-white/80">
      <svg width="84" height="84" viewBox="0 0 84 84" role="img" aria-label={`Data health score ${v ?? "not available"} out of 100`}>
        <circle cx="42" cy="42" r={r} fill="none" strokeWidth="7" style={{ stroke: "var(--surface-3)" }} />
        {v != null && <circle cx="42" cy="42" r={r} fill="none" strokeWidth="7" strokeLinecap="round" strokeDasharray={`${(v / 100) * c} ${c}`}
          transform="rotate(-90 42 42)" style={{ stroke: color }} />}
        <text x="42" y="49" textAnchor="middle" className="fill-[var(--ink)] text-[20px] font-bold">{v ?? "–"}</text>
      </svg>
      <div>
        <p className="text-xs font-semibold text-ink">Data health score</p>
        <ul className="mt-1 space-y-0.5 text-[0.71875rem] text-ink-2">
          {parts.map(([label, p]) => <li key={label}>{label} <b className="num text-ink">{p == null ? "–" : Math.round(p)}</b></li>)}
        </ul>
      </div>
    </div>
  );
}

// ── KPI tile ──
function Kpi({ label, value, sub, delta, icon: Icon, tone }: {
  label: string; value: ReactNode; sub?: ReactNode; icon: typeof Gauge; tone?: "good" | "warning" | "critical";
  delta?: { text: string; good: boolean } | null;
}) {
  return (
    <div className="rounded-2xl bg-surface p-4 shadow-[inset_0_0_0_1px_var(--line)] light:bg-white">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium text-ink-3">{label}</p>
        <Icon className={cn("size-4", tone === "critical" ? "text-[var(--critical)]" : tone === "warning" ? "text-[var(--warning)]" : tone === "good" ? "text-[var(--good)]" : "text-ink-3")} aria-hidden />
      </div>
      <p className="display mt-1.5 text-[1.75rem] font-bold leading-none tracking-[-0.02em] text-ink">{value}</p>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-ink-3">
        {delta && (
          <span className={cn("inline-flex items-center font-semibold", delta.good ? "text-[var(--good-ink,var(--good))]" : "text-[var(--critical-ink)]")}>
            {delta.good ? <ArrowUpRight className="size-3.5" /> : <ArrowDownRight className="size-3.5" />}{delta.text}
          </span>
        )}
        {sub}
      </div>
    </div>
  );
}

// ── runs per day: succeeded (blue) + failed (red, hatched), stacked; hover a day for its numbers ──
function RunsChart({ days }: { days: Day[] }) {
  const [ref, w] = useBox<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const H = 190, pad = { l: 32, r: 8, t: 8, b: 22 };
  const max = Math.max(1, ...days.map((d) => d.succeeded + d.failed));
  const ys = ticks(max);
  const top = ys[ys.length - 1]!;
  const iw = w - pad.l - pad.r, ih = H - pad.t - pad.b;
  const slot = iw / days.length, bw = Math.max(3, Math.min(22, slot - 4));
  const y = (v: number) => pad.t + ih - (v / top) * ih;
  const every = Math.ceil(days.length / 8);
  const h = hover != null ? days[hover] : null;
  return (
    <div>
      <div className="mb-2 flex items-center gap-4 text-xs text-ink-2">
        <span className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-[var(--chart-ok)]" /><CheckCircle2 className="size-3.5 text-ink-3" />Succeeded</span>
        <span className="inline-flex items-center gap-1.5"><svg width="10" height="10" aria-hidden><rect width="10" height="10" rx="2" fill="url(#hatch-legend)" /><defs><pattern id="hatch-legend" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="4" height="4" style={{ fill: "var(--chart-bad)" }} /><rect width="1.4" height="4" fill="rgb(255 255 255 / 0.45)" /></pattern></defs></svg><XCircle className="size-3.5 text-ink-3" />Failed</span>
      </div>
      <div ref={ref} className="relative" onMouseLeave={() => setHover(null)}>
        <svg width={w} height={H} role="img" aria-label="Pipeline runs per day, succeeded and failed">
          <defs>
            <pattern id="hatch-bad" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="5" height="5" style={{ fill: "var(--chart-bad)" }} /><rect width="1.6" height="5" fill="rgb(255 255 255 / 0.4)" />
            </pattern>
          </defs>
          {ys.map((t) => (
            <g key={t}>
              <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} style={{ stroke: "var(--line)" }} strokeDasharray={t ? "3 4" : undefined} />
              <text x={pad.l - 6} y={y(t) + 3.5} textAnchor="end" className="fill-[var(--ink-3)] text-[10px]">{t}</text>
            </g>
          ))}
          {days.map((d, i) => {
            const x = pad.l + i * slot + (slot - bw) / 2;
            const ok = d.succeeded, bad = d.failed;
            const yOk = y(ok), yAll = y(ok + bad);
            return (
              <g key={d.date} opacity={hover == null || hover === i ? 1 : 0.45}>
                {ok > 0 && <rect x={x} y={yOk} width={bw} height={y(0) - yOk} rx={bad ? 0 : 3} style={{ fill: "var(--chart-ok)" }} />}
                {bad > 0 && <rect x={x} y={yAll} width={bw} height={Math.max(2, yOk - yAll - (ok ? 2 : 0))} rx={3} fill="url(#hatch-bad)" />}
                <rect x={pad.l + i * slot} y={pad.t} width={slot} height={ih} fill="transparent" onMouseEnter={() => setHover(i)} />
                {i % every === 0 && <text x={pad.l + i * slot + slot / 2} y={H - 6} textAnchor="middle" className="fill-[var(--ink-3)] text-[10px]">{dayLabel(d.date)}</text>}
              </g>
            );
          })}
        </svg>
        {h && (
          <div className="pointer-events-none absolute top-0 z-10 rounded-lg border border-line-strong bg-[var(--overlay-bg)] px-2.5 py-1.5 text-xs shadow-float backdrop-blur"
            style={{ left: Math.min(w - 150, Math.max(0, pad.l + hover! * slot - 40)) }}>
            <p className="font-semibold text-ink">{dayLabel(h.date)}</p>
            <p className="text-ink-2">{h.succeeded} succeeded · {h.failed} failed</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── rows delivered per day (one series) ──
function RowsChart({ days }: { days: Day[] }) {
  const [ref, w] = useBox<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const H = 170, pad = { l: 40, r: 8, t: 8, b: 22 };
  const max = Math.max(1, ...days.map((d) => d.rows));
  const ys = ticks(max);
  const top = ys[ys.length - 1]!;
  const iw = w - pad.l - pad.r, ih = H - pad.t - pad.b;
  const slot = iw / days.length, bw = Math.max(3, Math.min(22, slot - 4));
  const y = (v: number) => pad.t + ih - (v / top) * ih;
  const every = Math.ceil(days.length / 8);
  const h = hover != null ? days[hover] : null;
  return (
    <div ref={ref} className="relative" onMouseLeave={() => setHover(null)}>
      <svg width={w} height={H} role="img" aria-label="Rows delivered per day">
        {ys.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} style={{ stroke: "var(--line)" }} strokeDasharray={t ? "3 4" : undefined} />
            <text x={pad.l - 6} y={y(t) + 3.5} textAnchor="end" className="fill-[var(--ink-3)] text-[10px]">{short(t)}</text>
          </g>
        ))}
        {days.map((d, i) => (
          <g key={d.date} opacity={hover == null || hover === i ? 1 : 0.45}>
            {d.rows > 0 && <rect x={pad.l + i * slot + (slot - bw) / 2} y={y(d.rows)} width={bw} height={y(0) - y(d.rows)} rx={3} style={{ fill: "var(--chart-ok)" }} />}
            <rect x={pad.l + i * slot} y={pad.t} width={slot} height={ih} fill="transparent" onMouseEnter={() => setHover(i)} />
            {i % every === 0 && <text x={pad.l + i * slot + slot / 2} y={H - 6} textAnchor="middle" className="fill-[var(--ink-3)] text-[10px]">{dayLabel(d.date)}</text>}
          </g>
        ))}
      </svg>
      {h && (
        <div className="pointer-events-none absolute top-0 z-10 rounded-lg border border-line-strong bg-[var(--overlay-bg)] px-2.5 py-1.5 text-xs shadow-float backdrop-blur"
          style={{ left: Math.min(w - 140, Math.max(0, pad.l + hover! * slot - 40)) }}>
          <p className="font-semibold text-ink">{dayLabel(h.date)}</p><p className="text-ink-2">{num(h.rows)} rows</p>
        </div>
      )}
    </div>
  );
}

// ── why runs failed: ranked ──
function Reasons({ items, total }: { items: Mgmt["failure_reasons"]; total: number }) {
  if (!items.length) return <p className="text-sm text-ink-3">No failed runs in this period.</p>;
  const max = Math.max(...items.map((r) => r.count));
  return (
    <ul className="space-y-3.5">
      {items.slice(0, 6).map((r) => (
        <li key={r.reason}>
          <div className="flex items-baseline justify-between gap-3">
            <p className="text-[0.8125rem] font-medium leading-snug text-ink first-letter:uppercase">{r.reason}</p>
            <p className="shrink-0 text-sm font-semibold tabular-nums text-ink">{r.count}<span className="ml-1 text-xs font-normal text-ink-3">{Math.round((r.count / total) * 100)}%</span></p>
          </div>
          <div className="mt-1.5 h-2 rounded-full bg-surface-3"><div className="h-2 rounded-full" style={{ width: `${(r.count / max) * 100}%`, background: "var(--chart-bad)" }} /></div>
          <p className="mt-1 text-[0.71875rem] text-ink-3">Step {r.steps.join(", ") || "–"} · last {ago(r.last)}</p>
        </li>
      ))}
    </ul>
  );
}

const STATE: Record<string, { label: string; icon: typeof CheckCircle2; color: string }> = {
  ok: { label: "Loaded, normal", icon: CheckCircle2, color: "var(--good)" },
  unusual: { label: "Loaded, unusual volume", icon: AlertTriangle, color: "var(--warning)" },
  late: { label: "No load in over a day", icon: Clock, color: "var(--warning)" },
  issue: { label: "Open incident", icon: OctagonAlert, color: "var(--critical)" },
};

// ── delivery scorecard: table × environment ──
function Scorecard({ tables, projectKey }: { tables: Mgmt["tables"]; projectKey: string }) {
  const envs = ["PROD", "UAT", "DEV"].filter((e) => tables.some((t) => t.env === e));
  const cols = envs.length ? envs : [null];
  const names = [...new Set(tables.map((t) => t.name))];
  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
        {Object.entries(STATE).map(([k, s]) => <span key={k} className="inline-flex items-center gap-1.5"><s.icon className="size-3.5" style={{ color: s.color }} />{s.label}</span>)}
        {tables.some((t) => t.quality) && <>
          <span className="inline-flex items-center gap-1.5"><ShieldCheck className="size-3.5 text-[var(--good)]" />Data checks pass</span>
          <span className="inline-flex items-center gap-1.5"><ShieldAlert className="size-3.5 text-[var(--critical)]" />Data checks failing</span>
        </>}
      </div>
      <div className="overflow-auto">
        <table className="data-table min-w-[36rem]">
          <thead><tr><th>Table</th>{cols.map((e) => <th key={e ?? "all"}>{e ? <EnvBadge env={e} /> : "Latest load"}</th>)}</tr></thead>
          <tbody>
            {names.map((n) => (
              <tr key={n}>
                <td><Link to={`/p/${projectKey}/tables`} className="font-semibold text-ink hover:underline">{n}</Link>
                  <span className="block text-[0.6875rem] text-ink-3">{tables.find((t) => t.name === n)!.pipeline}</span></td>
                {cols.map((e) => {
                  const t = tables.find((x) => x.name === n && (e == null || x.env === e));
                  if (!t) return <td key={e ?? "all"} className="text-xs text-ink-3">–</td>;
                  const s = STATE[t.state]!;
                  return (
                    <td key={e ?? "all"} title={`${s.label} · ${dateTime(t.last_at)}`}>
                      <span className="inline-flex items-center gap-1.5"><s.icon className="size-4 shrink-0" style={{ color: s.color }} aria-label={s.label} />
                        <span className="font-semibold tabular-nums text-ink">{num(t.rows)}</span>
                        {t.quality?.status === "fail" && <span title={`Data checks failing: ${t.quality.failing.join(", ")}`}><ShieldAlert className="size-3.5 text-[var(--critical)]" aria-label={`Data checks failing: ${t.quality.failing.join(", ")}`} /></span>}
                        {t.quality?.status === "ok" && <span title="Data checks pass"><ShieldCheck className="size-3.5 text-[var(--good)]" aria-label="Data checks pass" /></span>}
                      </span>
                      <span className="block text-[0.6875rem] text-ink-3">{ago(t.last_at)}{t.normal ? ` · usual ${short(Math.round(t.normal))}` : ""}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const pctDelta = (now: number | null, before: number | null, higherIsBetter = true, unit = "pts") => {
  if (now == null || before == null) return null;
  const d = now - before;
  if (Math.abs(d) < 0.05) return null;
  return { text: `${d > 0 ? "+" : ""}${unit === "%" ? Math.round(d) + "%" : d.toFixed(1) + " pts"} vs previous`, good: higherIsBetter ? d > 0 : d < 0 };
};

export function ManagementOverview({ sample }: { sample?: Mgmt } = {}) {
  const { key = "" } = useParams();
  const [days, setDays] = useState<7 | 30 | 90>(30);
  const api = useApi<Mgmt>(sample ? null : `/api/projects/${key}/management?days=${days}`, { refreshMs: 60000 });
  const data = sample ?? api.data;
  const error = api.error;

  if (error && !data) return <Notice tone="warn">Couldn't load this project: {error.message}</Notice>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-36 w-full rounded-[1.5rem]" /><Skeleton className="h-28 w-full" /><Skeleton className="h-80 w-full" /></div>;
  const k = data.kpis;
  const lvl = data.status.level;
  const LvlIcon = lvl === "critical" ? OctagonAlert : lvl === "warning" ? AlertTriangle : CheckCircle2;
  const rowsDelta = k.rows_prev_7d ? Math.round((k.rows_7d / k.rows_prev_7d - 1) * 100) : null;
  const dq = data.quality?.enabled ? data.quality : null;

  return (
    <div className="mgmt">
      {/* status banner */}
      <section className={cn("relative overflow-hidden rounded-[1.5rem] p-6 shadow-[inset_0_0_0_1px_var(--line)]",
        lvl === "critical" ? "bg-[var(--critical-soft)]" : lvl === "warning" ? "bg-[var(--warning-soft)]" : "bg-[var(--good-soft)]")}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="eyebrow flex items-center gap-1.5 font-semibold text-ink-2"><Gauge className="size-3.5" /> {data.project.name} · last {data.period_days} days</p>
          <div className="flex flex-wrap items-center gap-2 print:hidden">
            <Segmented label="Period" value={days} onChange={setDays} options={[{ value: 7, label: "7 days" }, { value: 30, label: "30 days" }, { value: 90, label: "90 days" }]} />
            <Link to={`/p/${key}/report`}><Button size="sm" variant="secondary"><FileText />Morning report</Button></Link>
            <Button size="sm" variant="secondary" onClick={() => window.print()}><Printer />Print / PDF</Button>
          </div>
        </div>
        <div className="mt-3 flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0 max-w-3xl flex-1">
            <h1 className="display mt-2 flex items-start gap-2.5 text-[1.5rem] font-bold leading-tight tracking-[-0.02em] text-ink">
              <LvlIcon className="mt-1 size-6 shrink-0" style={{ color: lvl === "critical" ? "var(--critical)" : lvl === "warning" ? "var(--warning)" : "var(--good)" }} aria-label={lvl} />
              {data.status.headline}
            </h1>
            <ul className="mt-3 space-y-1 text-sm text-ink-2">{data.status.reasons.map((r) => <li key={r}>• {r}</li>)}</ul>
          </div>
          {data.health && <HealthGauge health={data.health} />}
        </div>
        <p className="mt-4 text-xs text-ink-3">Updated {ago(data.generated_at)} · {dateTime(data.generated_at)}</p>
      </section>

      {/* KPIs */}
      <div className={cn("mt-4 grid grid-cols-2 gap-3 md:grid-cols-3", dq ? "xl:grid-cols-4 2xl:grid-cols-7" : "xl:grid-cols-6")}>
        <Kpi label="Pipeline success rate" icon={Workflow} tone={k.success_rate == null ? undefined : k.success_rate >= 90 ? "good" : k.success_rate >= 70 ? "warning" : "critical"}
          value={k.success_rate == null ? "–" : <>{Math.round(k.success_rate)}<span className="text-base text-ink-3">%</span></>}
          delta={pctDelta(k.success_rate, k.success_rate_prev)} />
        <Kpi label="Pipeline runs" icon={ListChecks} value={num(k.runs)}
          sub={<span className={k.failed ? "text-[var(--critical-ink)]" : undefined}>{k.failed} failed{k.runs_prev ? ` · ${k.failed_prev} of ${k.runs_prev} before` : ""}</span>} />
        {k.tables > 0 && (
          <Kpi label="Tables delivered (24 h)" icon={Database} tone={k.tables_behind ? "warning" : "good"}
            value={<>{k.tables_on_time}<span className="text-base text-ink-3"> / {k.tables}</span></>}
            sub={k.tables_behind ? <span className="text-[var(--warning-ink)]">{k.tables_behind} late or need a look</span> : "all on time"} />
        )}
        {dq && (
          <Kpi label="Data quality" icon={dq.summary.failing ? ShieldAlert : ShieldCheck}
            tone={dq.status?.unavailable ? "warning" : dq.summary.failing ? "critical" : dq.summary.tables ? "good" : undefined}
            value={dq.summary.score == null ? "–" : <>{dq.summary.score}<span className="text-base text-ink-3">/100</span></>}
            sub={dq.status?.unavailable ? <span className="text-[var(--warning-ink)]">warehouse not reachable</span>
              : dq.summary.tables ? <span className={dq.summary.failing ? "text-[var(--critical-ink)]" : undefined}>{dq.summary.failing} of {dq.summary.tables} tables failing checks</span>
              : "waiting for the first check"} />
        )}
        <Kpi label={`Rows delivered (7 days)`} icon={Database} value={short(Math.round(k.rows_7d))}
          delta={rowsDelta != null && rowsDelta !== 0 ? { text: `${rowsDelta > 0 ? "+" : ""}${rowsDelta}% vs prior week`, good: rowsDelta > 0 } : null}
          sub={k.rows_scope} />
        <Kpi label="Need attention" icon={Siren} tone={k.open_critical ? "critical" : k.open_warning ? "warning" : "good"}
          value={k.open_critical + k.open_warning}
          sub={<>{k.open_critical} critical · {k.open_warning} warning · {k.being_handled ?? 0} being handled · {k.incidents_opened} opened in period</>} />
        <Kpi label="Last fully successful run" icon={Timer} tone={k.last_full_success_at && Date.now() - new Date(k.last_full_success_at).getTime() > 48 * 3600e3 ? "warning" : "good"}
          value={k.last_full_success_at ? ago(k.last_full_success_at) : "never"}
          sub={k.mttr_hours != null ? `incidents fixed in ${k.mttr_hours} h on average (${k.fixed})` : "no incidents fixed in period yet"} />
      </div>

      {/* trends */}
      <div className="mt-4 grid grid-cols-12 gap-4">
        <ChartCard className="col-span-12 xl:col-span-7" icon={Workflow} title="Pipeline runs per day" subtitle={`${num(k.runs)} runs · ${k.failed} failed · hover a day for details`}>
          <RunsChart days={data.daily} />
        </ChartCard>
        <ChartCard className="col-span-12 xl:col-span-5" icon={XCircle} title="Why runs failed" subtitle={k.failed ? `${k.failed} failed runs, grouped by cause` : "No failures"}>
          <Reasons items={data.failure_reasons} total={Math.max(1, k.failed)} />
        </ChartCard>
        <ChartCard className="col-span-12" icon={Database} title="Rows delivered per day" subtitle={`${k.rows_scope} · rows copied by successful load steps`}>
          <RowsChart days={data.daily} />
        </ChartCard>
      </div>

      {/* delivery scorecard + pipelines */}
      <div className="mt-4 grid grid-cols-12 gap-4">
        {data.tables.length > 0 && (
          <ChartCard className="col-span-12 xl:col-span-7" icon={Database} title="Delivery scorecard" subtitle="Each table's latest load, per environment">
            <Scorecard tables={data.tables} projectKey={key} />
          </ChartCard>
        )}
        <div className={cn("col-span-12 space-y-4", data.tables.length > 0 && "xl:col-span-5")}>
          <ChartCard icon={Workflow} title={data.pipelines.some((p) => p.type === "logic_app") ? "Logic Apps" : "Pipelines"}
            subtitle={`${data.pipelines.length} available · ${data.pipelines.filter((p) => p.runs).length} ran in the last ${data.period_days} days`}
            actions={<Link to="/pipelines" className="text-xs font-semibold text-accent hover:underline">All pipelines</Link>}
            empty={!data.pipelines.length} emptyText="No pipelines found yet.">
            <div className="space-y-3">
              {data.pipelines.filter((p) => p.runs).map((p) => (
                <div key={p.name} className="rounded-xl bg-surface-2 p-3 shadow-[inset_0_0_0_1px_var(--line)]">
                  <div className="flex items-center justify-between gap-2">
                    <Link to={`/p/${key}/runs`} className="truncate font-semibold text-ink hover:underline">{p.name}</Link>
                    <StatusChip status={p.last_status} />
                  </div>
                  <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface-3" title={`${p.success_rate ?? 0}% succeeded`}>
                    <div className="h-2" style={{ width: `${p.success_rate ?? 0}%`, background: "var(--chart-ok)" }} />
                  </div>
                  <p className="mt-1.5 text-xs text-ink-2">
                    <b className="text-ink">{p.success_rate != null ? `${Math.round(p.success_rate)}%` : "–"}</b> succeeded · {p.runs} runs, {p.failed} failed
                    {p.failing_streak >= 2 && <span className="text-[var(--critical-ink)]"> · failed the last {p.failing_streak} in a row</span>}
                  </p>
                  <p className="mt-0.5 text-[0.71875rem] text-ink-3">
                    Last run {ago(p.last_run_at)} · last success {p.last_success_at ? ago(p.last_success_at) : "–"}
                    {p.median_duration_s ? ` · usually ${duration(p.median_duration_s)}` : ""}{p.trigger ? ` · ${p.trigger}` : ""}
                  </p>
                </div>
              ))}
              {data.pipelines.some((p) => !p.runs) && (
                <div>
                  <p className="mb-1.5 mt-1 text-xs font-semibold text-ink-2">No runs in the last {data.period_days} days</p>
                  <ul className="divide-y divide-line rounded-xl shadow-[inset_0_0_0_1px_var(--line)]">
                    {data.pipelines.filter((p) => !p.runs).map((p) => (
                      <li key={p.name} className="flex items-center justify-between gap-2 px-3 py-2">
                        <span className="min-w-0">
                          <Link to={`/p/${key}/runs?pipeline=${encodeURIComponent(p.name)}`} className="block truncate text-[0.8125rem] font-medium text-ink hover:underline">{p.name}</Link>
                          <span className="block truncate text-[0.6875rem] text-ink-3">
                            {[p.folder, p.steps != null ? `${p.steps} steps` : null,
                              p.triggers?.length ? p.triggers.map((t) => t.kind + (t.started ? "" : " (stopped)")).join(", ") : "manual only"].filter(Boolean).join(" · ")}
                          </span>
                        </span>
                        <span className="shrink-0 text-[0.6875rem] text-ink-3">{p.state === "Disabled" ? "Disabled" : p.last_run_at ? `last ran ${ago(p.last_run_at)}` : "No run on record"}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </ChartCard>
          <ChartCard icon={Siren} title="Open incidents" subtitle={`Worst first · ${data.incidents.filter((i) => i.status === "open").length} need attention · ${data.incidents.filter((i) => i.status === "acknowledged").length} being handled`}
            empty={!data.incidents.length} emptyText="No open incidents." emptyTone="good">
            <ul className="divide-y divide-line">
              {data.incidents.map((i) => (
                <li key={i.id} className="py-2.5 first:pt-0 last:pb-0">
                  <div className="flex items-center gap-2"><SeverityPill severity={i.severity} />
                    <Link to={`/p/${key}/i/${i.id}`} className="min-w-0 truncate text-sm font-semibold text-ink hover:underline">{i.title}</Link></div>
                  <p className="mt-1 line-clamp-2 text-xs text-ink-2">{i.likely_cause.split("\n")[0]}</p>
                  <p className="mt-0.5 text-[0.71875rem] text-ink-3">Open {i.age_hours < 48 ? `${Math.round(i.age_hours)} h` : `${Math.round(i.age_hours / 24)} days`}{i.status === "acknowledged" ? ` · acknowledged by ${i.acknowledged_by ?? "someone"}: being handled` : " · nobody has acknowledged it yet"}</p>
                </li>
              ))}
            </ul>
          </ChartCard>
        </div>
      </div>
    </div>
  );
}
