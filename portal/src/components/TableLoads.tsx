// ADF projects: exactly how many rows went into each table. For the chosen period: rows inserted per table (and against
// the period before), per day, every load with its run, the learned normal, and the data-quality result. Sort, filter,
// search, export to CSV, and open any load's run.
import {
  ArrowDown, ArrowDownRight, ArrowUp, ArrowUpRight, ChevronRight, Database, Download, ExternalLink, FolderOpen, Minus, Search,
  ShieldAlert, ShieldCheck, TriangleAlert,
} from "lucide-react";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChartCard, Notice } from "@/components/Cards";
import { StatusChip } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Input, Segmented, Select, Skeleton } from "@/components/ui/primitives";
import { ago, compactNum, dateTime, duration, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface Load {
  at: string; run_id: string; run_status: string | null; rows_copied: number | null; rows_read: number | null; rows_skipped: number | null;
  duration_s?: number | null; trigger?: string | null;
}
export interface TableLoad {
  table: string; pipeline: string; kind: string; env: string | null; dataset: string | null; steps: string[];
  last: Load; normal: { median: number; low: number; high: number; samples: number } | null; loads: Load[];
  loads_count: number; zero_loads: number; skipped_total: number; status: string; incident: { id: number; title: string } | null;
  inserted?: number; inserted_prev?: number | null; read?: number; skipped?: number; daily?: { date: string; rows: number }[];
  quality?: { status: "ok" | "fail" | "learning"; score: number | null; failing: string[]; checked_at: string | null } | null;
}
interface Day { date: string; rows: number; loads: number; top: { table: string; rows: number }[] }
interface LoadsView {
  tables: TableLoad[]; days: number; from?: string; to?: string; timezone?: string; daily?: Day[];
  totals?: { inserted: number; inserted_prev: number | null; loads: number; tables: number; tables_loaded: number; skipped: number; needs_attention: number; empty_loads: number };
}

const ENV_STYLE: Record<string, string> = {
  PROD: "bg-[var(--critical-soft)] text-[var(--critical-ink)]",
  UAT: "bg-[var(--warning-soft)] text-[var(--warning-ink)]",
  DEV: "bg-[var(--accent-soft)] text-accent",
};

export function EnvBadge({ env }: { env: string | null }) {
  if (!env) return null;
  return <span className={cn("rounded px-1.5 py-px text-[0.625rem] font-bold tracking-wide", ENV_STYLE[env] ?? "bg-surface-3 text-ink-3")}>{env}</span>;
}

function shortName(table: string) {
  const parts = table.split(".");
  return { name: parts[parts.length - 1]!, where: parts.slice(0, -1).join(".") };
}

const fmtDay = (d: string, long = false) => new Date(`${d}T12:00:00`).toLocaleDateString(undefined, long ? { weekday: "short", day: "numeric", month: "short" } : { day: "numeric", month: "short" });

/** +12% / −40% against the previous period, coloured by direction (more data is not "bad"; less is worth a look). */
function Delta({ now, before }: { now: number; before: number | null | undefined }) {
  if (before == null) return <span className="text-[0.6875rem] text-ink-3">no earlier data</span>;
  if (before === 0) return now ? <span className="text-[0.6875rem] text-ink-3">new this period</span> : <span className="text-[0.6875rem] text-ink-3">none before either</span>;
  const pct = Math.round((now / before - 1) * 100);
  const Icon = pct > 0 ? ArrowUpRight : pct < 0 ? ArrowDownRight : Minus;
  return (
    <span className={cn("inline-flex items-center gap-0.5 text-[0.6875rem] font-semibold", pct <= -30 ? "text-[var(--critical-ink)]" : pct < 0 ? "text-[var(--warning-ink)]" : "text-ink-2")}
      title={`${num(before)} in the previous period`}>
      <Icon className="size-3" aria-hidden />{pct > 0 ? "+" : ""}{pct}% vs before
    </span>
  );
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(220, e!.contentRect.width)));
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

/** Rows inserted per day: one series, 4px rounded tops on the baseline, hover a day for its numbers. */
function DayBars({ days, height = 200, label }: { days: { date: string; rows: number; loads?: number; top?: { table: string; rows: number }[] }[]; height?: number; label: string }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const pad = { l: 44, r: 8, t: 8, b: 22 };
  const max = Math.max(1, ...days.map((d) => d.rows));
  const ys = ticks(max);
  const top = ys[ys.length - 1]!;
  const iw = w - pad.l - pad.r, ih = height - pad.t - pad.b;
  const slot = iw / Math.max(1, days.length), bw = Math.max(3, Math.min(28, slot - 4));
  const y = (v: number) => pad.t + ih - (v / top) * ih;
  const every = Math.ceil(days.length / 10);
  const h = hover != null ? days[hover] : null;
  return (
    <div ref={ref} className="relative" onMouseLeave={() => setHover(null)}>
      <svg width={w} height={height} role="img" aria-label={label}>
        {ys.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} style={{ stroke: "var(--line)" }} strokeDasharray={t ? "3 4" : undefined} />
            <text x={pad.l - 6} y={y(t) + 3.5} textAnchor="end" className="fill-[var(--ink-3)] text-[10px]">{compactNum(t)}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const x = pad.l + i * slot + (slot - bw) / 2;
          return (
            <g key={d.date} opacity={hover == null || hover === i ? 1 : 0.45}>
              {d.rows > 0
                ? <path d={bar(x, y(d.rows), bw, y(0) - y(d.rows))} style={{ fill: "var(--chart-ok)" }} />
                : <rect x={x} y={y(0) - 2} width={bw} height={2} rx={1} style={{ fill: "var(--line-strong)" }} />}
              <rect x={pad.l + i * slot} y={pad.t} width={slot} height={ih} fill="transparent" onMouseEnter={() => setHover(i)}
                onFocus={() => setHover(i)} tabIndex={-1} />
              {i % every === 0 && <text x={pad.l + i * slot + slot / 2} y={height - 6} textAnchor="middle" className="fill-[var(--ink-3)] text-[10px]">{fmtDay(d.date)}</text>}
            </g>
          );
        })}
      </svg>
      {h && (
        <div className="pointer-events-none absolute top-0 z-10 min-w-40 rounded-lg border border-line-strong bg-[var(--overlay-bg)] px-3 py-2 text-xs shadow-float backdrop-blur"
          style={{ left: Math.min(w - 200, Math.max(0, pad.l + hover! * slot - 60)) }}>
          <p className="font-semibold text-ink">{fmtDay(h.date, true)}</p>
          <p className="text-ink"><b>{num(h.rows)}</b> rows inserted{h.loads != null ? <span className="text-ink-3"> · {h.loads} load{h.loads === 1 ? "" : "s"}</span> : null}</p>
          {h.top && h.top.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-ink-2">{h.top.map((t) => <li key={t.table} className="flex justify-between gap-3"><span className="truncate">{shortName(t.table).name}</span><span className="num">{num(t.rows)}</span></li>)}</ul>
          )}
        </div>
      )}
    </div>
  );
}

/** A bar with 4px rounded top corners, square on the baseline. */
function bar(x: number, y: number, w: number, h: number) {
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** Tiny per-day bars inside a row. */
function MiniBars({ daily }: { daily: { date: string; rows: number }[] }) {
  const d = daily.slice(-30);
  const max = Math.max(1, ...d.map((x) => x.rows));
  return (
    <div className="flex h-7 w-28 items-end gap-px" aria-hidden title={`${d.filter((x) => x.rows).length} of ${d.length} days had rows`}>
      {d.map((x) => <span key={x.date} className="flex-1 rounded-t-[1px]" style={{ height: x.rows ? `${Math.max(8, (x.rows / max) * 100)}%` : "2px", background: x.rows ? "var(--chart-ok)" : "var(--line-strong)" }} />)}
    </div>
  );
}

function vsNormal(rows: number | null, n: TableLoad["normal"]) {
  if (rows == null || !n) return { text: "learning its normal", tone: "text-ink-3", out: false };
  if (rows === 0 && n.median > 0) return { text: "no rows", tone: "text-[var(--critical-ink)]", out: true };
  const pct = n.median ? Math.round((rows / n.median - 1) * 100) : 0;
  const out = rows < n.low || rows > n.high;
  return { text: `${pct >= 0 ? "+" : ""}${pct}% vs usual ${compactNum(n.median)}`, tone: out ? (rows < n.low ? "text-[var(--critical-ink)]" : "text-[var(--warning-ink)]") : "text-ink-3", out };
}

type SortKey = "name" | "inserted" | "loads" | "last" | "change";
type Show = "all" | "attention" | "loaded" | "none";
const PERIODS = [{ value: 1, label: "Today" }, { value: 7, label: "7 days" }, { value: 30, label: "30 days" }, { value: 90, label: "90 days" }] as const;

function needsLook(t: TableLoad) {
  return t.status !== "ok" || t.quality?.status === "fail";
}

function toCsv(rows: TableLoad[], from?: string, to?: string) {
  const head = ["table", "environment", "pipeline", "copy steps", "rows inserted", "rows inserted (previous period)", "loads", "rows read", "rows skipped",
    "latest load at", "latest load rows", "latest run status", "usual rows per load", "status", "data quality", "period from", "period to"];
  const esc = (v: unknown) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const lines = rows.map((t) => [t.table, t.env, t.pipeline, t.steps.join(" | "), t.inserted ?? "", t.inserted_prev ?? "", t.loads_count, t.read ?? "", t.skipped ?? t.skipped_total,
    t.last.at, t.last.rows_copied, t.last.run_status, t.normal ? Math.round(t.normal.median) : "", t.status, t.quality?.status ?? "", from, to].map(esc).join(","));
  return [head.join(","), ...lines].join("\n");
}

export function TableLoads({ projectKey, sample }: { projectKey: string; sample?: LoadsView }) {
  const [days, setDays] = useState<1 | 7 | 30 | 90>(7);
  const api = useApi<LoadsView>(sample ? null : `/api/projects/${projectKey}/loads?days=${days}`, { refreshMs: 60000 });
  const { error, reload } = api;
  const data = sample ?? api.data;
  const [env, setEnv] = useState<"all" | "PROD" | "UAT" | "DEV">("all");
  const [pipeline, setPipeline] = useState("");
  const [q, setQ] = useState("");
  const [show, setShow] = useState<Show>("all");
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "inserted", dir: -1 });
  const [open, setOpen] = useState<string | null>(null);
  const [params] = useSearchParams();
  const want = params.get("table");

  // opened from search: show that table, expanded
  useEffect(() => {
    if (!want || !data) return;
    const t = data.tables.find((x) => x.table === want && (!params.get("pipeline") || x.pipeline === params.get("pipeline"))) ?? data.tables.find((x) => x.table === want);
    if (!t) return;
    setOpen(`${t.pipeline}|${t.table}`);
    window.setTimeout(() => document.getElementById(`tbl-${t.pipeline}|${t.table}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [want, data]);

  const all = data?.tables ?? [];
  const envs = useMemo(() => (["PROD", "UAT", "DEV"] as const).filter((e) => all.some((t) => t.env === e)), [all]);
  const pipelines = useMemo(() => [...new Set(all.map((t) => t.pipeline))].sort(), [all]);
  const tables = useMemo(() => {
    const s = q.trim().toLowerCase();
    const rows = all.filter((t) => (env === "all" || t.env === env) && (!pipeline || t.pipeline === pipeline)
      && (show === "all" || (show === "attention" && needsLook(t)) || (show === "loaded" && t.loads_count > 0) || (show === "none" && t.loads_count === 0))
      && (!s || `${t.table} ${t.pipeline} ${t.steps.join(" ")} ${t.dataset ?? ""}`.toLowerCase().includes(s)));
    const val = (t: TableLoad): number | string => ({
      name: shortName(t.table).name.toLowerCase(), inserted: t.inserted ?? 0, loads: t.loads_count, last: t.last.at,
      change: t.inserted_prev ? (t.inserted ?? 0) / t.inserted_prev - 1 : Number.NEGATIVE_INFINITY,
    })[sort.key];
    return [...rows].sort((a, b) => (val(a) < val(b) ? -1 : val(a) > val(b) ? 1 : 0) * sort.dir);
  }, [all, env, pipeline, show, q, sort]);
  const maxInserted = Math.max(1, ...tables.map((t) => t.inserted ?? 0));
  const T = data?.totals;
  const range = data?.from && data?.to ? (data.from === data.to ? fmtDay(data.to, true) : `${fmtDay(data.from)} – ${fmtDay(data.to)}`) : "";

  const exportCsv = () => {
    const blob = new Blob([toCsv(tables, data?.from, data?.to)], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `table-loads_${projectKey}_${data?.from ?? ""}_${data?.to ?? ""}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  const sortBy = (key: SortKey) => setSort((s) => (s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: key === "name" ? 1 : -1 }));
  const Th = ({ k, children, right }: { k: SortKey; children: React.ReactNode; right?: boolean }) => (
    <th className={right ? "text-right" : undefined} aria-sort={sort.key === k ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => sortBy(k)} className={cn("inline-flex items-center gap-1 [letter-spacing:inherit] [text-transform:inherit] hover:text-ink", sort.key === k && "text-ink")}>
        {children}{sort.key === k ? (sort.dir === 1 ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />) : null}
      </button>
    </th>
  );

  return (
    <div>
      {/* period */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Segmented label="Period" value={days} onChange={(v) => { setDays(v); setOpen(null); }} options={PERIODS.map((p) => ({ value: p.value, label: p.label }))} />
        {range && <p className="text-xs text-ink-3">Showing <b className="text-ink-2">{range}</b>{data?.timezone ? ` · days in ${data.timezone}` : ""} · compared with the {days === 1 ? "day" : `${days} days`} before</p>}
      </div>

      {/* totals */}
      {T ? (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          <div className="col-span-2 rounded-2xl bg-surface p-4 shadow-[inset_0_0_0_1px_var(--line)] md:col-span-1 light:bg-white">
            <p className="text-xs text-ink-3">Rows inserted</p>
            <p className="display mt-1 text-[1.75rem] font-bold leading-none tracking-[-0.02em] text-ink">{num(T.inserted)}</p>
            <p className="mt-2"><Delta now={T.inserted} before={T.inserted_prev} /></p>
          </div>
          {[
            ["Tables that got data", <>{T.tables_loaded}<span className="text-base text-ink-3"> / {T.tables}</span></>, T.tables - T.tables_loaded ? `${T.tables - T.tables_loaded} got nothing in this period` : "every table got data"],
            ["Loads", num(T.loads), T.empty_loads ? `${T.empty_loads} loaded 0 rows` : "none were empty"],
            ["Need a look", num(T.needs_attention), T.needs_attention ? "unusual volume, failing checks or an incident" : "all as expected"],
            ["Rows skipped", num(T.skipped), T.skipped ? "rows the copy couldn't load" : "nothing skipped"],
          ].map(([k, v, sub]) => (
            <div key={String(k)} className="rounded-2xl bg-surface p-4 shadow-[inset_0_0_0_1px_var(--line)] light:bg-white">
              <p className="text-xs text-ink-3">{k}</p>
              <p className="display mt-1 text-[1.75rem] font-bold leading-none tracking-[-0.02em] text-ink">{v}</p>
              <p className="mt-2 text-[0.6875rem] text-ink-3">{sub}</p>
            </div>
          ))}
        </div>
      ) : !error ? <Skeleton className="mb-4 h-28 w-full" /> : null}

      {data?.daily && data.daily.length > 1 && (
        <ChartCard className="mb-4" icon={Database} title="Rows inserted per day" subtitle={`All tables · ${range} · hover a day to see which tables got the most`}>
          <DayBars days={data.daily} label={`Rows inserted per day, ${range}`} />
        </ChartCard>
      )}

      <ChartCard flush icon={Database} title="Rows inserted per table"
        subtitle={data ? `${tables.length} of ${all.length} tables · click a table for every load` : undefined}
        actions={data && tables.length ? <Button size="sm" variant="secondary" onClick={exportCsv}><Download />Export CSV</Button> : null}
        error={error} onRetry={reload} loading={!data && !error}
        empty={!!data && !all.length} emptyText="No table loads recorded yet. They appear after the pipelines' copy steps run."
        skeleton={<div className="space-y-2 px-6">{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>}
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search tables, pipelines, steps" aria-label="Search tables" className="w-64 pl-8" />
            </div>
            <Select value={show} onChange={(e) => setShow(e.target.value as Show)} aria-label="Show" className="w-48">
              <option value="all">All tables</option>
              <option value="attention">Need a look</option>
              <option value="loaded">Got data in this period</option>
              <option value="none">Got nothing in this period</option>
            </Select>
            {pipelines.length > 1 && (
              <Select value={pipeline} onChange={(e) => setPipeline(e.target.value)} aria-label="Pipeline" className="w-56">
                <option value="">All pipelines</option>
                {pipelines.map((p) => <option key={p}>{p}</option>)}
              </Select>
            )}
            {envs.length > 1 && (
              <div className="ml-auto">
                <Segmented size="sm" label="Environment" value={env} onChange={setEnv}
                  options={[{ value: "all", label: "All" }, ...envs.map((e) => ({ value: e, label: e }))]} />
              </div>
            )}
          </div>
        }>
        {tables.length === 0 ? (
          <p className="px-6 py-6 text-sm text-ink-3">No table matches these filters. <button className="font-semibold text-accent hover:underline" onClick={() => { setQ(""); setShow("all"); setPipeline(""); setEnv("all"); }}>Clear filters</button></p>
        ) : (
          <div className="scrollbar-thin overflow-auto">
            <table className="data-table min-w-[66rem]">
              <thead>
                <tr>
                  <Th k="name">Table</Th>
                  <Th k="inserted">Rows inserted</Th>
                  <Th k="loads" right>Loads</Th>
                  <Th k="last">Latest load</Th>
                  <th>Per day</th>
                  <Th k="change">Change</Th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {tables.map((t) => {
                  const id = `${t.pipeline}|${t.table}`;
                  const isOpen = open === id;
                  const { name, where } = shortName(t.table);
                  const v = vsNormal(t.last.rows_copied, t.normal);
                  const ins = t.inserted ?? 0;
                  return (
                    <Fragment key={id}>
                      <tr id={`tbl-${id}`} className={cn("cursor-pointer scroll-mt-24", isOpen && "bg-[var(--accent-soft)]")} onClick={() => setOpen(isOpen ? null : id)}>
                        <td className="max-w-[20rem]">
                          <button type="button" className="flex w-full items-center gap-2 text-left" aria-expanded={isOpen} aria-label={`${name}: show every load`}>
                            <ChevronRight className={cn("size-3.5 shrink-0 text-ink-3 transition-transform", isOpen && "rotate-90")} aria-hidden />
                            {t.kind === "files" ? <FolderOpen className="size-4 shrink-0 text-ink-3" aria-hidden /> : <Database className="size-4 shrink-0 text-accent" aria-hidden />}
                            <span className="min-w-0">
                              <span className="flex items-center gap-1.5"><span className="truncate font-semibold text-ink">{name}</span><EnvBadge env={t.env} /></span>
                              <span className="block truncate font-mono text-[0.6875rem] text-ink-3" title={`${t.table} · ${t.pipeline} · ${t.steps.join(", ")}`}>{where} · {t.pipeline}</span>
                            </span>
                          </button>
                        </td>
                        <td className="w-56">
                          <div className="flex items-baseline justify-between gap-2"><span className="num text-[0.9375rem] font-semibold text-ink">{num(ins)}</span></div>
                          <div className="mt-1 h-1.5 rounded-full bg-surface-3"><div className="h-1.5 rounded-full" style={{ width: `${Math.max(ins ? 2 : 0, (ins / maxInserted) * 100)}%`, background: "var(--chart-ok)" }} /></div>
                        </td>
                        <td className="num-cell">{t.loads_count}{t.zero_loads ? <span className="block text-[0.6875rem] text-[var(--warning-ink)]">{t.zero_loads} empty</span> : null}</td>
                        <td className="whitespace-nowrap">
                          <span className="num font-semibold text-ink">{num(t.last.rows_copied)}</span> <span className="text-xs text-ink-3">rows · {ago(t.last.at)}</span>
                          <span className={cn("block text-[0.6875rem]", v.tone)}>{v.text}</span>
                        </td>
                        <td>{t.daily ? <MiniBars daily={t.daily} /> : null}</td>
                        <td className="whitespace-nowrap"><Delta now={ins} before={t.inserted_prev} /></td>
                        <td className="whitespace-nowrap">
                          {t.incident ? <Link to={`/p/${projectKey}/i/${t.incident.id}`} onClick={(e) => e.stopPropagation()} className="inline-flex items-center gap-1 text-xs font-semibold text-[var(--critical-ink)] hover:underline" title={t.incident.title}><TriangleAlert className="size-3.5" />Incident</Link>
                            : v.out ? <span className="inline-flex items-center gap-1 text-xs font-semibold text-[var(--warning-ink)]"><TriangleAlert className="size-3.5" />Unusual</span>
                            : <span className="inline-flex items-center gap-1 text-xs text-ink-2"><ShieldCheck className="size-3.5 text-[var(--good)]" />Normal</span>}
                          {t.quality && (
                            <span className={cn("mt-0.5 flex items-center gap-1 text-[0.6875rem]", t.quality.status === "fail" ? "text-[var(--critical-ink)]" : "text-ink-3")}
                              title={t.quality.failing.length ? `Failing: ${t.quality.failing.join(", ")}` : "Data checks pass"}>
                              {t.quality.status === "fail" ? <ShieldAlert className="size-3" /> : <ShieldCheck className="size-3" />}
                              {t.quality.status === "fail" ? `${t.quality.failing.length} data check${t.quality.failing.length > 1 ? "s" : ""} failing` : t.quality.status === "ok" ? "data checks pass" : "data checks learning"}
                            </span>
                          )}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-surface-2/60">
                          <td colSpan={7} className="!p-0"><TableDetail t={t} projectKey={projectKey} days={data!.days} /></td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </ChartCard>
      <p className="mt-3 text-[0.6875rem] text-ink-3">Rows inserted = rows each Copy step wrote into the table, from Data Factory's own run records. A run can show Failed and still have loaded a table: a run fails if any of its steps fails.</p>
    </div>
  );
}

function TableDetail({ t, projectKey, days }: { t: TableLoad; projectKey: string; days: number }) {
  const facts: [string, React.ReactNode][] = [
    ["Full name", <span className="font-mono">{t.table}</span>],
    ["Loaded by", <>{t.pipeline} · {t.steps.join(", ") || "–"}</>],
    ["Dataset", t.dataset ?? "–"],
    ["Usual rows per load", t.normal ? <>{num(t.normal.median)} <span className="text-ink-3">(normal {num(t.normal.low)}–{num(t.normal.high)}, from {t.normal.samples} loads)</span></> : "learning"],
    ["In this period", <>{num(t.inserted ?? 0)} inserted · {num(t.read ?? 0)} read · {num(t.skipped ?? 0)} skipped</>],
  ];
  return (
    <div className="space-y-5 px-6 py-5">
      {t.incident && <Notice tone="warn">Open incident: <Link className="font-semibold hover:underline" to={`/p/${projectKey}/i/${t.incident.id}`}>{t.incident.title}</Link></Notice>}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <div>
          <p className="mb-2 text-xs font-semibold text-ink-2">Rows inserted per day</p>
          {t.daily && t.daily.length > 1 ? <DayBars days={t.daily} height={160} label={`Rows inserted per day into ${t.table}`} />
            : <p className="text-sm text-ink-2"><b className="text-ink">{num(t.inserted ?? 0)}</b> rows today across {t.loads_count} load{t.loads_count === 1 ? "" : "s"}.</p>}
        </div>
        <dl className="grid grid-cols-[auto_1fr] content-start gap-x-4 gap-y-2 text-[0.8125rem]">
          {facts.map(([k, v]) => <Fragment key={k}><dt className="text-ink-3">{k}</dt><dd className="min-w-0 break-words text-ink">{v}</dd></Fragment>)}
          {t.quality && (
            <>
              <dt className="text-ink-3">Data quality</dt>
              <dd className="text-ink">{t.quality.status === "fail" ? <span className="text-[var(--critical-ink)]">Failing: {t.quality.failing.join(", ")}</span> : t.quality.status === "ok" ? "All checks pass" : "Learning"}
                {t.quality.checked_at ? <span className="text-ink-3"> · checked {ago(t.quality.checked_at)}</span> : null}</dd>
            </>
          )}
        </dl>
      </div>
      <div>
        <p className="mb-2 text-xs font-semibold text-ink-2">Every load in the last {days === 1 ? "day" : `${days} days`} ({t.loads.length})</p>
        {t.loads.length === 0 ? <p className="text-sm text-ink-3">No load in this period. Latest load: {num(t.last.rows_copied)} rows {ago(t.last.at)} ({dateTime(t.last.at)}).</p> : (
          <div className="max-h-96 overflow-auto rounded-xl bg-surface shadow-[inset_0_0_0_1px_var(--line)]">
            <table className="data-table">
              <thead className="sticky top-0 z-[1] bg-surface"><tr><th>Loaded</th><th className="text-right">Rows inserted</th><th className="text-right">Read</th><th className="text-right">Skipped</th><th>Against usual</th><th>Run</th><th /></tr></thead>
              <tbody>
                {t.loads.map((l) => {
                  const v = vsNormal(l.rows_copied, t.normal);
                  return (
                    <tr key={l.run_id}>
                      <td className="whitespace-nowrap">{dateTime(l.at)}<span className="block text-[0.6875rem] text-ink-3">{ago(l.at)}{l.trigger ? ` · ${l.trigger}` : ""}</span></td>
                      <td className="num-cell font-semibold">{num(l.rows_copied)}</td>
                      <td className="num-cell text-ink-2">{num(l.rows_read)}</td>
                      <td className={cn("num-cell", l.rows_skipped ? "font-semibold text-[var(--warning-ink)]" : "text-ink-3")}>{num(l.rows_skipped ?? 0)}</td>
                      <td className={cn("text-xs", v.tone)}>{v.text}</td>
                      <td className="whitespace-nowrap"><StatusChip status={l.run_status ?? "unknown"} />{l.duration_s ? <span className="ml-1.5 text-[0.6875rem] text-ink-3">{duration(l.duration_s)}</span> : null}</td>
                      <td><Link to={`/p/${projectKey}/runs?pipeline=${encodeURIComponent(t.pipeline)}&run=${encodeURIComponent(l.run_id)}`} className="inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold text-accent hover:underline">Open run <ExternalLink className="size-3" /></Link></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/** Under the run graph: what each target table got from this run. */
export function RunTables({ tables }: { tables: { step: string; table: string; env: string | null; kind: string | null; status: string; rows_read: number | null; rows_copied: number | null; rows_skipped: number | null; error: string | null }[] }) {
  if (!tables.length) return null;
  const rank: Record<string, number> = { PROD: 0, UAT: 1, DEV: 2 };
  tables = [...tables].sort((a, b) => shortName(a.table).name.localeCompare(shortName(b.table).name) || (rank[a.env ?? ""] ?? 3) - (rank[b.env ?? ""] ?? 3));
  const loaded = tables.filter((t) => t.status === "Succeeded").length;
  return (
    <div className="mt-4">
      <p className="mb-2 text-sm font-semibold text-ink">Tables loaded in this run <span className="font-normal text-ink-3">· {loaded} of {tables.length} loaded</span></p>
      <div className="overflow-auto rounded-xl shadow-[inset_0_0_0_1px_var(--line)]">
        <table className="data-table min-w-[44rem]">
          <thead><tr><th>Table</th><th>Copy step</th><th>Result</th><th className="text-right">Rows read</th><th className="text-right">Rows inserted</th><th className="text-right">Skipped</th></tr></thead>
          <tbody>
            {tables.map((t) => {
              const { name, where } = shortName(t.table);
              return (
                <tr key={t.step}>
                  <td><div className="flex items-center gap-1.5"><span className="font-semibold text-ink">{name}</span><EnvBadge env={t.env} /></div><div className="font-mono text-[0.6875rem] text-ink-3">{where}</div></td>
                  <td className="text-xs text-ink-2">{t.step}</td>
                  <td>{t.status === "Succeeded" ? <StatusChip status="Succeeded" /> : t.status === "Failed" ? <StatusChip status="Failed" /> : <span className="text-xs text-ink-3">not loaded (didn't run)</span>}
                    {t.status === "Failed" && t.error && <p className="mt-0.5 line-clamp-1 max-w-[18rem] text-[0.6875rem] text-[var(--critical-ink)]" title={t.error}>{t.error}</p>}</td>
                  <td className="num-cell">{num(t.rows_read)}</td>
                  <td className="num-cell font-semibold">{t.rows_copied != null ? num(t.rows_copied) : "–"}</td>
                  <td className={cn("num-cell", t.rows_skipped ? "text-[var(--warning-ink)]" : "text-ink-3")}>{t.rows_copied != null ? num(t.rows_skipped ?? 0) : "–"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
