// Pipeline report: the team's daily pipeline summary (runs per day, data volume per day, today so far against the
// usual) rebuilt from what the bot watches, plus what a static report can't say: who's overdue, today's pace, the
// errors that keep coming back, and when in the week things run and fail. Colours follow the pipeline (fixed order),
// never its rank; status colours are only used for status.
import {
  Activity, AlertTriangle, ArrowDownRight, ArrowRight, ArrowUpRight, CalendarClock, CheckCircle2, Clock3, Database, Flame, Gauge, Grid3x3, HeartPulse, Info,
  ListChecks, Loader2, Siren, Sparkles, Timer, TrendingUp, Wrench, XCircle, type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Metric, MetricStrip } from "@/components/Cards";
import { RunMap, type MapMeta, type MapPipe } from "@/components/RunMap";
import { Button } from "@/components/ui/button";
import { EmptyState, Segmented, Skeleton } from "@/components/ui/primitives";
import { ago, duration, time } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import { cn } from "@/lib/utils";

// ── shapes ───────────────────────────────────────────────────────────────────
type State = "failing" | "late" | "handled" | "unstable" | "running" | "healthy";
interface Day { date: string; ok: number; failed: number; other: number; rows: number }
interface Pipe extends Pick<MapPipe, "grid" | "incidents"> {
  key: string; project: string; project_name: string; name: string; trigger: string | null; state: State; enabled: boolean; daily: Day[];
  today: { ok: number; failed: number; rows: number; typical_by_now: number | null; typical_day: number | null; pace: number | null };
  last: { at: string | null; status: string; duration_s: number | null; run_id: string; error: string | null };
  runtime: { verdict?: string; pct?: number; median_s: number; running?: boolean; elapsed_s?: number } | null;
  duration: { p50: number | null; p95: number | null };
  cadence: { interval_s: number | null; since_last_s: number; next_at: string | null; overdue: boolean };
  runs: number; ok: number; failed: number; success_rate: number | null; success_rate_prev: number | null;
  incident: { id: number; title: string; severity: string; status: string } | null;
  handled: { kind: string; by: string | null } | null;
  snowflake: { monthly: number; per_run: number | null; how: string } | null;
}
interface Vol {
  key: string; project: string; project_name: string; table: string; short: string; pipeline: string; daily: { date: string; rows: number }[];
  today: number; typical_by_now: number | null; typical_day: number | null; avg_7d: number | null; pct_vs_typical: number | null;
  verdict: "none" | "low" | "high" | "normal" | null; period_total: number; last_load: { at: string | null; rows: number };
}
interface Sig {
  key: string; title: string | null; known: boolean; sample: string; steps: string[]; count: number; pipelines: { project: string; name: string }[];
  first_at: string; last_at: string; incident: { id: number; project: string; title: string; status: string } | null;
  last_run?: { project: string; run_id: string; pipeline: string };
}
interface Insight { tone: "critical" | "warning" | "info" | "good"; kind: string; title: string; detail: string; link: Record<string, string | number> | null }
interface Report {
  days: number; timezone: string; generated_at: string; today: string; calendar: string[];
  summary: {
    pipelines: number; quiet: number; tables: number; states: Record<State, number>; success_rate: number | null; success_rate_prev: number | null;
    runs: number; failed: number; runs_today: number; failed_today: number; runs_typical_by_now: number; rows_today: number; rows_typical_by_now: number;
    rows_period: number; open_incidents: number; incidents_opened: number; incidents_resolved: number; mttr_hours: number | null;
  };
  insights: Insight[]; pipelines: Pipe[]; volumes: Vol[];
  errors: { daily: { date: string; total: number; by: Record<string, number> }[]; signatures: Sig[];
    recent: { at: string; project: string; pipeline: string; run_id: string; status: string; error: string; incident_id: number | null }[] };
  heatmap: { runs: number; failed: number }[][];
  grid: MapMeta;
  open_incidents: { id: number; project: string; title: string; severity: string; status: string; opened_at: string }[];
}

// ── helpers ──────────────────────────────────────────────────────────────────
const nf = new Intl.NumberFormat("en-US");
const cf = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const n = (v: number) => (Math.abs(v) < 10000 ? nf.format(Math.round(v)) : cf.format(v));
const pc = (v: number | null | undefined, d = 0) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
/** "2026-10-22" as a local calendar day (not UTC midnight). */
const dayOf = (s: string) => { const [y, m, d] = s.split("-").map(Number); return new Date(y!, m! - 1, d!); };
const wd = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const dm = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const every = (s: number | null) => (s == null ? null : s < 5400 ? `${Math.round(s / 60)} min` : s < 129600 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} days`);
const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const STATE: Record<State, { label: string; color: string; icon: LucideIcon }> = {
  failing: { label: "Failing", color: "var(--critical)", icon: XCircle },
  late: { label: "Overdue", color: "var(--serious, var(--critical))", icon: Clock3 },
  handled: { label: "Being handled", color: "var(--accent)", icon: Wrench },
  unstable: { label: "Some failures", color: "var(--warning)", icon: AlertTriangle },
  running: { label: "Running", color: "var(--cat-1)", icon: Loader2 },
  healthy: { label: "Healthy", color: "var(--good)", icon: CheckCircle2 },
};
const TONE: Record<Insight["tone"], { color: string; soft: string; icon: LucideIcon }> = {
  critical: { color: "var(--critical)", soft: "var(--critical-soft, color-mix(in srgb, var(--critical) 12%, transparent))", icon: Siren },
  warning: { color: "var(--warning)", soft: "var(--warning-soft)", icon: AlertTriangle },
  info: { color: "var(--cat-1)", soft: "color-mix(in srgb, var(--cat-1) 10%, transparent)", icon: Info },
  good: { color: "var(--good)", soft: "var(--good-soft)", icon: Sparkles },
};

function useCountUp(target: number, ms = 900) {
  const [v, setV] = useState(target);
  const from = useRef(0);
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { setV(target); return; }
    const start = performance.now(), a = from.current;
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / ms), e = 1 - Math.pow(1 - k, 3);
      setV(a + (target - a) * e);
      if (k < 1) raf = requestAnimationFrame(step); else from.current = target;
    };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); from.current = target; };
  }, [target, ms]);
  return v;
}
const Count = ({ value, fmt }: { value: number; fmt: (v: number) => string }) => <>{fmt(useCountUp(value))}</>;

/** One hover card for the whole page: charts call show() with content, it follows the pointer and stays on screen. */
type Tip = { x: number; y: number; body: ReactNode } | null;
function HoverCard({ tip }: { tip: Tip }) {
  if (!tip) return null;
  const left = Math.min(tip.x + 14, window.innerWidth - 280);
  return <div className="ai-tip pointer-events-none fixed z-[80] max-w-[17rem] text-[0.75rem]" style={{ left, top: Math.max(8, tip.y - 12), transform: "translateY(-100%)" }}>{tip.body}</div>;
}

const linkOf = (l: Insight["link"]): string | null => {
  if (!l) return null;
  if (l.kind === "incident") return `/p/${l.project}/i/${l.id}`;
  if (l.kind === "pipeline") return `/p/${l.project}/runs?pipeline=${encodeURIComponent(String(l.name))}`;
  if (l.kind === "table") return `/p/${l.project}/tables`;
  if (l.kind === "errors") return "#errors";
  return null;
};

type Period = 4 | 7 | 14 | 30;
const readPeriod = (): Period => { try { const v = Number(localStorage.getItem("alertbot-pr-days")); return ([4, 7, 14, 30].includes(v) ? v : 7) as Period; } catch { return 7; } };

// ── the page ─────────────────────────────────────────────────────────────────
export function PipelineReport() {
  const [days, setDaysRaw] = useState<Period>(readPeriod);
  const setDays = (d: Period) => { setDaysRaw(d); try { localStorage.setItem("alertbot-pr-days", String(d)); } catch { /* storage blocked */ } };
  const { data, error, reload } = useApi<Report>(`/api/pipeline-report?days=${days}`, { refreshMs: 60000 });
  const [tip, setTip] = useState<Tip>(null);
  useNow();

  // colour follows the pipeline: a fixed order by name, so a filter or a new period never repaints the others
  const colorOf = useMemo(() => {
    const keys = [...new Set((data?.pipelines ?? []).map((p) => p.key))].sort();
    // 7 hues, ordered so neighbours differ most (the two greens, 3 and 6, never sit side by side); the palette's 8th is
    // a red that would read as "failed" next to the failure bars, so it isn't used
    const ORDER = [1, 2, 7, 4, 3, 5, 6];
    return (key: string) => { const i = keys.indexOf(key); return i >= 0 && i < ORDER.length ? `var(--cat-${ORDER[i]})` : "var(--ink-3)"; };
  }, [data]);

  if (error && !data) return <div className="rounded-2xl bg-[var(--warning-soft)] p-4 text-sm text-ink">Couldn't load the pipeline report: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></div>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-64 w-full rounded-[1.75rem]" /><Skeleton className="h-28 w-full" /><Skeleton className="h-96 w-full" /></div>;
  const s = data.summary;
  const show = (e: React.MouseEvent, body: ReactNode) => setTip({ x: e.clientX, y: e.clientY, body });
  const hide = () => setTip(null);

  return (
    <div className="pr pb-24" onMouseLeave={hide}>
      <Hero data={data} days={days} setDays={setDays} />
      {data.pipelines.length === 0 ? (
        <EmptyState icon={Activity} action={<Button asChild variant="primary"><Link to="/pipelines">See all pipelines <ArrowRight /></Link></Button>}>
          No pipeline has run in the last {days} days in the projects you can see. Once one runs, its report appears here.
        </EmptyState>
      ) : (
        <>
          <MetricStrip className="mb-5" cols="grid-cols-2 lg:grid-cols-4">
            <Metric icon={HeartPulse} iconColor="var(--good)" label="Pipelines" value={<Count value={s.pipelines} fmt={(v) => nf.format(Math.round(v))} />}
              footnote={<StateSplit states={s.states} />} />
            <Metric icon={Gauge} iconColor="var(--cat-1)" label="Success rate" value={pc(s.success_rate, s.success_rate != null && s.success_rate < 0.995 ? 1 : 0)}
              footnote={<Delta now={s.success_rate} before={s.success_rate_prev} label="the period before" />} />
            <Metric icon={ListChecks} iconColor="var(--cat-2)" label={`Runs, last ${days} days`} value={<Count value={s.runs} fmt={(v) => nf.format(Math.round(v))} />}
              footnote={`${nf.format(s.failed)} failed · ${n(s.rows_period)} rows loaded`} />
            <Metric icon={Siren} iconColor="var(--critical)" label="Open incidents" value={nf.format(s.open_incidents)}
              footnote={`${s.incidents_opened} opened, ${s.incidents_resolved} resolved${s.mttr_hours != null ? ` · fixed in ${s.mttr_hours < 48 ? `${s.mttr_hours} h` : `${Math.round(s.mttr_hours / 24)} days`} (median)` : ""}`} />
          </MetricStrip>

          {data.insights.length > 0 && <Insights items={data.insights} />}

          <Section id="runmap" icon={Grid3x3} title={`Run map, last ${days} days`}
            hint="Every pipeline across the period, one cell per time slot. Pick what colours the cells; failures always show. Hover a cell for what happened, click it to open the run.">
            <RunMap pipes={data.pipelines} meta={data.grid} calendar={data.calendar} today={data.today} colorOf={colorOf} show={show} hide={hide} timezone={data.timezone} />
          </Section>

          <TodaySoFar data={data} colorOf={colorOf} />

          <Section id="execution" icon={ListChecks} title={`Pipeline execution, last ${days} days`}
            hint="Successful runs per day, failures stacked on top in red. Each row is scaled to its own busiest day: compare the shape within a pipeline, not bar heights across pipelines.">
            <ExecutionTable data={data} colorOf={colorOf} show={show} hide={hide} />
          </Section>

          {data.volumes.length > 0 && (
            <Section id="volume" icon={Database} title={`Data volume, last ${days} days`}
              hint="Rows each table received per day. Each row is scaled to its own range, like the report: compare the shape within a table.">
              <VolumeTable data={data} colorOf={colorOf} show={show} hide={hide} />
            </Section>
          )}

          <Section id="errors" icon={Flame} title="Errors" hint="What failed, grouped by what went wrong, so the error that keeps coming back stands out.">
            <Errors data={data} colorOf={colorOf} show={show} hide={hide} />
          </Section>

          <Section id="rhythm" icon={CalendarClock} title="When pipelines run"
            hint={`Every run in the last ${days} days by weekday and hour (${data.timezone}). Darker is busier; a red dot means something failed in that hour.`}>
            <Heatmap heat={data.heatmap} show={show} hide={hide} />
          </Section>

          <p className="mt-4 text-center text-[0.71875rem] text-ink-3">
            Built from every run the bot watched · days are whole days in {data.timezone} · updated {ago(data.generated_at)}
            {s.quiet ? ` · ${s.quiet} more pipeline${s.quiet === 1 ? "" : "s"} didn't run in this period` : ""}
          </p>
        </>
      )}
      <HoverCard tip={tip} />
    </div>
  );
}

// ── hero ─────────────────────────────────────────────────────────────────────
function Hero({ data, days, setDays }: { data: Report; days: Period; setDays: (d: Period) => void }) {
  const s = data.summary;
  const attention = s.states.failing + s.states.late;
  const headline = !s.pipelines ? "Nothing ran yet" : attention ? `${attention} of ${s.pipelines} pipeline${s.pipelines === 1 ? "" : "s"} need${attention === 1 ? "s" : ""} attention`
    : s.states.unstable ? "Running, with a few failures" : "Everything is running as usual";
  const rate = s.success_rate ?? 0;
  return (
    <section className="ai-hero pr-hero card-in relative mb-5 overflow-hidden rounded-[1.75rem] p-6 sm:p-8">
      <div className="ai-hero-glow pr-glow" aria-hidden />
      <div className="relative flex flex-wrap items-start justify-between gap-3">
        <p className="eyebrow flex items-center gap-2 font-semibold text-accent"><HeartPulse className="size-3.5" /> Pipeline report</p>
        <Segmented<Period> size="sm" label="Period" value={days} onChange={setDays}
          options={[{ value: 4, label: "4 days" }, { value: 7, label: "7 days" }, { value: 14, label: "14 days" }, { value: 30, label: "30 days" }]} />
      </div>
      <div className="relative mt-4 grid gap-6 lg:grid-cols-[auto_minmax(0,1fr)_minmax(0,1.05fr)] lg:items-center">
        <Ring value={s.success_rate} />
        <div className="min-w-0">
          <h1 className="display text-[1.75rem] font-bold leading-[1.1] tracking-[-0.02em] text-ink sm:text-[2.125rem]">{headline}</h1>
          <p className="mt-2 text-[0.9375rem] text-ink-2">
            <b className="font-semibold text-ink">{pc(rate, rate < 0.995 ? 1 : 0)}</b> of {nf.format(s.runs)} runs succeeded in the last {days} days
            {s.success_rate_prev != null && <> · <Delta now={s.success_rate} before={s.success_rate_prev} label="before" inline /></>}
          </p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {(Object.keys(STATE) as State[]).filter((k) => s.states[k]).map((k) => {
              const I = STATE[k].icon;
              return <span key={k} className="pr-state"><I className="size-3.5" style={{ color: STATE[k].color }} aria-hidden />{s.states[k]} {STATE[k].label.toLowerCase()}</span>;
            })}
          </div>
        </div>
        <div className="ai-whatif min-w-0 rounded-2xl p-4 sm:p-5">
          <p className="flex items-center justify-between text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">
            Today so far <span className="normal-case tracking-normal font-normal">{time(data.generated_at)}</span>
          </p>
          <Pace label="Successful runs" value={s.runs_today} typical={s.runs_typical_by_now} fmt={(v) => nf.format(Math.round(v))} />
          {data.volumes.length > 0 && <Pace label="Rows loaded" value={s.rows_today} typical={s.rows_typical_by_now} fmt={n} />}
          <div className="mt-3 flex items-center justify-between text-[0.8125rem]">
            <span className="text-ink-2">Failed runs today</span>
            <span className={cn("font-semibold tabular-nums", s.failed_today ? "text-[var(--critical)]" : "text-ink")}>{s.failed_today}</span>
          </div>
        </div>
      </div>
    </section>
  );
}

function Ring({ value }: { value: number | null }) {
  const v = useCountUp(value ?? 0);
  const r = 46, c = 2 * Math.PI * r;
  const color = value == null ? "var(--ink-3)" : value >= 0.97 ? "var(--good)" : value >= 0.85 ? "var(--warning)" : "var(--critical)";
  return (
    <div className="relative mx-auto size-[8.5rem] shrink-0 lg:mx-0" role="img" aria-label={`Success rate ${pc(value)}`}>
      <svg viewBox="0 0 108 108" className="size-full -rotate-90">
        <circle cx="54" cy="54" r={r} fill="none" stroke="var(--surface-3)" strokeWidth="9" />
        <circle cx="54" cy="54" r={r} fill="none" stroke={color} strokeWidth="9" strokeLinecap="round" strokeDasharray={`${c * v} ${c}`} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="display text-[1.75rem] font-bold leading-none tabular-nums text-ink">{value == null ? "–" : `${Math.round(v * 100)}%`}</span>
        <span className="mt-1 text-[0.6875rem] font-medium uppercase tracking-[0.08em] text-ink-3">succeeded</span>
      </div>
    </div>
  );
}

/** Today's count against what's usual by this time of day; the tick marks "usual so far". */
function Pace({ label, value, typical, fmt }: { label: string; value: number; typical: number | null; fmt: (v: number) => string }) {
  const max = Math.max(value, typical ?? 0, 1) * 1.15;
  const ratio = typical ? value / typical : null;
  return (
    <div className="mt-3">
      <div className="flex items-baseline justify-between gap-2 text-[0.8125rem]">
        <span className="text-ink-2">{label}</span>
        <span className="tabular-nums"><b className="text-[1.0625rem] font-bold text-ink"><Count value={value} fmt={fmt} /></b>
          {typical ? <span className="text-ink-3"> / {fmt(typical)} usual by now</span> : <span className="text-ink-3"> · still learning usual</span>}</span>
      </div>
      <div className="pr-pace mt-1.5">
        <span className="ai-grow-x" style={{ width: `${(value / max) * 100}%`, background: ratio == null ? "var(--cat-1)" : ratio < 0.6 ? "var(--warning)" : "var(--good)" }} />
        {typical ? <i style={{ left: `${(typical / max) * 100}%` }} title="usual by now" /> : null}
      </div>
      {ratio != null && <p className="mt-1 text-[0.6875rem] text-ink-3">{pc(ratio)} of usual so far</p>}
    </div>
  );
}

function StateSplit({ states }: { states: Record<State, number> }) {
  const parts = (Object.keys(STATE) as State[]).filter((k) => states[k]);
  return <span className="flex flex-wrap gap-x-2">{parts.map((k) => <span key={k} className="inline-flex items-center gap-1"><span className="size-1.5 rounded-full" style={{ background: STATE[k].color }} />{states[k]} {STATE[k].label.toLowerCase()}</span>)}</span>;
}

function Delta({ now, before, label, inline }: { now: number | null; before: number | null; label: string; inline?: boolean }) {
  if (now == null || before == null) return <>{inline ? null : "nothing to compare yet"}</>;
  const d = (now - before) * 100;
  const up = d >= 0;
  const I = Math.abs(d) < 0.5 ? null : up ? ArrowUpRight : ArrowDownRight;
  return (
    <span className={cn("inline-flex items-center gap-0.5", Math.abs(d) < 0.5 ? "text-ink-3" : up ? "text-[var(--good)]" : "text-[var(--critical)]")}>
      {I && <I className="size-3.5" aria-hidden />}{Math.abs(d) < 0.5 ? "same as" : `${Math.abs(d).toFixed(1)} pts ${up ? "up" : "down"} on`} {label}
    </span>
  );
}

// ── insights ─────────────────────────────────────────────────────────────────
function Insights({ items }: { items: Insight[] }) {
  return (
    <section className="mb-5">
      <h2 className="mb-2.5 flex items-center gap-2 text-[0.8125rem] font-semibold uppercase tracking-[0.08em] text-ink-3"><Sparkles className="size-3.5 text-accent" />What to know</h2>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {items.map((it, i) => {
          const t = TONE[it.tone];
          const href = linkOf(it.link);
          const body = (
            <>
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg" style={{ background: t.soft, color: t.color }}><t.icon className="size-4" aria-hidden /></span>
              <span className="min-w-0 flex-1">
                <span className="block text-[0.875rem] font-semibold leading-5 text-ink">{it.title}</span>
                <span className="mt-0.5 line-clamp-3 block text-[0.75rem] leading-[1.125rem] text-ink-2" title={it.detail}>{it.detail}</span>
              </span>
              {href && <ArrowRight className="pr-go size-4 shrink-0 text-ink-3" aria-hidden />}
            </>
          );
          const cls = "pr-insight card-in flex items-start gap-3 rounded-2xl p-3.5";
          const style = { "--tone": t.color, animationDelay: `${i * 50}ms` } as React.CSSProperties;
          return href?.startsWith("#") ? <a key={i} href={href} className={cls} style={style}>{body}</a>
            : href ? <Link key={i} to={href} className={cls} style={style}>{body}</Link>
              : <div key={i} className={cls} style={style}>{body}</div>;
        })}
      </div>
    </section>
  );
}

// ── today so far: one tile per pipeline and per table, like the report ───────
function TodaySoFar({ data, colorOf }: { data: Report; colorOf: (k: string) => string }) {
  return (
    <Section id="today" icon={Timer} title="Today so far" hint="Each pipeline's successful runs today against what it usually has by this time of day, and each table's rows against its usual.">
      <div className="grid gap-3 p-5 sm:grid-cols-2 sm:p-6 lg:grid-cols-3 2xl:grid-cols-4">
        {data.pipelines.map((p) => {
          const pace = p.today.pace;
          const St = STATE[p.state];
          return (
            <Link key={p.key} to={`/p/${p.project}/runs?pipeline=${encodeURIComponent(p.name)}`} className="pr-tile">
              <span className="pr-swatch" style={{ background: colorOf(p.key) }} aria-hidden />
              <span className="flex items-start justify-between gap-2">
                <span className="min-w-0">
                  <span className="block truncate text-[0.875rem] font-semibold text-ink" title={p.name}>{p.name}</span>
                  <span className="block truncate text-[0.6875rem] text-ink-3">{p.project_name}</span>
                </span>
                <span className="pr-chip" style={{ color: St.color }}><St.icon className={cn("size-3", p.state === "running" && "animate-spin")} aria-hidden />{St.label}</span>
              </span>
              <span className="mt-2 flex items-baseline gap-1.5">
                <b className="display text-[1.5rem] font-bold leading-none tabular-nums text-ink">{p.today.ok}</b>
                <span className="text-[0.75rem] text-ink-2">run{p.today.ok === 1 ? "" : "s"}{p.today.failed ? <span className="text-[var(--critical)]"> · {p.today.failed} failed</span> : null}</span>
              </span>
              <span className="pr-pace mt-2">
                <span className="ai-grow-x" style={{ width: `${Math.min(100, (pace ?? 0) * 100 / 1.3)}%`, background: pace == null ? "var(--surface-3)" : pace < 0.6 ? "var(--warning)" : colorOf(p.key) }} />
                {p.today.typical_by_now ? <i style={{ left: `${100 / 1.3}%` }} /> : null}
              </span>
              <span className="mt-1.5 flex justify-between gap-2 text-[0.6875rem] text-ink-3">
                <span>{pace == null ? "learning what's usual" : `${pc(pace)} of typical so far`}</span>
                <span>{p.cadence.overdue ? <b className="font-semibold text-[var(--critical)]">overdue</b> : p.cadence.next_at ? `next ~${time(p.cadence.next_at)}` : p.last.at ? `last ${ago(p.last.at)}` : ""}</span>
              </span>
            </Link>
          );
        })}
        {data.volumes.map((v) => {
          const d = v.pct_vs_typical;
          const up = (d ?? 0) >= 0;
          return (
            <Link key={v.key} to={`/p/${v.project}/tables`} className="pr-tile">
              <span className="pr-swatch" style={{ background: colorOf(`${v.project}/${v.pipeline}`) }} aria-hidden />
              <span className="flex items-start justify-between gap-2">
                <span className="min-w-0">
                  <span className="block truncate text-[0.875rem] font-semibold text-ink" title={v.table}>{v.short}</span>
                  <span className="block truncate text-[0.6875rem] text-ink-3">rows · {v.pipeline}</span>
                </span>
                <Database className="size-3.5 shrink-0 text-ink-3" aria-hidden />
              </span>
              <span className="mt-2 flex items-baseline gap-1.5">
                <b className="display text-[1.5rem] font-bold leading-none tabular-nums text-ink">{n(v.today)}</b>
                <span className="text-[0.75rem] text-ink-2">rows today</span>
              </span>
              <span className="mt-2 flex items-center gap-1 text-[0.75rem] font-semibold"
                style={{ color: d == null ? "var(--ink-3)" : v.verdict === "none" || v.verdict === "low" ? "var(--critical)" : v.verdict === "high" ? "var(--warning)" : "var(--ink-2)" }}>
                {d == null ? "learning what's usual" : <>{up ? <ArrowUpRight className="size-3.5" /> : <ArrowDownRight className="size-3.5" />}{Math.abs(d * 100).toFixed(0)}% vs usual by now</>}
              </span>
              <span className="mt-1 text-[0.6875rem] text-ink-3">{v.avg_7d != null ? `${n(v.avg_7d)} a day on average` : ""}{v.last_load.at ? ` · last load ${ago(v.last_load.at)}` : ""}</span>
            </Link>
          );
        })}
      </div>
    </Section>
  );
}

// ── per-day tables ───────────────────────────────────────────────────────────
function DayHead({ calendar, today }: { calendar: string[]; today: string }) {
  return (
    <>
      {calendar.map((d) => (
        <th key={d} className={cn("pr-dayh", d === today && "is-today")}>
          <span>{d === today ? "Today" : wd.format(dayOf(d))}</span><b>{dayOf(d).getDate()}</b>
        </th>
      ))}
    </>
  );
}

type Show = (e: React.MouseEvent, body: ReactNode) => void;

function ExecutionTable({ data, colorOf, show, hide }: { data: Report; colorOf: (k: string) => string; show: Show; hide: () => void }) {
  return (
    <div className="scrollbar-thin overflow-x-auto">
      <table className="pr-table w-full text-[0.8125rem]" style={{ minWidth: `${30 + data.calendar.length * 3.4}rem` }}>
        <thead><tr><th className="pl-5 text-left sm:pl-6">Pipeline · last run</th><DayHead calendar={data.calendar} today={data.today} /><th className="text-right">Success</th><th className="pr-5 text-right sm:pr-6">Runtime</th></tr></thead>
        <tbody>
          {data.pipelines.map((p) => {
            const max = Math.max(1, ...p.daily.map((d) => d.ok + d.failed));
            const c = colorOf(p.key);
            const St = STATE[p.state];
            const rt = p.runtime;
            return (
              <tr key={p.key} className="pr-row">
                <td className="pl-5 sm:pl-6">
                  <Link to={`/p/${p.project}/runs?pipeline=${encodeURIComponent(p.name)}`} className="group block min-w-0 max-w-[19rem]">
                    <span className="flex items-center gap-2">
                      <span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: c }} aria-hidden />
                      <span className="truncate font-semibold text-ink group-hover:underline" title={p.name}>{p.name}</span>
                      <St.icon className={cn("size-3.5 shrink-0", p.state === "running" && "animate-spin")} style={{ color: St.color }} aria-label={St.label} />
                    </span>
                    <span className="mt-0.5 block truncate text-[0.71875rem] text-ink-3">
                      {p.last.at ? `${new Date(p.last.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} · ` : ""}
                      <b className={cn("font-semibold", p.last.status === "Succeeded" ? "text-ink-2" : p.last.status === "Failed" ? "text-[var(--critical)]" : "text-ink-2")}>{p.last.status.toUpperCase()}</b>
                      {p.last.duration_s != null ? ` · ${duration(p.last.duration_s)}` : ""}
                    </span>
                    <span className="block truncate text-[0.6875rem] text-ink-3">
                      {p.cadence.interval_s ? `every ${every(p.cadence.interval_s)}` : p.trigger ?? ""}
                      {p.cadence.overdue ? <b className="font-semibold text-[var(--critical)]"> · overdue by {every(p.cadence.since_last_s - (p.cadence.interval_s ?? 0))}</b>
                        : p.cadence.next_at ? ` · next ~${time(p.cadence.next_at)}` : ""}
                    </span>
                  </Link>
                </td>
                {p.daily.map((d) => (
                  <td key={d.date} className={cn("pr-cell", d.date === data.today && "is-today")}
                    onMouseMove={(e) => show(e, <DayTip title={p.name} date={d.date} today={data.today} lines={[["Succeeded", d.ok, c], ["Failed", d.failed, "var(--critical)"], ...(d.other ? [["Other", d.other, "var(--ink-3)"] as [string, number, string]] : []), ...(d.rows ? [["Rows loaded", d.rows, "transparent"] as [string, number, string]] : [])]} />)}
                    onMouseLeave={hide}>
                    <Bar ok={d.ok} failed={d.failed} max={max} color={c} />
                  </td>
                ))}
                <td className="text-right">
                  <span className={cn("font-semibold tabular-nums", p.success_rate == null ? "text-ink-3" : p.success_rate < 0.9 ? "text-[var(--critical)]" : "text-ink")}>{pc(p.success_rate)}</span>
                  <span className="block text-[0.6875rem] text-ink-3">{p.ok}/{p.ok + p.failed}{p.success_rate_prev != null && p.success_rate != null && Math.abs(p.success_rate - p.success_rate_prev) >= 0.005
                    ? <span className={p.success_rate > p.success_rate_prev ? "text-[var(--good)]" : "text-[var(--critical)]"}> {p.success_rate > p.success_rate_prev ? "▲" : "▼"}</span> : null}</span>
                </td>
                <td className="pr-5 text-right sm:pr-6">
                  <span className="font-semibold tabular-nums text-ink">{duration(p.duration.p50)}</span>
                  <span className="block text-[0.6875rem] text-ink-3">
                    {rt?.verdict && rt.pct != null ? <span className={cn(rt.verdict === "much_slower" || rt.verdict === "slower" ? "text-[var(--warning)]" : rt.verdict === "faster" ? "text-[var(--good)]" : "")}>last {rt.pct > 0 ? "+" : ""}{rt.pct}%</span>
                      : p.duration.p95 != null ? `p95 ${duration(p.duration.p95)}` : "typical"}
                  </span>
                  {p.snowflake?.per_run != null && <Link to="/compute" className="mt-0.5 block text-[0.6875rem] text-accent hover:underline" title="Snowflake compute per run (last 30 days)">❄ ${p.snowflake.per_run.toFixed(2)} a run</Link>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Bar({ ok, failed, max, color }: { ok: number; failed: number; max: number; color: string }) {
  const h = 38;
  const okH = (ok / max) * h, fH = (failed / max) * h;
  const total = ok + failed;
  return (
    <div className="pr-bar" aria-label={`${ok} succeeded, ${failed} failed`}>
      <span className="pr-num">{total ? n(total) : "0"}</span>
      <span className="pr-stack" style={{ height: h }}>
        {failed > 0 && <span className="ai-grow" style={{ height: Math.max(3, fH), background: "var(--critical)" }} />}
        {ok > 0 && <span className="ai-grow" style={{ height: Math.max(3, okH), background: color }} />}
        {!total && <span className="pr-zero" />}
      </span>
    </div>
  );
}

function DayTip({ title, date, today, lines }: { title: string; date: string; today: string; lines: [string, number, string][] }) {
  return (
    <>
      <p className="font-semibold text-ink">{title}</p>
      <p className="mb-1 text-ink-3">{date === today ? "Today, so far" : `${wd.format(dayOf(date))} ${dm.format(dayOf(date))}`}</p>
      {lines.map(([l, v, c]) => (
        <p key={l} className="flex items-center justify-between gap-4 tabular-nums">
          <span className="flex items-center gap-1.5 text-ink-2"><span className="size-2 rounded-[2px]" style={{ background: c }} />{l}</span><b className="text-ink">{nf.format(Math.round(v))}</b>
        </p>
      ))}
    </>
  );
}

function VolumeTable({ data, colorOf, show, hide }: { data: Report; colorOf: (k: string) => string; show: Show; hide: () => void }) {
  return (
    <div className="scrollbar-thin overflow-x-auto">
      <table className="pr-table w-full text-[0.8125rem]" style={{ minWidth: `${28 + data.calendar.length * 3.4}rem` }}>
        <thead><tr><th className="pl-5 text-left sm:pl-6">Table · loaded by</th><DayHead calendar={data.calendar} today={data.today} /><th className="pr-5 text-right sm:pr-6">Today vs usual</th></tr></thead>
        <tbody>
          {data.volumes.map((v) => {
            const max = Math.max(1, ...v.daily.map((d) => d.rows));
            const c = colorOf(`${v.project}/${v.pipeline}`);
            const d = v.pct_vs_typical;
            return (
              <tr key={v.key} className="pr-row">
                <td className="pl-5 sm:pl-6">
                  <Link to={`/p/${v.project}/tables`} className="group block min-w-0 max-w-[19rem]">
                    <span className="flex items-center gap-2"><span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: c }} aria-hidden />
                      <span className="truncate font-semibold text-ink group-hover:underline" title={v.table}>{v.short}</span></span>
                    <span className="mt-0.5 block truncate text-[0.71875rem] text-ink-3" title={v.table}>{v.pipeline} · {v.table.split(".").slice(0, -1).join(".") || "table"}</span>
                    <span className="block truncate text-[0.6875rem] text-ink-3">{v.last_load.at ? `last load ${ago(v.last_load.at)} · ${n(v.last_load.rows)} rows` : ""}</span>
                  </Link>
                </td>
                {v.daily.map((x) => (
                  <td key={x.date} className={cn("pr-cell", x.date === data.today && "is-today")}
                    onMouseMove={(e) => show(e, <DayTip title={v.short} date={x.date} today={data.today} lines={[["Rows", x.rows, c]]} />)} onMouseLeave={hide}>
                    <Bar ok={x.rows} failed={0} max={max} color={c} />
                  </td>
                ))}
                <td className="pr-5 text-right sm:pr-6">
                  <span className={cn("font-semibold tabular-nums", v.verdict === "none" || v.verdict === "low" ? "text-[var(--critical)]" : v.verdict === "high" ? "text-[var(--warning)]" : "text-ink")}>
                    {d == null ? "–" : `${d >= 0 ? "+" : "−"}${Math.abs(d * 100).toFixed(0)}%`}
                  </span>
                  <span className="block text-[0.6875rem] text-ink-3">{v.typical_by_now != null ? `${n(v.today)} / ${n(v.typical_by_now)} by now` : "learning"}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── errors ───────────────────────────────────────────────────────────────────
function Errors({ data, colorOf, show, hide }: { data: Report; colorOf: (k: string) => string; show: Show; hide: () => void }) {
  const e = data.errors;
  const total = e.daily.reduce((a, d) => a + d.total, 0);
  const max = Math.max(1, ...e.daily.map((d) => d.total));
  const names = Object.fromEntries(data.pipelines.map((p) => [p.key, p.name]));
  if (!total) {
    return <div className="flex items-center gap-3 px-5 py-8 text-[0.875rem] text-ink-2 sm:px-6"><CheckCircle2 className="size-5 text-[var(--good)]" />No failed runs in the last {data.days} days.</div>;
  }
  return (
    <div className="grid gap-0 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
      <div className="border-line p-5 sm:p-6 lg:border-r">
        <p className="text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">Failed runs per day</p>
        <p className="mt-1 text-[0.8125rem] text-ink-2"><b className="text-[1.25rem] font-bold text-ink">{total}</b> in {data.days} days</p>
        <div className="pr-errchart mt-4" style={{ gridTemplateColumns: `repeat(${e.daily.length}, minmax(0, 1fr))` }}>
          {e.daily.map((d) => (
            <div key={d.date} className="flex flex-col items-center gap-1"
              onMouseMove={(ev) => show(ev, <DayTip title="Failed runs" date={d.date} today={data.today}
                lines={Object.entries(d.by).map(([k, v]) => [names[k] ?? k.split("/").pop()!, v, colorOf(k)] as [string, number, string])} />)}
              onMouseLeave={hide}>
              <span className="text-[0.6875rem] font-semibold tabular-nums text-ink-2">{d.total || ""}</span>
              <span className="flex h-28 w-full max-w-[2.25rem] flex-col-reverse gap-[2px]">
                {Object.entries(d.by).sort().map(([k, v]) => <span key={k} className="ai-grow rounded-[3px]" style={{ height: `${(v / max) * 100}%`, background: colorOf(k), minHeight: 3 }} />)}
                {!d.total && <span className="pr-zero mx-auto" />}
              </span>
              <span className={cn("text-[0.6875rem] text-ink-3", d.date === data.today && "font-semibold text-ink")}>{d.date === data.today ? "Today" : dayOf(d.date).getDate()}</span>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[0.6875rem] text-ink-2">
          {[...new Set(e.daily.flatMap((d) => Object.keys(d.by)))].sort().map((k) => (
            <span key={k} className="inline-flex items-center gap-1.5"><span className="size-2 rounded-[2px]" style={{ background: colorOf(k) }} />{names[k] ?? k.split("/").pop()}</span>
          ))}
        </div>
        {e.recent.length > 0 && (
          <>
            <p className="mt-6 text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">Latest failures</p>
            <ul className="mt-2 space-y-1">
              {e.recent.slice(0, 6).map((r) => (
                <li key={r.run_id}>
                  <Link to={r.incident_id ? `/p/${r.project}/i/${r.incident_id}` : `/p/${r.project}/runs?pipeline=${encodeURIComponent(r.pipeline)}&run=${encodeURIComponent(r.run_id)}`}
                    className="pr-recent">
                    <span className="size-2 shrink-0 rounded-[2px]" style={{ background: colorOf(`${r.project}/${r.pipeline}`) }} />
                    <span className="min-w-0 flex-1"><b className="font-semibold text-ink">{r.pipeline}</b> <span className="text-ink-3">· {ago(r.at)}</span>
                      <span className="block truncate text-[0.71875rem] text-ink-3" title={r.error}>{r.error || r.status}</span></span>
                    <ArrowRight className="size-3.5 shrink-0 text-ink-3" />
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
      <div className="p-5 sm:p-6">
        <p className="text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">What went wrong, most often first</p>
        <ul className="mt-3 space-y-2.5">
          {e.signatures.map((g) => {
            const share = g.count / total;
            const href = g.incident ? `/p/${g.incident.project}/i/${g.incident.id}`
              : g.last_run ? `/p/${g.last_run.project}/runs?pipeline=${encodeURIComponent(g.last_run.pipeline)}&run=${encodeURIComponent(g.last_run.run_id)}` : null;
            const inner = (
              <>
                <span className="flex items-start gap-3">
                  <span className="pr-count">{g.count}×</span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-1.5">
                      <b className="text-[0.875rem] font-semibold text-ink">{g.title ?? "Unrecognised error"}</b>
                      {g.known && <span className="ai-chip good"><Wrench className="mr-0.5 inline size-2.5" />known fix</span>}
                      {g.incident && <span className="ai-chip">{g.incident.status}</span>}
                    </span>
                    {g.steps.length > 0 && <span className="mt-0.5 block truncate text-[0.75rem] text-ink-2">in step{g.steps.length === 1 ? "" : "s"} {g.steps.slice(0, 3).join(", ")}{g.steps.length > 3 ? "…" : ""}</span>}
                    <span className="mt-0.5 line-clamp-2 block font-mono text-[0.6875rem] leading-4 text-ink-3" title={g.sample}>{g.sample}</span>
                    <span className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[0.6875rem] text-ink-2">
                      {g.pipelines.slice(0, 4).map((p) => <span key={p.project + p.name} className="inline-flex items-center gap-1"><span className="size-2 rounded-[2px]" style={{ background: colorOf(`${p.project}/${p.name}`) }} />{p.name}</span>)}
                      <span className="text-ink-3">· last {ago(g.last_at)}</span>
                    </span>
                  </span>
                </span>
                <span className="pr-share mt-2"><span className="ai-grow-x" style={{ width: `${share * 100}%` }} /></span>
              </>
            );
            return <li key={g.key}>{href ? <Link to={href} className="pr-sig">{inner}</Link> : <div className="pr-sig">{inner}</div>}</li>;
          })}
        </ul>
        {data.open_incidents.length > 0 && (
          <>
            <p className="mt-6 text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">Open incidents</p>
            <ul className="mt-2 space-y-1">
              {data.open_incidents.map((i) => (
                <li key={i.id}><Link to={`/p/${i.project}/i/${i.id}`} className="pr-recent">
                  <Siren className={cn("size-3.5 shrink-0", i.status === "acknowledged" ? "text-accent" : "text-[var(--critical)]")} />
                  <span className="min-w-0 flex-1 truncate text-ink">{i.title}</span>
                  <span className="shrink-0 text-[0.6875rem] text-ink-3">{i.status === "acknowledged" ? "being handled · " : ""}{ago(i.opened_at)}</span>
                </Link></li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

// ── rhythm ───────────────────────────────────────────────────────────────────
function Heatmap({ heat, show, hide }: { heat: Report["heatmap"]; show: Show; hide: () => void }) {
  // stretch the scale between the quietest and busiest hour that had runs, so a steady schedule still shows its peaks
  const busy = heat.flat().map((c) => c.runs).filter((v) => v > 0);
  const lo = busy.length ? Math.min(...busy) : 0, hi = busy.length ? Math.max(...busy) : 1;
  const shade = (v: number) => {
    const spread = hi === lo ? 0.72 : (v - lo) / (hi - lo);
    const intensity = Math.round(18 + Math.pow(Math.max(0, spread), 0.72) * 76);
    return `color-mix(in srgb, var(--cat-1) ${intensity}%, var(--surface-2))`;
  };
  const busiest = heat.flatMap((row, d) => row.map((c, h) => ({ d, h, ...c }))).sort((a, b) => b.runs - a.runs)[0];
  const failHours = heat.flatMap((row, d) => row.map((c, h) => ({ d, h, ...c }))).filter((c) => c.failed).sort((a, b) => b.failed - a.failed);
  const hh = (h: number) => `${String(h).padStart(2, "0")}:00`;
  return (
    <div className="p-5 sm:p-6">
      <div className="pr-heat-frame">
        <div className="pr-heat-caption"><span>RUN ACTIVITY</span><span>LOCAL TIME · 24 HOUR</span></div>
        <div className="scrollbar-thin overflow-x-auto">
        <div className="pr-heat" role="img" aria-label="Runs by weekday and hour">
          <span className="pr-heat-axis">DAY</span>
          {Array.from({ length: 24 }, (_, h) => <span key={h} className="pr-hh">{h % 6 === 0 ? String(h).padStart(2, "0") : ""}</span>)}
          {heat.map((row, d) => (
            <div key={d} className="contents">
              <span className="pr-hd">{WEEK[d]}</span>
              {row.map((c, h) => (
                <span key={h} className={cn("pr-hc", h % 6 === 0 && "is-hour-mark", c.failed > 0 && "has-failures")}
                  style={{ background: c.runs ? shade(c.runs) : undefined }}
                  onMouseMove={(e) => show(e, <><p className="font-semibold text-ink">{WEEK[d]} {hh(h)}–{hh((h + 1) % 24)}</p><p className="text-ink-2">{c.runs} run{c.runs === 1 ? "" : "s"}{c.failed ? <b className="text-[var(--critical)]"> · {c.failed} failed</b> : ""}</p></>)}
                  onMouseLeave={hide}>
                  {c.failed > 0 && <i />}
                </span>
              ))}
            </div>
          ))}
        </div>
      </div>
      </div>
      <div className="pr-heat-footer mt-4 flex flex-wrap gap-x-6 gap-y-2 text-[0.8125rem] text-ink-2">
        {busiest && busiest.runs > 0 && <span className="inline-flex items-center gap-1.5"><TrendingUp className="size-4 text-accent" />Busiest: <b className="text-ink">{WEEK[busiest.d]} {hh(busiest.h)}</b> ({busiest.runs} runs)</span>}
        {failHours[0] && <span className="inline-flex items-center gap-1.5"><Flame className="size-4 text-[var(--critical)]" />Most failures: <b className="text-ink">{WEEK[failHours[0].d]} {hh(failHours[0].h)}</b> ({failHours[0].failed})</span>}
        <span className="inline-flex items-center gap-2 text-[0.75rem] text-ink-3">fewer
          <span className="flex gap-[2px]">{[0, 0.33, 0.66, 1].map((k) => <span key={k} className="size-3 rounded-[3px]" style={{ background: shade(lo + k * (hi - lo)) }} />)}</span>more
          <span className="tabular-nums">({lo}–{hi} runs an hour)</span>
          <span className="ml-2 inline-flex items-center gap-1"><span className="size-1.5 rounded-full bg-[var(--critical)]" />failed</span></span>
      </div>
    </div>
  );
}

// ── shared ───────────────────────────────────────────────────────────────────
function Section({ id, icon: Icon, title, hint, children }: { id: string; icon: LucideIcon; title: string; hint: string; children: ReactNode }) {
  return (
    <section id={id} className="card-in mb-5 scroll-mt-24 overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]">
      <header className="flex items-start gap-3 border-b border-line px-5 py-4 sm:px-6">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><Icon className="size-[1.125rem]" aria-hidden /></span>
        <div className="min-w-0">
          <h2 className="display text-[1rem] font-semibold text-ink">{title}</h2>
          <p className="text-[0.75rem] leading-5 text-ink-3">{hint}</p>
        </div>
      </header>
      {children}
    </section>
  );
}
