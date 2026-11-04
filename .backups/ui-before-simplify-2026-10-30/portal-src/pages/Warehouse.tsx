// Warehouse cost: what nobody uses (tables, views, stored procedures, functions, pipelines), what it costs to keep,
// and the script to remove it. "Ready to remove" only on evidence: no use over a stretch the bot could see, at least
// the threshold long. What the pipelines load, read or call is never suggested. The bot never runs the script.
import {
  Archive, ArrowRight, Braces, Check, ChevronDown, CircleHelp, Copy, Database, Download, Eye, FileCode2, Hourglass, Layers, Link2, Loader2, Lock,
  PiggyBank, Pin, PinOff, RefreshCw, Search, ShieldAlert, Snowflake, Table2, Trash2, TriangleAlert, Workflow, X, Zap, type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Notice } from "@/components/Cards";
import { Modal } from "@/components/Databases";
import { DbPicker } from "@/components/DbPicker";
import { Button } from "@/components/ui/button";
import { EmptyState, Input, Segmented, Skeleton } from "@/components/ui/primitives";
import { api, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import { cn } from "@/lib/utils";

// ── shapes ───────────────────────────────────────────────────────────────────
type Status = "unused" | "watching" | "write_only" | "maybe" | "in_use" | "kept" | "unknown" | "wasted";
type ObjKind = "view" | "procedure" | "function";
interface Totals { tables: number; count: number; gb: number; monthly: number }
interface Base {
  key: string; schema: string; table: string; status: Status; why: string; confidence?: "high" | "medium" | "low";
  seen_days: number | null; idle_days: number | null; since: string | null; sure_in_days: number | null; created: string | null;
  deps: { object: string; kind: string }[];
}
interface TableRow extends Base {
  kind: "table"; rows: number | null; bytes: number | null; gb: number; monthly: number; yearly: number;
  last_read: string | null; last_write: string | null; altered: string | null;
}
interface ObjRow extends Base { kind: ObjKind; altered: string | null; uses: string[]; used_by: string[]; last_used: string | null; evidence: string | null }
interface Db {
  id: number; name: string; kind: string; host: string | null; database: string | null; status: string;
  scanned_at: string | null; error: string | null; usage: string | null; usage_error: string | null; covered_since: string | null; server_start: string | null;
  price: number; price_note: string; price_custom: boolean; grant: { why: string; sql: string[] } | null;
  tables: TableRow[]; totals: Record<string, Totals>; objects: ObjRow[]; object_totals: Record<ObjKind, Record<string, Totals>>;
  object_evidence: Record<ObjKind, string | null>;
}
interface Trigger { name: string; kind: string; started: boolean; every_hours?: number | null }
interface PipeRow {
  project: string; project_name: string; service: string; name: string; type: string; folder: string | null; description: string; steps: number;
  enabled: boolean; triggers: Trigger[]; calls: string[]; called_by: string[]; last_run: string | null; last_status: string | null;
  runs_30d: number; hours_30d: number; monthly: number; yearly: number; writes: { table: string; status: string | null; database: string }[];
  seen_days: number; idle_days: number | null; sure_in_days: number | null; saving: number; status: Status; why: string; confidence?: string;
  shared_triggers: string[];
}
interface PipeProject {
  key: string; name: string; service: string; factory: string; resource_group: string; covered_since: string; retention_days: number;
  inventory_at: string | null; rows: PipeRow[]; totals: Record<string, { count: number; monthly: number }>;
}
interface Report {
  days: number; generated_at: string; databases: Db[]; scope: number | null; all_databases: { id: number; name: string; kind: string }[];
  pipelines: { projects: PipeProject[]; price_note: string; summary: { unused: number; wasted: number; watching: number; monthly: number; run_monthly: number; pipelines: number } };
  summary: {
    unused_tables: number; unused_gb: number; monthly: number; yearly: number; watching: number; watching_monthly: number; write_only: number;
    total_gb: number; total_monthly: number; unused_views: number; unused_procedures: number; unused_functions: number;
    unused_pipelines: number; wasted_pipelines: number; pipelines_monthly: number; all_monthly: number; all_yearly: number;
  };
}
interface Script { sql: string; filename: string; tables: number; items: number; objects: number; gb: number; monthly: number; yearly: number; warnings: string[]; skipped: string[]; mode: string; language?: string }
type Tab = "tables" | "views" | "code" | "pipelines";
/** Where a selection's script comes from: a database (tables, views, procedures) or a project's pipelines. */
type Target = { kind: "db"; id: number; keys: string[] } | { kind: "pl"; id: string; keys: string[] };

// ── statuses, in the order a person works through them ───────────────────────
const STATUS: Record<Status, { label: string; icon: LucideIcon; tone: string; hint: string }> = {
  unused: { label: "Ready to remove", icon: Trash2, tone: "var(--critical)", hint: "Not used for the whole period, and the bot watched all of it" },
  wasted: { label: "Runs for nothing", icon: Zap, tone: "var(--critical)", hint: "It runs and costs money, but nobody reads what it loads" },
  watching: { label: "Watching", icon: Hourglass, tone: "var(--warning)", hint: "No use so far, but the bot hasn't watched long enough to be sure" },
  write_only: { label: "Loaded, never read", icon: Eye, tone: "var(--cat-4)", hint: "Something keeps writing to it, but nobody reads it" },
  maybe: { label: "Maybe", icon: CircleHelp, tone: "var(--cat-5)", hint: "Not changed for the period; reads can't be seen with this login" },
  in_use: { label: "In use", icon: Check, tone: "var(--good)", hint: "Used recently, or a pipeline, data check or something in use depends on it" },
  kept: { label: "Kept", icon: Pin, tone: "var(--accent)", hint: "Someone chose to keep it: never suggested again" },
  unknown: { label: "Can't tell", icon: CircleHelp, tone: "var(--ink-3)", hint: "Its use can't be seen with this login or from this database" },
};
const ORDER: Record<Tab, Status[]> = {
  tables: ["unused", "watching", "write_only", "maybe", "in_use", "kept", "unknown"],
  views: ["unused", "watching", "in_use", "kept", "unknown"],
  code: ["unused", "watching", "in_use", "kept", "unknown"],
  pipelines: ["wasted", "unused", "watching", "in_use", "kept"],
};
const SELECTABLE: Status[] = ["unused", "watching", "write_only", "maybe", "unknown", "wasted"];
const PERIODS = [{ value: 90, label: "3 months" }, { value: 180, label: "6 months" }, { value: 365, label: "12 months" }] as const;
type Period = (typeof PERIODS)[number]["value"];
const EVIDENCE: Record<string, string> = {
  base_tables: "judged by its tables: a view is read through them, so if none were read, nobody queried it",
  plan_cache: "SQL Server's execution statistics (it can forget them when memory is short: medium confidence)",
  access_history: "Snowflake's access history: every read, for a year",
  query_history: "Snowflake's query history: every CALL, for a year",
};

// ── numbers ──────────────────────────────────────────────────────────────────
const nf = new Intl.NumberFormat("en-US");
const money = (v: number) => (v === 0 ? "$0" : Math.abs(v) < 0.01 ? "< $0.01" : Math.abs(v) < 1000 ? `$${v.toFixed(2)}` : `$${nf.format(Math.round(v))}`);
function size(gb: number) {
  if (gb >= 1024) return `${(gb / 1024).toFixed(gb >= 10240 ? 0 : 1)} TB`;
  if (gb >= 1) return `${gb.toFixed(gb >= 100 ? 0 : 1)} GB`;
  const mb = gb * 1024;
  return mb >= 1 ? `${mb.toFixed(mb >= 100 ? 0 : 1)} MB` : `${Math.max(1, Math.round(mb * 1024))} KB`;
}
const span = (d: number | null) => (d == null ? "–" : d < 1 ? "today" : d < 60 ? `${Math.round(d)} days` : d < 730 ? `${Math.round(d / 30.4)} months` : `${(d / 365).toFixed(1)} years`);
const when = (iso: string | null) => (iso ? ago(iso) : "never seen");
const dateOf = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "–");
const hours = (h: number) => (h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(h < 10 ? 1 : 0)} h`);
const plural = (n: number, one: string, many = `${one}s`) => `${nf.format(n)} ${n === 1 ? one : many}`;

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
const Count = ({ value, fmt }: { value: number; fmt: (n: number) => string }) => <>{fmt(useCountUp(value))}</>;

const stored = <T,>(key: string, ok: (v: string) => boolean, fallback: T): T => {
  try { const v = localStorage.getItem(key); return v != null && ok(v) ? (isNaN(Number(v)) ? v : Number(v)) as T : fallback; } catch { return fallback; }
};
const store = (key: string, v: string | number) => { try { localStorage.setItem(key, String(v)); } catch { /* storage blocked */ } };

// ── the page ─────────────────────────────────────────────────────────────────
export function Warehouse() {
  const [days, setDaysRaw] = useState<Period>(() => stored("alertbot-wh-days", (v) => PERIODS.some((p) => String(p.value) === v), 180 as Period));
  const [tab, setTabRaw] = useState<Tab>(() => stored("alertbot-wh-tab", (v) => ["tables", "views", "code", "pipelines"].includes(v), "tables" as Tab));
  const setDays = (d: Period) => { setDaysRaw(d); store("alertbot-wh-days", d); };
  const setTab = (t: Tab) => { setTabRaw(t); store("alertbot-wh-tab", t); };
  const [db, setDbRaw] = useState<number | null>(() => stored<number | null>("alertbot-wh-db", (v) => /^\d+$/.test(v), null));
  const setDb = (d: number | null) => { setDbRaw(d); store("alertbot-wh-db", d ?? "all"); setPicked({}); };
  const scope = db === null ? "" : `&conn=${db}`;
  const { data, error, reload, setData } = useApi<Report>(`/api/storage?days=${days}${scope}`, { refreshMs: 60000 });
  const [scanning, setScanning] = useState(false);
  const [picked, setPicked] = useState<Record<string, string[]>>({});  // "db:3" | "pl:adf-x" → keys
  const [scriptFor, setScriptFor] = useState<Target[] | null>(null);
  const autoScanned = useRef(false);
  useNow();

  const scan = async (quiet = false) => {
    setScanning(true);
    const id = quiet ? undefined : toast.loading("Reading your databases' catalogues…", { description: "Sizes, and when each table, view and procedure was last used. Never a row." });
    try {
      const r = await post<Report>(`/api/storage/scan?days=${days}${scope}`);
      setData(r);
      const failed = r.databases.filter((d) => d.error);
      if (!quiet) {
        if (failed.length) toast.warning(`Scanned, but ${plural(failed.length, "database")} couldn't be read`, { id, description: failed.map((d) => `${d.name}: ${d.error}`).join(" · ") });
        else toast.success(r.summary.all_monthly ? `You could save ${money(r.summary.all_monthly)} a month` : "Scanned: nothing unused for that long", { id });
      }
    } catch (e) {
      if (!quiet) toast.error("Couldn't scan", { id, description: (e as Error).message });
    } finally {
      setScanning(false);
    }
  };

  useEffect(() => {  // the chosen database was removed: back to all of them
    if (data && db !== null && !data.all_databases.some((d) => d.id === db)) setDb(null);
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {  // the first visit: nothing scanned yet → scan straight away (the worker keeps it fresh hourly after that)
    if (!data || autoScanned.current) return;
    if (data.databases.length && data.databases.every((d) => !d.scanned_at)) { autoScanned.current = true; void scan(true); }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  // a rescan or a new threshold: forget picks that no longer qualify
  useEffect(() => {
    if (!data) return;
    setPicked((p) => {
      const ok = new Map<string, Set<string>>();
      for (const d of data.databases) ok.set(`db:${d.id}`, new Set([...d.tables, ...d.objects].filter((t) => SELECTABLE.includes(t.status)).map((t) => t.key)));
      for (const pr of data.pipelines.projects) ok.set(`pl:${pr.key}`, new Set(pr.rows.filter((r) => SELECTABLE.includes(r.status)).map((r) => r.name)));
      const next: Record<string, string[]> = {};
      for (const [k, keys] of Object.entries(p)) { const keep = keys.filter((x) => ok.get(k)?.has(x)); if (keep.length) next[k] = keep; }
      return next;
    });
  }, [data]);

  if (error && !data) return <div className="rounded-2xl bg-[var(--warning-soft)] p-4 text-sm text-ink">Couldn't load warehouse costs: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></div>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-60 w-full rounded-[1.75rem]" /><Skeleton className="h-16 w-full" /><Skeleton className="h-96 w-full" /></div>;

  const toggle = (target: string, keys: string[], on: boolean) => setPicked((p) => {
    const cur = new Set(p[target] ?? []);
    keys.forEach((k) => (on ? cur.add(k) : cur.delete(k)));
    return { ...p, [target]: [...cur] };
  });
  const targets = (): Target[] => Object.entries(picked).filter(([, k]) => k.length).map(([t, keys]) => t.startsWith("db:")
    ? { kind: "db" as const, id: Number(t.slice(3)), keys } : { kind: "pl" as const, id: t.slice(3), keys });
  const s = data.summary;
  const counts: Record<Tab, { n: number; money?: number }> = {
    tables: { n: s.unused_tables, money: s.monthly }, views: { n: s.unused_views },
    code: { n: s.unused_procedures + s.unused_functions }, pipelines: { n: s.unused_pipelines + s.wasted_pipelines, money: s.pipelines_monthly },
  };
  const nothing = !data.databases.length && !data.pipelines.projects.length;

  return (
    <div className="wh pb-32">
      <Hero data={data} days={days} setDays={setDays} scan={scan} scanning={scanning} goto={setTab} db={db} setDb={setDb} />
      {nothing ? (
        <EmptyState icon={Database} action={<Button asChild variant="primary"><Link to="/databases">Connect a database <ArrowRight /></Link></Button>}>
          Connect a SQL Server or Snowflake database and the bot will find the tables, views and procedures nobody uses, what they cost, and write the script to remove them.
        </EmptyState>
      ) : (
        <>
          <KindTabs tab={tab} setTab={setTab} counts={counts} />
          {tab === "pipelines" ? (
            data.pipelines.projects.length === 0
              ? <EmptyState icon={Workflow}>No Data Factory, Synapse or Logic Apps project of yours has an inventory yet. Once the bot has listed a factory's pipelines, they show here.</EmptyState>
              : <div className="space-y-5">
                {data.pipelines.projects.map((p, i) => (
                  <PipeCard key={p.key} project={p} days={days} index={i} picked={picked[`pl:${p.key}`] ?? []} onPick={(k, on) => toggle(`pl:${p.key}`, k, on)}
                    onScript={(keys) => setScriptFor([{ kind: "pl", id: p.key, keys }])} onChanged={reload} priceNote={data.pipelines.price_note} />
                ))}
              </div>
          ) : !data.databases.length ? (
            <EmptyState icon={Database} action={<Button asChild variant="primary"><Link to="/databases">Connect a database <ArrowRight /></Link></Button>}>
              Connect a SQL Server or Snowflake database to see its unused {tab === "tables" ? "tables" : tab === "views" ? "views" : "procedures and functions"}.
            </EmptyState>
          ) : (
            <div className="space-y-5">
              {[...data.databases].sort((a, b) => b.totals.unused.monthly - a.totals.unused.monthly || b.totals.all.monthly - a.totals.all.monthly).map((d, i) => (
                <DbCard key={d.id} db={d} tab={tab} days={days} index={i} picked={picked[`db:${d.id}`] ?? []} onPick={(k, on) => toggle(`db:${d.id}`, k, on)}
                  onScript={(keys) => setScriptFor([{ kind: "db", id: d.id, keys }])} onChanged={reload} />
              ))}
            </div>
          )}
          <HowItWorks tab={tab} />
        </>
      )}
      <SelectionBar data={data} picked={picked} onClear={() => setPicked({})} onScript={() => setScriptFor(targets())} />
      <ScriptModal targets={scriptFor} data={data} onClose={() => setScriptFor(null)} />
    </div>
  );
}

// ── hero ─────────────────────────────────────────────────────────────────────
function Hero({ data, days, setDays, scan, scanning, goto, db, setDb }: {
  data: Report; days: Period; setDays: (d: Period) => void; scan: () => void; scanning: boolean; goto: (t: Tab) => void;
  db: number | null; setDb: (d: number | null) => void;
}) {
  const s = data.summary;
  const last = data.databases.map((d) => d.scanned_at).filter(Boolean).sort().pop() ?? null;
  const period = PERIODS.find((p) => p.value === days)!.label;
  const tidy: [Tab, number, string][] = [
    ["tables", s.unused_tables, "table"], ["views", s.unused_views, "view"], ["code", s.unused_procedures, "procedure"],
    ["code", s.unused_functions, "function"], ["pipelines", s.unused_pipelines + s.wasted_pipelines, "pipeline"],
  ];
  const share = s.total_monthly ? Math.min(100, (s.monthly / s.total_monthly) * 100) : 0;
  const watchShare = s.total_monthly ? Math.min(100 - share, (s.watching_monthly / s.total_monthly) * 100) : 0;
  return (
    <section className="ai-hero wh-hero card-in relative mb-4 overflow-hidden rounded-[1.75rem] p-6 sm:p-8">
      <div className="ai-hero-glow wh-glow" aria-hidden />
      <div className="relative flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <p className="eyebrow flex items-center gap-2 font-semibold text-accent"><PiggyBank className="size-3.5" /> Warehouse cost</p>
          {data.all_databases.length > 0 && <DbPicker options={data.all_databases} value={db} onChange={setDb} />}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.75rem] text-ink-3">Unused for at least</span>
          <Segmented<Period> size="sm" label="Unused for at least" value={days} onChange={setDays} options={PERIODS.map((p) => ({ ...p }))} />
          <Button size="sm" variant="secondary" onClick={() => scan()} disabled={scanning || !data.databases.length}>
            {scanning ? <Loader2 className="animate-spin" /> : <RefreshCw />}{scanning ? "Scanning…" : "Scan now"}
          </Button>
        </div>
      </div>
      <div className="relative mt-4 grid gap-6 lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)] lg:items-end">
        <div className="min-w-0">
          <p className="text-[0.9375rem] font-medium text-ink-2">You could save</p>
          <h1 className="display mt-1 flex flex-wrap items-baseline gap-x-3 text-[2.75rem] font-bold leading-[1.02] tracking-[-0.03em] text-ink sm:text-[3.5rem]">
            <span className="tabular-nums"><Count value={s.all_monthly} fmt={money} /></span>
            <span className="text-[1.125rem] font-semibold tracking-normal text-ink-2 sm:text-[1.25rem]">a month</span>
            <span className="wh-pill self-center"><PiggyBank className="size-3.5 text-[var(--good)]" /><Count value={s.all_yearly} fmt={money} /> a year</span>
          </h1>
          <ul className="mt-3 space-y-1 text-[0.9375rem] leading-6 text-ink-2">
            <li><b className="font-semibold text-ink">{money(s.monthly)}</b> in storage: {plural(s.unused_tables, "table")} ({size(s.unused_gb)}) nobody has read or written for {period} or more</li>
            {db === null && <li><b className="font-semibold text-ink">{money(s.pipelines_monthly)}</b> in pipeline runs: {plural(s.wasted_pipelines, "pipeline")} that load{s.wasted_pipelines === 1 ? "s" : ""} only tables nobody reads</li>}
          </ul>
          <div className="mt-4 flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-[0.75rem] font-medium text-ink-3">Ready to tidy:</span>
            {tidy.map(([t, n, what]) => (
              <button key={what} onClick={() => goto(t)} className={cn("wh-tidy", !n && "is-zero")}>{plural(n, what)}<ArrowRight className="size-3" /></button>
            ))}
          </div>
          {db !== null && <p className="mt-2 text-[0.8125rem] text-ink-3">Pipelines aren't inside one database, so their savings show under All databases and the Pipelines tab.</p>}
          <p className="mt-3 text-[0.75rem] text-ink-3">{last ? `Databases scanned ${ago(last)} · the bot rescans every hour` : data.databases.length ? "Not scanned yet" : "No database connected yet"}</p>
        </div>
        <div className="ai-whatif min-w-0 rounded-2xl p-4 sm:p-5">
          <p className="text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">What you pay to store it all</p>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="display text-[2rem] font-bold leading-none tabular-nums text-ink"><Count value={s.total_monthly} fmt={money} /></span>
            <span className="text-sm text-ink-2">a month · {size(s.total_gb)}</span>
          </div>
          <div className="wh-split mt-3" role="img" aria-label={`${share.toFixed(0)}% could go now, ${watchShare.toFixed(0)}% being watched`}>
            {share > 0 && <span className="ai-grow-x" style={{ width: `${Math.max(share, 1.5)}%`, background: "var(--critical)" }} />}
            {watchShare > 0 && <span className="ai-grow-x" style={{ width: `${Math.max(watchShare, 1.5)}%`, background: "var(--warning)" }} />}
            <span style={{ flex: 1, background: "var(--surface-3)" }} />
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.75rem] text-ink-2">
            <Legend color="var(--critical)">{share.toFixed(share < 10 && share > 0 ? 1 : 0)}% could go now</Legend>
            <Legend color="var(--warning)">{watchShare.toFixed(watchShare < 10 && watchShare > 0 ? 1 : 0)}% watching</Legend>
            <Legend color="var(--surface-3)">the rest is in use</Legend>
          </div>
          {data.pipelines.summary.run_monthly > 0 && (
            <p className="mt-3 border-t border-line pt-3 text-[0.8125rem] text-ink-2">
              Pipeline runs: about <b className="text-ink">{money(data.pipelines.summary.run_monthly)}</b> a month across {plural(data.pipelines.summary.pipelines, "pipeline")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

const Legend = ({ color, children }: { color: string; children: ReactNode }) => (
  <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: color, boxShadow: "inset 0 0 0 1px var(--line-strong)" }} />{children}</span>
);

function KindTabs({ tab, setTab, counts }: { tab: Tab; setTab: (t: Tab) => void; counts: Record<Tab, { n: number; money?: number }> }) {
  const tabs: [Tab, string, LucideIcon][] = [["tables", "Tables", Table2], ["views", "Views", Layers], ["code", "Procedures & functions", Braces], ["pipelines", "Pipelines", Workflow]];
  return (
    <nav className="wh-kinds mb-4" aria-label="What to look at">
      <div className="wh-kinds-inner" role="tablist">
        {tabs.map(([id, label, I]) => {
          const c = counts[id];
          return (
            <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)} className={cn("wh-kind", tab === id && "is-on")}>
              <I className="size-4 shrink-0" aria-hidden />
              <span className="min-w-0 text-left">
                <span className="block truncate text-[0.875rem] font-semibold">{label}</span>
                <span className="block truncate text-[0.71875rem] text-ink-3">
                  {c.n ? <><b className="font-semibold text-[var(--critical)]">{c.n} ready</b>{c.money ? ` · ${money(c.money)}/mo` : ""}</> : "nothing to remove"}
                </span>
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

// ── one database ─────────────────────────────────────────────────────────────
type AnyRow = TableRow | ObjRow;

function DbCard({ db, tab, days, index, picked, onPick, onScript, onChanged }: {
  db: Db; tab: Exclude<Tab, "pipelines">; days: Period; index: number; picked: string[]; onPick: (keys: string[], on: boolean) => void;
  onScript: (keys: string[]) => void; onChanged: () => void;
}) {
  const all: AnyRow[] = useMemo(() => (tab === "tables" ? db.tables : db.objects.filter((o) => (tab === "views" ? o.kind === "view" : o.kind !== "view"))), [db, tab]);
  const countOf = (st: Status | "all") => (st === "all" ? all.length : all.filter((r) => r.status === st).length);
  const costOf = (st: Status) => all.filter((r) => r.status === st).reduce((a, r) => a + (r.kind === "table" ? r.monthly : 0), 0);
  const firstWith = ORDER[tab].find((s) => countOf(s) > 0 && s !== "in_use" && s !== "kept") ?? "all";
  const [group, setGroup] = useState<Status | "all">(firstWith);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [keeping, setKeeping] = useState<AnyRow | null>(null);
  const [grantOpen, setGrantOpen] = useState(false);
  useEffect(() => { setGroup(firstWith); }, [tab, db.scanned_at, days]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const cost = (r: AnyRow) => (r.kind === "table" ? r.monthly : 0);
    return all.filter((t) => (group === "all" || t.status === group) && (!needle || t.key.toLowerCase().includes(needle)))
      .sort((a, b) => cost(b) - cost(a) || (b.idle_days ?? -1) - (a.idle_days ?? -1) || a.key.localeCompare(b.key));
  }, [all, group, q]);
  const maxGb = Math.max(...db.tables.map((t) => t.gb), 0.000001);
  const selectable = rows.filter((t) => SELECTABLE.includes(t.status));
  const allOn = selectable.length > 0 && selectable.every((t) => picked.includes(t.key));
  const ready = all.filter((r) => r.status === "unused");
  const Kind = db.kind === "snowflake" ? Snowflake : Database;
  const evidence = tab === "views" ? db.object_evidence.view : tab === "code" ? db.object_evidence.procedure : null;
  const what = tab === "tables" ? "tables" : tab === "views" ? "views" : "procedures and functions";

  const unkeep = async (t: AnyRow) => {
    try {
      await api(`/api/storage/${db.id}/keep`, { method: "PUT", body: JSON.stringify({ key: t.key, keep: false }) });
      toast.success(`${t.key} can be suggested again`);
      onChanged();
    } catch (e) { toast.error("Couldn't change that", { description: (e as Error).message }); }
  };
  const keep = async (reason: string) => {
    if (!keeping) return;
    await api(`/api/storage/${db.id}/keep`, { method: "PUT", body: JSON.stringify({ key: keeping.key, keep: true, reason: reason || null }) });
    toast.success(`Keeping ${keeping.key}`, { description: "It won't be suggested again. Unpin it any time from the Kept filter." });
    setKeeping(null);
    onChanged();
  };

  return (
    <section className="wh-db card-in overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]" style={{ animationDelay: `${index * 60}ms` }}>
      <header className="flex flex-wrap items-start gap-3 border-b border-line px-5 py-4 sm:px-6">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><Kind className="size-5" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <h2 className="display flex flex-wrap items-center gap-2 text-[1.0625rem] font-semibold text-ink">
            {db.name}
            <span className="ai-chip">{db.kind === "sqlserver" ? "SQL Server" : db.kind === "snowflake" ? "Snowflake" : db.kind}</span>
            {tab === "tables" ? <UsageChip db={db} /> : evidence ? <span className="ai-chip good" title={EVIDENCE[evidence]}>usage visible</span> : <span className="ai-chip">use can't be seen</span>}
          </h2>
          <p className="mt-0.5 text-[0.8125rem] text-ink-3">
            {db.database}{db.host ? ` on ${db.host}` : ""} · {db.scanned_at ? `scanned ${ago(db.scanned_at)}` : "not scanned yet"}
            {db.covered_since ? ` · usage seen since ${dateOf(db.covered_since)}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {tab === "tables" && <PriceEditor db={db} onChanged={onChanged} />}
          {ready.length > 0 && (
            <Button size="sm" variant="outline" onClick={() => onScript(ready.map((t) => t.key))}>
              <FileCode2 /> Script {ready.length} ready{tab === "tables" && costOf("unused") ? ` · ${money(costOf("unused"))}/mo` : ""}
            </Button>
          )}
        </div>
      </header>

      {db.error && <Notice tone="warn" className="mx-5 mt-4 sm:mx-6">Couldn't read this database: {db.error}. The last good scan is shown.</Notice>}
      {db.grant && (
        <div className="mx-5 mt-4 rounded-xl bg-[var(--warning-soft)] px-4 py-3 text-[0.875rem] text-ink sm:mx-6">
          <div className="flex flex-wrap items-center gap-2">
            <ShieldAlert className="size-4 shrink-0 text-[var(--warning)]" aria-hidden />
            <span className="min-w-0 flex-1"><b className="font-semibold">Sizes only for now.</b> {db.kind === "snowflake" ? "Without access history the bot can only see when a table last changed, not when it was read." : "Without usage statistics the bot can't tell which tables, views and procedures are used."}</span>
            <button className="text-[0.8125rem] font-medium text-accent underline-offset-2 hover:underline" onClick={() => setGrantOpen((o) => !o)}>{grantOpen ? "Hide" : "How to fix"}</button>
          </div>
          {grantOpen && (
            <div className="mt-2.5 space-y-2">
              <p className="text-[0.8125rem] text-ink-2">{db.grant.why}</p>
              {db.grant.sql.map((s) => <CodeLine key={s} code={s} />)}
              <p className="text-[0.75rem] text-ink-3">Read-only either way: these only let the login see statistics, never data.</p>
            </div>
          )}
        </div>
      )}
      {tab !== "tables" && evidence && <p className="mx-5 mt-4 rounded-xl bg-surface-2 px-4 py-2.5 text-[0.8125rem] text-ink-2 sm:mx-6">How it's judged: {EVIDENCE[evidence]}. These take no storage; removing them tidies the database and leaves less to break.</p>}

      <Filters order={ORDER[tab]} group={group} setGroup={setGroup} countOf={countOf} costOf={tab === "tables" ? costOf : undefined} q={q} setQ={setQ}
        placeholder={`Find a ${tab === "tables" ? "table" : tab === "views" ? "view" : "procedure or function"}`} />

      {rows.length === 0 ? (
        <div className="px-5 py-10 text-center text-[0.9375rem] text-ink-3 sm:px-6">
          {all.length === 0 ? (db.scanned_at ? `The scan found no ${what}.` : "Scanning…")
            : group === "unused" ? <span className="inline-flex items-center gap-2"><Check className="size-4 text-[var(--good)]" />Nothing unused for {PERIODS.find((p) => p.value === days)!.label} or more.</span> : "Nothing here."}
        </div>
      ) : (
        <div className="wh-list mt-2" role="table" aria-label={what}>
          <div className="wh-grid wh-head" role="row">
            <span role="columnheader">{selectable.length > 0 && <input type="checkbox" className="wh-check" checked={allOn} aria-label="Select every one shown" onChange={(e) => onPick(selectable.map((t) => t.key), e.target.checked)} />}</span>
            <span role="columnheader">{tab === "tables" ? "Table" : tab === "views" ? "View" : "Procedure or function"}</span>
            <span className="wh-metrics">
              {(tab === "tables" ? ["Size", "Last read", "Last write", "Cost"] : ["Type", "Last used", "Unused for", "Used by"]).map((h) => <span key={h} role="columnheader">{h}</span>)}
            </span>
            <span role="columnheader" />
          </div>
          {rows.slice(0, 400).map((t) => (
            <ItemRow key={t.key} row={t} maxGb={maxGb} on={picked.includes(t.key)} open={open === t.key} onToggle={() => setOpen(open === t.key ? null : t.key)}
              onPick={(on) => onPick([t.key], on)} onScript={() => onScript([t.key])} onKeep={() => setKeeping(t)} onUnkeep={() => unkeep(t)} db={db} />
          ))}
          {rows.length > 400 && <p className="px-6 py-3 text-[0.8125rem] text-ink-3">Showing the first 400 of {nf.format(rows.length)}: search to narrow it down.</p>}
        </div>
      )}
      <KeepModal what={keeping?.key ?? ""} cost={keeping && keeping.kind === "table" ? `${money(keeping.monthly)} a month (${size(keeping.gb)})` : null}
        open={keeping != null} onClose={() => setKeeping(null)} onKeep={keep} />
    </section>
  );
}

function Filters({ order, group, setGroup, countOf, costOf, q, setQ, placeholder }: {
  order: Status[]; group: Status | "all"; setGroup: (s: Status | "all") => void; countOf: (s: Status | "all") => number;
  costOf?: (s: Status) => number; q: string; setQ: (v: string) => void; placeholder: string;
}) {
  return (
    <div className="px-5 pt-4 sm:px-6">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap gap-1.5" role="tablist" aria-label="Filter">
          {[...order, "all" as const].map((g) => {
            const n = countOf(g);
            if (!n && g !== "all" && g !== order[0] && g !== group) return null;
            const meta = g === "all" ? { label: "All", icon: Layers, tone: "var(--ink-3)", hint: "Everything the scan found" } : STATUS[g];
            const cost = g !== "all" && costOf ? costOf(g) : 0;
            return (
              <button key={g} role="tab" aria-selected={group === g} title={meta.hint} onClick={() => setGroup(g)} className={cn("wh-tab", group === g && "is-on")}>
                <meta.icon className="size-3.5" style={{ color: meta.tone }} aria-hidden />{meta.label}<span className="wh-count">{nf.format(n)}</span>
                {cost > 0 && (g === "unused" || g === "watching" || g === "wasted") && <span className="wh-cost">{money(cost)}/mo</span>}
              </button>
            );
          })}
        </div>
        <label className="relative w-full sm:w-60">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={placeholder} aria-label={placeholder} className="h-9 pl-8 text-[0.875rem]" />
        </label>
      </div>
      <p className="pt-2 text-[0.8125rem] text-ink-3">{group === "all" ? "Everything the scan found." : `${STATUS[group].hint}.`}</p>
    </div>
  );
}

function UsageChip({ db }: { db: Db }) {
  const exact = db.usage === "dmv" || db.usage === "access_history";
  const label = db.usage === "dmv" ? "Usage: SQL Server statistics" : db.usage === "access_history" ? "Usage: access history" : db.usage === "last_altered" ? "Changes only" : db.scanned_at ? "Sizes only" : null;
  if (!label) return null;
  const tip = db.usage === "dmv" ? `Reads and writes from SQL Server's own usage statistics. They empty when the server restarts${db.server_start ? ` (last ${ago(db.server_start)})` : ""}, so the bot keeps its own record from every scan.`
    : db.usage === "access_history" ? "Snowflake's ACCESS_HISTORY: every query's reads and writes, for up to a year." : db.usage_error ?? "";
  return <span className={cn("ai-chip", exact && "good")} title={tip}>{label}</span>;
}

// ── a row: the summary line, and everything about it when opened ─────────────
function Metrics({ items }: { items: [string, ReactNode][] }) {
  return <span className="wh-metrics">{items.map(([label, v]) => <span key={label} className="wh-metric"><span className="wh-metric-label">{label}</span><span className="wh-metric-v">{v}</span></span>)}</span>;
}

function ItemRow({ row: t, maxGb, on, open, onToggle, onPick, onScript, onKeep, onUnkeep, db }: {
  row: AnyRow; maxGb: number; on: boolean; open: boolean; onToggle: () => void; onPick: (on: boolean) => void; onScript: () => void;
  onKeep: () => void; onUnkeep: () => void; db: Db;
}) {
  const st = STATUS[t.status];
  const can = SELECTABLE.includes(t.status);
  const guarded = t.status === "in_use" && t.why.startsWith("Used by pipeline");
  const metrics: [string, ReactNode][] = t.kind === "table" ? [
    ["Size", <span key="s" className="flex items-center gap-2"><span className="wh-bar" aria-hidden><span className="ai-grow-x" style={{ width: `${Math.max(2, (t.gb / maxGb) * 100)}%`, background: st.tone }} /></span><span className="tabular-nums">{size(t.gb)}</span></span>],
    ["Last read", when(t.last_read)],
    ["Last write", when(t.last_write ?? t.altered)],
    ["Cost", <span key="c"><b className="font-semibold tabular-nums text-ink">{money(t.monthly)}</b><span className="text-ink-3">/mo</span></span>],
  ] : [
    ["Type", <span key="t" className="capitalize">{t.kind}</span>],
    ["Last used", t.status === "unknown" ? "can't see" : when(t.last_used)],
    ["Unused for", t.status === "in_use" && !t.idle_days ? "–" : span(t.idle_days)],
    ["Used by", t.used_by.length ? plural(t.used_by.length, "object") : "nothing"],
  ];
  return (
    <div className={cn("wh-item", on && "is-on", open && "is-open")} role="row">
      <div className="wh-grid">
        <span>{can && <input type="checkbox" className="wh-check" checked={on} onChange={(e) => onPick(e.target.checked)} aria-label={`Select ${t.key}`} />}</span>
        <button className="wh-name" onClick={onToggle} aria-expanded={open}>
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="wh-dot" style={{ background: st.tone }} title={st.label} aria-hidden />
            <span className="min-w-0 font-semibold text-ink [overflow-wrap:anywhere]"><span className="font-normal text-ink-3">{t.schema}.</span>{t.table}</span>
            <span className="wh-status" style={{ color: st.tone }}>{st.label}</span>
            {guarded && <span className="wh-badge"><Lock className="size-3" />pipeline</span>}
            {t.deps.length > 0 && <span className="wh-badge warn"><Link2 className="size-3" />{t.deps.length} depend{t.deps.length === 1 ? "s" : ""} on it</span>}
            {t.confidence && t.confidence !== "high" && t.status !== "in_use" && <span className="wh-badge">{t.confidence} confidence</span>}
          </span>
          <span className="mt-1 block text-[0.8125rem] leading-5 text-ink-2">
            {t.why}{t.status === "watching" && t.sure_in_days != null ? ` · sure on ${new Date(Date.now() + t.sure_in_days * 864e5).toLocaleDateString(undefined, { day: "numeric", month: "short" })}` : ""}
          </span>
          <ChevronDown className="wh-chev size-4 text-ink-3" aria-hidden />
        </button>
        <Metrics items={metrics} />
        <span className="wh-actions">
          {can && <Button size="sm" variant="ghost" onClick={onScript} title="Script for this one"><FileCode2 />Script</Button>}
          {t.status === "kept"
            ? <Button size="icon-sm" variant="ghost" onClick={onUnkeep} title="Stop keeping (it can be suggested again)" aria-label={`Stop keeping ${t.key}`}><PinOff /></Button>
            : can && <Button size="icon-sm" variant="ghost" onClick={onKeep} title="Keep it: never suggest it again" aria-label={`Keep ${t.key}`}><Pin /></Button>}
        </span>
      </div>
      {open && <Details row={t} db={db} />}
    </div>
  );
}

function Details({ row: t, db }: { row: AnyRow; db: Db }) {
  const facts: [string, ReactNode][] = t.kind === "table" ? [
    ["Rows", t.rows == null ? "–" : nf.format(t.rows)], ["Size", `${size(t.gb)} · ${money(t.monthly)} a month · ${money(t.yearly)} a year`],
    ["Created", dateOf(t.created)], ["Last read", t.last_read ? `${dateOf(t.last_read)} (${ago(t.last_read)})` : "never, as far as the bot can see"],
    ["Last write", t.last_write ? `${dateOf(t.last_write)} (${ago(t.last_write)})` : t.altered ? `changed ${dateOf(t.altered)}` : "never, as far as the bot can see"],
    ["Watched for", t.seen_days != null ? span(t.seen_days) : "–"],
  ] : [
    ["Type", <span key="k" className="capitalize">{t.kind}</span>], ["Created", dateOf(t.created)], ["Last changed", dateOf(t.altered)],
    ["Last used", t.last_used ? `${dateOf(t.last_used)} (${ago(t.last_used)})` : "not seen"], ["Watched for", t.seen_days != null ? span(t.seen_days) : "–"],
    ["Evidence", t.evidence ? EVIDENCE[t.evidence] : "none: its use can't be seen"],
  ];
  const usedBy = t.kind === "table" ? t.deps.map((d) => `${d.object} (${d.kind})`) : t.used_by;
  return (
    <div className="wh-details">
      <dl className="wh-facts">{facts.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
      <div className="grid gap-4 sm:grid-cols-2">
        {t.kind !== "table" && <RefList title="It uses" items={t.uses} empty="nothing the bot can see" />}
        <RefList title={t.kind === "table" ? "Depends on it" : "Used by"} items={usedBy} empty="nothing: removing it breaks nothing the bot can see" warn />
      </div>
      <p className="text-[0.75rem] text-ink-3">{db.name} · {db.database}{db.host ? ` on ${db.host}` : ""}</p>
    </div>
  );
}

const RefList = ({ title, items, empty, warn }: { title: string; items: string[]; empty: string; warn?: boolean }) => (
  <div className="min-w-0">
    <p className="mb-1 text-[0.75rem] font-semibold uppercase tracking-[0.06em] text-ink-3">{title}</p>
    {items.length ? (
      <ul className="space-y-0.5 text-[0.8125rem] text-ink">{items.map((x) => <li key={x} className="flex items-center gap-1.5 break-all">{warn ? <TriangleAlert className="size-3.5 shrink-0 text-[var(--warning)]" /> : <ArrowRight className="size-3.5 shrink-0 text-ink-3" />}{x}</li>)}</ul>
    ) : <p className="text-[0.8125rem] text-ink-3">{empty}</p>}
  </div>
);

// ── pipelines ────────────────────────────────────────────────────────────────
function PipeCard({ project: p, days, index, picked, onPick, onScript, onChanged, priceNote }: {
  project: PipeProject; days: Period; index: number; picked: string[]; onPick: (k: string[], on: boolean) => void; onScript: (k: string[]) => void;
  onChanged: () => void; priceNote: string;
}) {
  const countOf = (s: Status | "all") => (s === "all" ? p.rows.length : p.rows.filter((r) => r.status === s).length);
  const costOf = (s: Status) => p.rows.filter((r) => r.status === s).reduce((a, r) => a + (s === "wasted" ? r.saving : 0), 0);
  const firstWith = ORDER.pipelines.find((s) => countOf(s) > 0 && s !== "in_use" && s !== "kept") ?? "all";
  const [group, setGroup] = useState<Status | "all">(firstWith);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [keeping, setKeeping] = useState<PipeRow | null>(null);
  useEffect(() => { setGroup(firstWith); }, [days, p.inventory_at]); // eslint-disable-line react-hooks/exhaustive-deps
  const rows = useMemo(() => p.rows.filter((r) => (group === "all" || r.status === group) && (!q.trim() || r.name.toLowerCase().includes(q.trim().toLowerCase()))), [p.rows, group, q]);
  const selectable = rows.filter((r) => SELECTABLE.includes(r.status));
  const allOn = selectable.length > 0 && selectable.every((r) => picked.includes(r.name));
  const ready = p.rows.filter((r) => r.status === "unused" || r.status === "wasted");
  const keepIt = async (name: string, keep: boolean, reason?: string) => {
    await api(`/api/storage/pipelines/${encodeURIComponent(p.key)}/keep`, { method: "PUT", body: JSON.stringify({ name, keep, reason: reason || null }) });
    toast.success(keep ? `Keeping ${name}` : `${name} can be suggested again`);
    setKeeping(null);
    onChanged();
  };
  const unkeep = (name: string) => keepIt(name, false).catch((e: Error) => toast.error("Couldn't change that", { description: e.message }));
  return (
    <section className="wh-db card-in overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]" style={{ animationDelay: `${index * 60}ms` }}>
      <header className="flex flex-wrap items-start gap-3 border-b border-line px-5 py-4 sm:px-6">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><Workflow className="size-5" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <h2 className="display flex flex-wrap items-center gap-2 text-[1.0625rem] font-semibold text-ink">{p.name}<span className="ai-chip">{p.service}</span></h2>
          <p className="mt-0.5 text-[0.8125rem] text-ink-3">
            {p.factory} · resource group {p.resource_group} · run history seen since {dateOf(p.covered_since)} (Azure keeps {p.retention_days} days; the bot keeps every run it sees)
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.8125rem] text-ink-2" title={priceNote}>runs ≈ <b className="text-ink">{money(p.totals.all.monthly)}</b>/mo</span>
          {ready.length > 0 && <Button size="sm" variant="outline" onClick={() => onScript(ready.map((r) => r.name))}><FileCode2 /> Script {ready.length} ready</Button>}
        </div>
      </header>
      <Filters order={ORDER.pipelines} group={group} setGroup={setGroup} countOf={countOf} costOf={costOf} q={q} setQ={setQ} placeholder="Find a pipeline" />
      {rows.length === 0 ? (
        <div className="px-5 py-10 text-center text-[0.9375rem] text-ink-3 sm:px-6">
          {group === "unused" ? <span className="inline-flex items-center gap-2"><Check className="size-4 text-[var(--good)]" />Every pipeline ran in the last {PERIODS.find((x) => x.value === days)!.label}.</span> : "Nothing here."}
        </div>
      ) : (
        <div className="wh-list mt-2" role="table" aria-label="Pipelines">
          <div className="wh-grid wh-head" role="row">
            <span role="columnheader">{selectable.length > 0 && <input type="checkbox" className="wh-check" checked={allOn} aria-label="Select every one shown" onChange={(e) => onPick(selectable.map((r) => r.name), e.target.checked)} />}</span>
            <span role="columnheader">Pipeline</span>
            <span className="wh-metrics">{["Last run", "Runs (30 days)", "Run time (30 days)", "Cost"].map((h) => <span key={h} role="columnheader">{h}</span>)}</span>
            <span role="columnheader" />
          </div>
          {rows.map((r) => {
            const st = STATUS[r.status];
            const can = SELECTABLE.includes(r.status);
            const isOpen = open === r.name;
            return (
              <div key={r.name} className={cn("wh-item", picked.includes(r.name) && "is-on", isOpen && "is-open")} role="row">
                <div className="wh-grid">
                  <span>{can && <input type="checkbox" className="wh-check" checked={picked.includes(r.name)} onChange={(e) => onPick([r.name], e.target.checked)} aria-label={`Select ${r.name}`} />}</span>
                  <button className="wh-name" onClick={() => setOpen(isOpen ? null : r.name)} aria-expanded={isOpen}>
                    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="wh-dot" style={{ background: st.tone }} aria-hidden />
                      <span className="min-w-0 font-semibold text-ink [overflow-wrap:anywhere]">{r.name}</span>
                      <span className="wh-status" style={{ color: st.tone }}>{st.label}</span>
                      {r.triggers.map((t) => <span key={t.name} className={cn("wh-badge", t.started && "on")} title={`${t.kind} trigger${t.started ? " (on)" : " (stopped)"}`}><Zap className="size-3" />{t.name}</span>)}
                      {!r.triggers.length && !r.called_by.length && <span className="wh-badge">no trigger</span>}
                      {r.confidence === "medium" && r.status === "unused" && <span className="wh-badge">medium confidence</span>}
                    </span>
                    <span className="mt-1 block text-[0.8125rem] leading-5 text-ink-2">{r.why}</span>
                    <ChevronDown className="wh-chev size-4 text-ink-3" aria-hidden />
                  </button>
                  <Metrics items={[
                    ["Last run", r.last_run ? <span key="l">{ago(r.last_run)}{r.last_status && r.last_status !== "Succeeded" ? <span className="text-[var(--critical)]"> · {r.last_status.toLowerCase()}</span> : null}</span> : "not seen"],
                    ["Runs (30 days)", nf.format(r.runs_30d)],
                    ["Run time (30 days)", r.hours_30d ? hours(r.hours_30d) : "–"],
                    ["Cost", <span key="c" title={priceNote}><b className="font-semibold tabular-nums text-ink">{money(r.monthly)}</b><span className="text-ink-3">/mo</span></span>],
                  ]} />
                  <span className="wh-actions">
                    {can && <Button size="sm" variant="ghost" onClick={() => onScript([r.name])}><FileCode2 />Script</Button>}
                    {r.status === "kept"
                      ? <Button size="icon-sm" variant="ghost" onClick={() => void unkeep(r.name)} title="Stop keeping" aria-label={`Stop keeping ${r.name}`}><PinOff /></Button>
                      : can && <Button size="icon-sm" variant="ghost" onClick={() => setKeeping(r)} title="Keep it: never suggest it again" aria-label={`Keep ${r.name}`}><Pin /></Button>}
                  </span>
                </div>
                {isOpen && (
                  <div className="wh-details">
                    {r.description && <p className="text-[0.875rem] text-ink-2">{r.description}</p>}
                    <dl className="wh-facts">
                      {([
                        ["Service", `${r.service}${r.folder ? ` · folder ${r.folder}` : ""}`], ["Steps", nf.format(r.steps)],
                        ["Last run", r.last_run ? `${dateOf(r.last_run)} (${ago(r.last_run)})${r.last_status ? ` · ${r.last_status}` : ""}` : "none the bot has seen"],
                        ["Runs, last 30 days", `${nf.format(r.runs_30d)} · ${r.hours_30d ? hours(r.hours_30d) : "no"} run time`],
                        ["Cost of runs", `≈ ${money(r.monthly)} a month, ${money(r.yearly)} a year`], ["Watched for", span(r.seen_days)],
                      ] as [string, ReactNode][]).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
                    </dl>
                    <div className="grid gap-4 sm:grid-cols-3">
                      <RefList title="Started by" items={[...r.triggers.map((t) => `${t.name}: ${t.kind}${t.every_hours ? `, every ${t.every_hours} h` : ""} (${t.started ? "on" : "stopped"})`), ...r.called_by.map((c) => `pipeline ${c}`)]} empty="nothing: no trigger, no other pipeline" />
                      <RefList title="Calls" items={r.calls.map((c) => `pipeline ${c}`)} empty="no other pipeline" />
                      <RefList title="Loads" items={r.writes.map((w) => `${w.table.split(".").slice(-2).join(".")} (${w.status ? STATUS[w.status as Status]?.label.toLowerCase() ?? w.status : "not scanned"})`)} empty="no table in a database you connected" />
                    </div>
                    {r.shared_triggers.length > 0 && <p className="text-[0.8125rem] text-[var(--warning)]">Trigger {r.shared_triggers.join(", ")} also starts other pipelines: the script leaves it running.</p>}
                    <p className="text-[0.75rem] text-ink-3">{priceNote}</p>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      <KeepModal what={keeping?.name ?? ""} cost={keeping ? `${money(keeping.monthly)} a month in runs` : null} open={keeping != null} onClose={() => setKeeping(null)}
        onKeep={(reason) => keepIt(keeping!.name, true, reason)} />
    </section>
  );
}

// ── small parts ──────────────────────────────────────────────────────────────
function PriceEditor({ db, onChanged }: { db: Db; onChanged: () => void }) {
  const [edit, setEdit] = useState(false);
  const [v, setV] = useState(String(db.price));
  const [busy, setBusy] = useState(false);
  useEffect(() => setV(String(db.price)), [db.price]);
  const save = async (price: number | null) => {
    if (price != null && (!Number.isFinite(price) || price < 0 || price > 1000)) { toast.error("A price between $0 and $1,000 per GB a month"); return; }
    setBusy(true);
    try {
      await api(`/api/storage/${db.id}/price`, { method: "PUT", body: JSON.stringify({ price }) });
      setEdit(false);
      onChanged();
    } catch (e) { toast.error("Couldn't save the price", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  if (!edit) {
    return (
      <button className="wh-price" onClick={() => setEdit(true)} title={`${db.price_note}. Click to use your own price.`}>
        ${db.price.toFixed(3)}<span>/GB-month</span>{db.price_custom ? <span className="ai-chip good">yours</span> : <span className="ai-chip">list</span>}
      </button>
    );
  }
  return (
    <form className="flex items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); void save(Number(v)); }}>
      <span className="text-[0.75rem] text-ink-3">$</span>
      <Input autoFocus value={v} onChange={(e) => setV(e.target.value)} inputMode="decimal" className="h-8 w-20 text-[0.8125rem]" aria-label="Price per GB a month, in dollars" />
      <span className="text-[0.75rem] text-ink-3">/GB-mo</span>
      <Button size="sm" variant="primary" type="submit" disabled={busy}>{busy ? <Loader2 className="animate-spin" /> : <Check />}Save</Button>
      {db.price_custom && <Button size="sm" variant="ghost" type="button" onClick={() => void save(null)} disabled={busy}>List price</Button>}
      <Button size="icon-sm" variant="ghost" type="button" onClick={() => setEdit(false)} aria-label="Cancel"><X /></Button>
    </form>
  );
}

function KeepModal({ what, cost, open, onClose, onKeep }: { what: string; cost: string | null; open: boolean; onClose: () => void; onKeep: (reason: string) => Promise<void> }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setReason(""), [what]);
  const keep = async () => {
    setBusy(true);
    try { await onKeep(reason.trim()); } catch (e) { toast.error("Couldn't keep it", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  return (
    <Modal open={open} onClose={onClose} icon={Pin} title={`Keep ${what}`} subtitle="It stays where it is and is never suggested for removal again."
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" onClick={() => void keep()} disabled={busy}>{busy ? <Loader2 className="animate-spin" /> : <Pin />}Keep it</Button></>}>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-ink-2">Why? (optional, the team sees it)</span>
        <Input value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} placeholder="e.g. yearly audit, legal hold, run by hand at quarter end" onKeyDown={(e) => { if (e.key === "Enter") void keep(); }} />
      </label>
      {cost && <p className="mt-3 text-[0.8125rem] text-ink-3">Keeping it costs {cost}.</p>}
    </Modal>
  );
}

function SelectionBar({ data, picked, onClear, onScript }: { data: Report; picked: Record<string, string[]>; onClear: () => void; onScript: () => void }) {
  let n = 0, mo = 0;
  const parts = new Map<string, number>();
  for (const [t, keys] of Object.entries(picked)) {
    for (const k of keys) {
      if (t.startsWith("db:")) {
        const d = data.databases.find((x) => x.id === Number(t.slice(3)));
        const row: AnyRow | undefined = d?.tables.find((x) => x.key === k) ?? d?.objects.find((x) => x.key === k);
        if (!row) continue;
        n++; mo += row.kind === "table" ? row.monthly : 0;
        parts.set(row.kind, (parts.get(row.kind) ?? 0) + 1);
      } else {
        const r = data.pipelines.projects.find((x) => x.key === t.slice(3))?.rows.find((x) => x.name === k);
        if (!r) continue;
        n++; mo += r.saving;
        parts.set("pipeline", (parts.get("pipeline") ?? 0) + 1);
      }
    }
  }
  if (!n) return null;
  return (
    <div className="wh-selbar" role="region" aria-label="Selected">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent text-[0.8125rem] font-bold text-accent-ink tabular-nums">{n}</span>
      <div className="min-w-0">
        <p className="truncate text-[0.875rem] font-semibold text-ink">{[...parts].map(([k, c]) => plural(c, k)).join(", ")}</p>
        <p className="text-[0.75rem] text-ink-2">{mo ? `saves ${money(mo)} a month, ${money(mo * 12)} a year` : "no running cost: tidying"}</p>
      </div>
      <Button size="sm" variant="ghost" onClick={onClear}>Clear</Button>
      <Button size="sm" variant="primary" onClick={onScript}><FileCode2 />Generate script</Button>
    </div>
  );
}

function ScriptModal({ targets, data, onClose }: { targets: Target[] | null; data: Report; onClose: () => void }) {
  const [which, setWhich] = useState(0);
  const target = targets?.[Math.min(which, (targets?.length ?? 1) - 1)];
  const isPipe = target?.kind === "pl";
  const [mode, setMode] = useState<string>("drop");
  const [out, setOut] = useState<Script | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const db = target?.kind === "db" ? data.databases.find((d) => d.id === target.id) : undefined;
  const nameOf = (t: Target) => (t.kind === "db" ? data.databases.find((d) => d.id === t.id)?.name : data.pipelines.projects.find((p) => p.key === t.id)?.name) ?? String(t.id);
  const validMode = isPipe ? ["delete", "stop"].includes(mode) : ["drop", "archive"].includes(mode);
  const keyOf = target && validMode ? `${target.kind}:${target.id}:${target.keys.join("|")}:${mode}` : "";

  useEffect(() => { if (targets) setWhich(0); }, [targets]);
  useEffect(() => { setMode(isPipe ? "delete" : "drop"); }, [isPipe, target?.id]);
  useEffect(() => {
    if (!target || !keyOf) { setOut(null); return; }
    let live = true;
    setOut(null); setErr(null); setCopied(false);
    const req = target.kind === "db"
      ? post<Script>(`/api/storage/${target.id}/script`, { keys: target.keys, mode })
      : post<Script>(`/api/storage/pipelines/${encodeURIComponent(target.id)}/script?days=${data.days}`, { names: target.keys, mode });
    req.then((r) => { if (live) setOut(r); }).catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [keyOf]); // eslint-disable-line react-hooks/exhaustive-deps

  const copy = async () => {
    if (!out) return;
    try { await navigator.clipboard.writeText(out.sql); setCopied(true); toast.success("Script copied"); setTimeout(() => setCopied(false), 1800); }
    catch { toast.error("Couldn't copy: select the script and copy it by hand"); }
  };
  const download = () => {
    if (!out) return;
    const url = URL.createObjectURL(new Blob([out.sql], { type: isPipe ? "text/plain;charset=utf-8" : "application/sql;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: out.filename });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const modes = isPipe ? [{ value: "delete", label: "Back up and delete" }, { value: "stop", label: "Stop triggers only" }] : [{ value: "drop", label: "Drop" }, { value: "archive", label: "Archive first" }];
  const explain = isPipe
    ? mode === "stop" ? "Saves each definition, then stops the triggers that start only these pipelines. Nothing is deleted." : "Saves each definition to a file, stops and deletes their triggers, then deletes the pipelines. Azure CLI commands."
    : mode === "drop" ? (db?.kind === "snowflake" ? "DROP, with the UNDROP lines to bring tables back during Time Travel." : "DROP inside one transaction: if any fails, nothing changes. Procedures and views go before the tables they use.")
      : "Moves each into an archive schema: nothing is lost yet, but tables still cost until you drop them.";

  return (
    <Modal open={targets != null} onClose={onClose} wide icon={FileCode2}
      title={isPipe ? "Commands to remove these pipelines" : "Script to remove these"}
      subtitle={`AlertBot never runs this. Review it, then run it yourself${isPipe ? " (Azure Cloud Shell, or a terminal after az login)" : " with a login allowed to change the database"}.`}
      footer={<>
        <span className="mr-auto text-[0.8125rem] text-ink-3">{out ? `${plural(out.items, isPipe ? "pipeline" : "object")}${out.tables ? ` · ${size(out.gb)}` : ""}${out.monthly ? ` · saves ${money(out.monthly)}/mo` : ""}` : ""}</span>
        <Button variant="secondary" onClick={download} disabled={!out || !out.items}><Download />Download {isPipe ? ".ps1" : ".sql"}</Button>
        <Button variant="primary" onClick={() => void copy()} disabled={!out || !out.items}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy"}</Button>
      </>}>
      <div className="flex flex-wrap items-center gap-2">
        <Segmented<string> size="sm" label="What it does" value={mode} onChange={setMode} options={modes} />
        {targets && targets.length > 1 && (
          <Segmented<number> size="sm" label="Which" value={Math.min(which, targets.length - 1)} onChange={setWhich} options={targets.map((t, i) => ({ value: i, label: nameOf(t) }))} />
        )}
      </div>
      <p className="mt-2 text-[0.8125rem] text-ink-2">{explain}</p>
      {targets && targets.length > 1 && <p className="mt-1 text-[0.8125rem] text-ink-3">One script per database or factory: {targets.length} chosen, switch between them above.</p>}
      {err ? <Notice tone="warn" className="mt-4">{err}</Notice> : !out ? <Skeleton className="mt-4 h-64 w-full" /> : (
        <>
          {out.warnings.length > 0 && (
            <div className="mt-4 rounded-xl bg-[var(--warning-soft)] px-4 py-3 text-[0.8125rem] text-ink">
              <p className="flex items-center gap-2 font-semibold"><TriangleAlert className="size-4 text-[var(--warning)]" />Review first</p>
              <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-ink-2">{out.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </div>
          )}
          {out.skipped.length > 0 && (
            <div className="mt-3 rounded-xl bg-surface-2 px-4 py-3 text-[0.8125rem] text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">
              <p className="flex items-center gap-2 font-semibold text-ink"><ShieldAlert className="size-4 text-accent" />Left out</p>
              <ul className="mt-1.5 list-disc space-y-0.5 pl-5">{out.skipped.map((w) => <li key={w}>{w}</li>)}</ul>
            </div>
          )}
          {out.items === 0 ? <p className="mt-4 text-[0.875rem] text-ink-2">Nothing left to script: everything chosen was left out.</p>
            : <pre className="wh-code scrollbar-thin mt-4"><code>{out.sql}</code></pre>}
        </>
      )}
    </Modal>
  );
}

function CodeLine({ code }: { code: string }) {
  const [ok, setOk] = useState(false);
  return (
    <div className="relative">
      <pre className="wh-code small scrollbar-thin"><code>{code}</code></pre>
      <button className="absolute right-2 top-2 flex size-7 items-center justify-center rounded-md bg-surface text-ink-3 shadow-[inset_0_0_0_1px_var(--line)] hover:text-ink" aria-label="Copy"
        onClick={() => { void navigator.clipboard.writeText(code).then(() => { setOk(true); setTimeout(() => setOk(false), 1500); }).catch(() => undefined); }}>
        {ok ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
    </div>
  );
}

function HowItWorks({ tab }: { tab: Tab }) {
  const items: Record<Tab, [LucideIcon, string, string][]> = {
    tables: [
      [Eye, "Only on evidence", "SQL Server's usage statistics (or Snowflake's access history) say when each table was last read or written. \"Ready\" means the bot watched the whole period and saw nothing."],
      [RefreshCw, "Survives restarts", "SQL Server forgets its statistics on a restart. The bot scans hourly and keeps what it saw, so a restart can't make a busy table look unused."],
      [Lock, "Pipelines' tables are safe", "Tables any pipeline loads or reads, and tables with data checks, are never suggested, and the script refuses them."],
      [Archive, "You run it, not the bot", "One transaction, dependency warnings, an archive option, and UNDROP for Snowflake."],
    ],
    views: [
      [Layers, "Read through its tables", "Querying a view reads its tables. If none of them were read for the whole period, nobody queried the view."],
      [Snowflake, "Exact on Snowflake", "Snowflake's access history records every view read directly, for a year."],
      [Link2, "What breaks", "Open a view to see what it uses and what uses it. The script warns about anything that would stop working."],
      [Archive, "No storage, less clutter", "Views cost nothing to keep, but every unused one is something to maintain, secure and explain."],
    ],
    code: [
      [Braces, "Execution statistics", "SQL Server records when each procedure and scalar function last ran. It can drop that when memory is short, so these are medium confidence."],
      [Snowflake, "Snowflake CALLs", "Snowflake's query history shows every CALL for a year. Only the procedure's name reaches the bot, never the query text."],
      [Lock, "Pipelines' procedures are safe", "Procedures a Data Factory step runs are never suggested."],
      [Archive, "Callers go first", "The script drops procedures and functions before the views and tables they use, and keeps overloads apart on Snowflake."],
    ],
    pipelines: [
      [Hourglass, "No run, whole period", "Azure keeps 45 days of run history (Logic Apps 90). The bot keeps every run it has seen, so the evidence grows the longer it watches."],
      [Zap, "Runs for nothing", "A pipeline that keeps running but only loads tables nobody reads: its runs cost money for nothing. The cost is estimated from Azure's list prices."],
      [Workflow, "Called pipelines stay", "A pipeline another pipeline in use calls is in use too. Triggers shared with other pipelines are left running."],
      [Archive, "Backed up first", "The commands save each pipeline's definition to a file before anything is stopped or deleted."],
    ],
  };
  return (
    <section className="mt-5 grid gap-4 rounded-[1.5rem] border border-line bg-surface-2/40 p-5 sm:grid-cols-2 sm:p-6 xl:grid-cols-4">
      {items[tab].map(([I, h, p]) => (
        <div key={h} className="min-w-0">
          <p className="flex items-center gap-2 text-[0.875rem] font-semibold text-ink"><I className="size-4 text-accent" aria-hidden />{h}</p>
          <p className="mt-1 text-[0.8125rem] leading-5 text-ink-3">{p}</p>
        </div>
      ))}
    </section>
  );
}
