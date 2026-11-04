// Compute cost: what Snowflake's warehouses (and its serverless features) cost, explained for people who don't live in
// Snowflake: what a credit is, where the money went, who spent it, what's wasted, what to change, and alerts. Colours
// follow the warehouse (fixed order), never its rank; status colours only for status.
import {
  AlertTriangle, ArrowDownRight, ArrowRight, ArrowUpRight, Bell, Bot, Check, ChevronDown, Clock, Copy, Database, Download, ExternalLink, FileCode2,
  Gauge, Hourglass, Info, Layers, Loader2, Moon, PiggyBank, Plus, RefreshCw, Snowflake, Sparkles, Tag, Timer, Users, Wallet, Workflow, X, Zap,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Metric, MetricStrip, Notice } from "@/components/Cards";
import { Modal } from "@/components/Databases";
import { DbPicker } from "@/components/DbPicker";
import { Button } from "@/components/ui/button";
import { Input, Segmented, Skeleton } from "@/components/ui/primitives";
import { api, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import { cn } from "@/lib/utils";

// ── shapes ───────────────────────────────────────────────────────────────────
interface Wh {
  name: string; size: string | null; state: string | null; auto_suspend: number | null; max_clusters: number | null; resource_monitor: string | null;
  credits: number; cost: number; idle_credits: number; idle_cost: number; idle_share: number | null; queries: number | null; p90_ms: number | null;
  queued: number | null; credits_per_hour: number | null; heat: number[][] | null; budget: number | null;
}
interface Named { name: string; credits: number; cost: number; queries: number; service?: boolean; alertbot?: boolean }
interface Shape {
  hash: string; credits: number; cost: number; runs: number; per_run: number | null; last_at: string | null; query_id: string; wh: string | null; user: string | null;
  type: string; database: string | null; schema: string | null; avg_ms: number | null; avg_bytes: number | null; tag: string; link: string | null;
}
interface Rec { id: string; kind: "suspend" | "downsize" | "upsize" | "monitor"; wh: string; credits: number; monthly: number; title: string; why: string; change: string; confidence: string }
interface Alert { key: string; kind: "spike" | "runaway" | "budget"; severity: string; title: string; detail: string; wh: string | null; at: string; delivery: string | null }
interface Account {
  id: number; name: string; account: string; scanned_at: string | null; running_at: string | null; error: string | null; errors: Record<string, string>;
  price: number; price_from: "yours" | "contract" | "list"; price_note: string; storage_tb_price: number;
  settings: { price: number | null; storage_tb: number | null; budget: number | null; warehouse_budgets: Record<string, number>; runaway_minutes: number; spike_factor: number;
    pipeline_users: string[] | null; alerts_on: boolean };
  summary: { yesterday: number; yesterday_credits: number; usual_day: number; yesterday_change: number | null; mtd: number; mtd_credits: number; last_month: number;
    forecast: number; budget: number | null; storage_month: number | null; storage_tb: number | null; bill_forecast: number; idle_cost: number; could_save: number;
    own_cost: number; own_credits: number; own_queries: number };
  chart: ChartDay[];
  /** what comes from the bill and the warehouse meters, for each of the page's periods */
  periods: Record<PeriodKey, { days: number; credits: number; cost: number; prev_cost: number; chart: ChartDay[];
    services: { service: string; name: string; credits: number; cost: number }[]; warehouses: Record<string, { credits: number; cost: number }> }>;
  services: { service: string; name: string; credits: number; cost: number }[];
  warehouses: Wh[]; users: Named[]; roles: Named[]; tags: Named[]; shapes: Shape[];
  pipelines: { rows: { project: string; project_name: string; pipeline: string; credits: number; cost: number; runs: number; per_run_cost: number | null; queries: number; how: string }[];
    unlinked: number; unlinked_cost: number; users: string[]; users_set: boolean; linked_pipelines: number; runs: number };
  recommendations: Rec[]; running: { id: string; user: string | null; wh: string | null; size: string; start: string; seconds: number; link: string | null; long: boolean }[];
  alerts: Alert[];
}
interface Report { accounts: Account[]; generated_at: string; sizes: Record<string, number>; list_price: number }
type Section = "overview" | "warehouses" | "who" | "queries" | "pipelines" | "savings" | "alerts";
type ChartDay = { date: string; credits: number; cost: number; warehouses: Record<string, number>; other: number };
export type PeriodKey = "30" | "90" | "365";
const PERIOD_LABEL: Record<PeriodKey, string> = { "30": "last 30 days", "90": "last 90 days", "365": "last 12 months" };
const PERIOD_SPAN: Record<PeriodKey, string> = { "30": "30 days", "90": "90 days", "365": "12 months" };

// ── numbers & colours ────────────────────────────────────────────────────────
const nf = new Intl.NumberFormat("en-US");
export const usd = (v: number | null | undefined, d?: number) => v == null ? "–" : `$${v.toLocaleString("en-US", { minimumFractionDigits: d ?? (Math.abs(v) < 100 ? 2 : 0), maximumFractionDigits: d ?? (Math.abs(v) < 100 ? 2 : 0) })}`;
export const cr = (v: number) => (v < 10 ? v.toFixed(2) : nf.format(Math.round(v)));
const secs = (ms: number | null) => (ms == null ? "–" : ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60000).toFixed(1)} min`);
const sleeps = (s: number | null) => (s == null || s === 0 ? "never sleeps by itself" : s < 120 ? `sleeps after ${s} s idle` : `sleeps after ${Math.round(s / 60)} min idle`);
export const dayOf = (s: string) => { const [y, m, d] = s.split("-").map(Number); return new Date(y!, m! - 1, d!); };
export const dm = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const ORDER = [1, 7, 3, 2, 5, 4, 6];  // neighbours differ most: blue, purple, teal, orange…
export function useColors(names: string[]) {
  return useMemo(() => { const k = [...new Set(names)].sort(); return (n: string) => { const i = k.indexOf(n); return i >= 0 && i < ORDER.length ? `var(--cat-${ORDER[i]})` : "var(--ink-3)"; }; }, [names.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps
}

function useCountUp(target: number, ms = 900) {
  const [v, setV] = useState(target);
  const from = useRef(0);
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { setV(target); return; }
    const start = performance.now(), a = from.current;
    let raf = 0;
    const step = (t: number) => { const k = Math.min(1, (t - start) / ms), e = 1 - Math.pow(1 - k, 3); setV(a + (target - a) * e); if (k < 1) raf = requestAnimationFrame(step); else from.current = target; };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); from.current = target; };
  }, [target, ms]);
  return v;
}
export const Count = ({ value, fmt }: { value: number; fmt: (n: number) => string }) => <>{fmt(useCountUp(value))}</>;
export type Tip = { x: number; y: number; body: ReactNode } | null;
export const HoverCard = ({ tip }: { tip: Tip }) => tip ? (
  <div className="ai-tip pointer-events-none fixed z-[80] max-w-[17rem] text-[0.75rem]" style={{ left: Math.min(tip.x + 14, window.innerWidth - 280), top: Math.max(8, tip.y - 12), transform: "translateY(-100%)" }}>{tip.body}</div>
) : null;
const store = (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* blocked */ } };
const stored = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };

// ── the page ─────────────────────────────────────────────────────────────────
export function Compute() {
  const { data, error, reload, setData } = useApi<Report>("/api/compute", { refreshMs: 120000 });
  const [which, setWhichRaw] = useState<number | null>(() => Number(stored("alertbot-cc-db")) || null);
  const setWhich = (id: number | null) => { setWhichRaw(id); if (id !== null) store("alertbot-cc-db", String(id)); };
  const [section, setSectionRaw] = useState<Section>(() => (stored("alertbot-cc-section") as Section) || "overview");
  const [pk, setPkRaw] = useState<PeriodKey>(() => (["30", "90", "365"].includes(stored("alertbot-cc-period") ?? "") ? stored("alertbot-cc-period") : "30") as PeriodKey);
  const setPk = (k: PeriodKey) => { setPkRaw(k); store("alertbot-cc-period", k); };
  const [scanning, setScanning] = useState(false);
  const [tip, setTip] = useState<Tip>(null);
  const autoScanned = useRef(false);
  useNow();
  const setSection = (s: Section) => { setSectionRaw(s); store("alertbot-cc-section", s); };
  const scan = async (quiet = false) => {
    setScanning(true);
    const id = quiet ? undefined : toast.loading("Reading Snowflake's usage history…", { description: "Credits, warehouses and who used them. Never a query's text, never your data." });
    try {
      const r = await post<Report>("/api/compute/scan");
      setData(r);
      if (!quiet) {
        const bad = r.accounts.filter((a) => a.error);
        if (bad.length) toast.warning("Couldn't read everything", { id, description: bad.map((a) => `${a.name}: ${a.error}`).join(" · ") });
        else toast.success("Up to date", { id, description: "Snowflake's usage views run up to about 3 hours behind." });
      }
    } catch (e) { if (!quiet) toast.error("Couldn't read Snowflake", { id, description: (e as Error).message }); } finally { setScanning(false); }
  };
  useEffect(() => {
    if (!data || autoScanned.current) return;
    if (data.accounts.length && data.accounts.every((a) => !a.scanned_at)) { autoScanned.current = true; void scan(true); }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error && !data) return <Notice tone="warn">Couldn't load compute cost: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-64 w-full rounded-[1.75rem]" /><Skeleton className="h-28 w-full" /><Skeleton className="h-96 w-full" /></div>;
  if (!data.accounts.length) return <Setup data={data} />;
  const a = data.accounts.find((x) => x.id === which) ?? data.accounts[0]!;
  const replace = (x: Account) => setData({ ...data, accounts: data.accounts.map((y) => (y.id === x.id ? x : y)) });
  const show = (e: React.MouseEvent, body: ReactNode) => setTip({ x: e.clientX, y: e.clientY, body });
  const hide = () => setTip(null);
  const newAlerts = a.alerts.filter((x) => Date.now() - new Date(x.at).getTime() < 48 * 3600e3).length;

  return (
    <div className="cc pb-24" onMouseLeave={hide}>
      <Hero a={a} scan={() => void scan()} scanning={scanning} onChange={replace} goto={setSection} pk={pk} setPk={setPk}
        picker={<DbPicker label="Snowflake database" all={false} value={a.id} onChange={setWhich}
          options={data.accounts.map((x) => ({ id: x.id, name: x.name, kind: "snowflake", note: x.account }))} />} />
      {a.error && <Notice tone="warn" className="mb-4">{a.error} {a.scanned_at ? "The last good reading is shown." : ""}</Notice>}
      {!a.scanned_at ? <div className="flex items-center gap-3 rounded-2xl bg-surface p-6 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]"><Loader2 className="size-5 animate-spin text-accent" />Reading Snowflake's usage history for the first time…</div> : (
        <>
          {/* the answer first (this month is in the hero above): yesterday, idle waste, what could be saved, and the one change worth making */}
          <MetricStrip className="mb-4" cols="grid-cols-1 sm:grid-cols-3">
            <Metric icon={Clock} iconColor="var(--cat-1)" label="Yesterday" value={usd(a.summary.yesterday)} footnote={<Change v={a.summary.yesterday_change} usual={a.summary.usual_day} />} />
            <Metric icon={Moon} iconColor="var(--warning)" label="Paid for idle time" value={usd(a.summary.idle_cost)} footnote="last 30 days: warehouses on, nothing running" onClick={() => setSection("warehouses")} />
            <Metric icon={PiggyBank} iconColor="var(--good)" label="Could save" value={`${usd(a.summary.could_save)}/mo`}
              footnote={`${a.recommendations.filter((r) => r.monthly > 0).length} changes suggested`} onClick={() => setSection("savings")} />
          </MetricStrip>
          {(() => {
            const best = [...a.recommendations].sort((x, y) => y.monthly - x.monthly)[0];
            return best && best.monthly > 0 ? (
              <button type="button" onClick={() => setSection("savings")} className="cc-best mb-5">
                <PiggyBank className="size-5 shrink-0 text-[var(--good)]" aria-hidden />
                <span className="min-w-0 flex-1 text-left">
                  <span className="block text-[0.875rem] font-semibold text-ink">Best saving: {best.title}</span>
                  <span className="block truncate text-[0.75rem] text-ink-2">About {usd(best.monthly)} a month · {best.change}</span>
                </span>
                <span className="shrink-0 text-[0.75rem] font-semibold text-accent">See all changes →</span>
              </button>
            ) : null;
          })()}
          <Sections section={section} setSection={setSection} counts={{ savings: a.recommendations.length, alerts: newAlerts, queries: a.shapes.length, pipelines: a.pipelines.rows.length }} />
          {section === "overview" && <Overview a={a} pk={pk} show={show} hide={hide} />}
          {section === "warehouses" && <Warehouses a={a} pk={pk} show={show} hide={hide} onChange={replace} />}
          {section === "who" && <Who a={a} />}
          {section === "queries" && <Queries a={a} />}
          {section === "pipelines" && <Pipelines a={a} onChange={replace} />}
          {section === "savings" && <Savings a={a} />}
          {section === "alerts" && <Alerts a={a} onChange={replace} />}
          <div className="mt-5"><Explainer price={a.price} sizes={data.sizes} open={false} /></div>
          <p className="mt-2 text-center text-[0.75rem] text-ink-3">
            AlertBot's own checks cost {usd(a.summary.own_cost)} ({nf.format(a.summary.own_queries)} queries) over the last 30 days ·{" "}
            From Snowflake's ACCOUNT_USAGE views (they run up to about 3 hours behind; what's running now: every 10 minutes) · read {a.scanned_at ? ago(a.scanned_at) : "never"} ·
            totals, names and ids only: never a query's text, never your data
          </p>
        </>
      )}
      <HoverCard tip={tip} />
    </div>
  );
}

// ── no account yet: what to do, step by step ─────────────────────────────────
function Setup({ data }: { data: Report }) {
  return (
    <div className="space-y-5">
      <section className="ai-hero cc-hero card-in relative overflow-hidden rounded-[1.75rem] p-6 sm:p-8">
        <div className="ai-hero-glow cc-glow" aria-hidden />
        <p className="eyebrow relative flex items-center gap-2 font-semibold text-accent"><Snowflake className="size-3.5" /> Compute cost</p>
        <h1 className="display relative mt-2 text-[2rem] font-bold leading-tight text-ink sm:text-[2.5rem]">See what Snowflake compute costs you, and where it goes</h1>
        <p className="relative mt-2 max-w-3xl text-[0.9375rem] leading-6 text-ink-2">Spend by day and warehouse, who and which pipelines use it, money spent on idle warehouses, changes that save money (with the script), and alerts when spend jumps. Three steps:</p>
        <ol className="relative mt-5 grid gap-3 lg:grid-cols-3">
          <SetupStep n={1} title="Connect your Snowflake account" done={false}>
            On the Databases page, add your Snowflake account (the account identifier, a login, its role and a warehouse).
            <Button asChild variant="primary" size="sm" className="mt-3"><Link to="/databases">Connect Snowflake <ArrowRight /></Link></Button>
          </SetupStep>
          <SetupStep n={2} title="Let its role read usage" done={false}>
            You said this is in place. If not, an admin runs:
            <code className="cc-code mt-2 block">GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE &lt;its role&gt;;</code>
            <span className="mt-1.5 block text-[0.75rem] text-ink-3">Read-only: usage history and settings, never your data.</span>
          </SetupStep>
          <SetupStep n={3} title="Your price per credit (optional)" done={false}>
            Not sure? No problem: the bot starts at Snowflake's list price of <b>${data.list_price.toFixed(2)}</b> (Enterprise), and uses your contract's real rate by itself if Snowflake lets the login read it. <PriceHelp />
          </SetupStep>
        </ol>
      </section>
      <Explainer price={data.list_price} sizes={data.sizes} open />
    </div>
  );
}
const SetupStep = ({ n, title, children, done }: { n: number; title: string; children: ReactNode; done: boolean }) => (
  <li className="cc-step"><span className={cn("cc-step-n", done && "done")}>{done ? <Check className="size-4" /> : n}</span><div className="min-w-0"><p className="font-semibold text-ink">{title}</p><div className="mt-1 text-[0.8125rem] leading-5 text-ink-2">{children}</div></div></li>
);
function PriceHelp() {
  const [open, setOpen] = useState(false);
  return (
    <span className="block">
      <button className="mt-2 inline-flex items-center gap-1 text-[0.8125rem] font-medium text-accent hover:underline" onClick={() => setOpen((o) => !o)}>
        <Info className="size-3.5" />How do I find my price?<ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <span className="mt-1.5 block rounded-lg bg-surface-2 p-2.5 text-[0.75rem] leading-5 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">
          In Snowsight: <b>Admin → Cost Management → Consumption</b> shows spend in your currency; divide it by the credits used. Or ask whoever pays the
          Snowflake invoice for the "price per credit" on your contract. List prices: Standard $2, Enterprise $3, Business Critical $4 (US regions; others cost a little more).
        </span>
      )}
    </span>
  );
}

// ── hero ─────────────────────────────────────────────────────────────────────
function Hero({ a, scan, scanning, onChange, goto, picker, pk, setPk }: {
  a: Account; scan: () => void; scanning: boolean; onChange: (a: Account) => void; goto: (s: Section) => void; picker: ReactNode;
  pk: PeriodKey; setPk: (k: PeriodKey) => void;
}) {
  const s = a.summary;
  const p = a.periods?.[pk];
  const change = p && p.prev_cost > 0 ? p.cost / p.prev_cost - 1 : null;
  const end = new Date(); end.setMonth(end.getMonth() + 1, 0);
  const vsLast = s.last_month ? s.forecast / s.last_month - 1 : null;
  const pct = s.budget ? s.mtd / s.budget : null;
  const fpct = s.budget ? s.forecast / s.budget : null;
  return (
    <section className="ai-hero cc-hero card-in relative mb-4 overflow-hidden rounded-[1.75rem] p-6 sm:p-8">
      <div className="ai-hero-glow cc-glow" aria-hidden />
      <div className="relative flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <p className="eyebrow flex items-center gap-2 font-semibold text-accent"><Snowflake className="size-3.5" /> Compute cost</p>
          {picker}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<PeriodKey> size="sm" label="Period" value={pk} onChange={setPk}
            options={[{ value: "30", label: "30 days" }, { value: "90", label: "90 days" }, { value: "365", label: "12 months" }]} />
          <PriceChip a={a} onChange={onChange} />
          <Button size="sm" variant="secondary" onClick={scan} disabled={scanning}>{scanning ? <Loader2 className="animate-spin" /> : <RefreshCw />}{scanning ? "Reading…" : "Refresh"}</Button>
        </div>
      </div>
      <div className="relative mt-4 grid gap-6 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] lg:items-end">
        <div className="min-w-0">
          <p className="text-[0.9375rem] font-medium text-ink-2">This month so far</p>
          <h1 className="display mt-1 flex flex-wrap items-baseline gap-x-3 text-[2.75rem] font-bold leading-[1.02] tracking-[-0.03em] text-ink sm:text-[3.5rem]">
            <span className="tabular-nums"><Count value={s.mtd} fmt={(v) => usd(v, 0)} /></span>
            <span className="text-[1.0625rem] font-semibold tracking-normal text-ink-2">{cr(s.mtd_credits)} credits</span>
          </h1>
          <p className="mt-2 text-[0.9375rem] leading-6 text-ink-2">
            At this pace, about <b className="font-semibold text-ink">{usd(s.forecast, 0)}</b> by {dm.format(end)}
            {vsLast != null && s.last_month > 0 && <> · <span className={vsLast > 0.05 ? "text-[var(--critical)]" : vsLast < -0.05 ? "text-[var(--good)]" : ""}>{vsLast >= 0 ? "+" : ""}{(vsLast * 100).toFixed(0)}% on last month ({usd(s.last_month, 0)})</span></>}
          </p>
          {p && (
            <p className="mt-1 text-[0.9375rem] leading-6 text-ink-2">
              The {PERIOD_LABEL[pk]}: <b className="font-semibold text-ink">{usd(p.cost)}</b> <span className="text-ink-3">({cr(p.credits)} credits)</span>
              {change != null ? <> · <span className={change > 0.05 ? "text-[var(--critical)]" : change < -0.05 ? "text-[var(--good)]" : ""}>{change >= 0 ? "+" : ""}{(change * 100).toFixed(0)}%</span> on the {PERIOD_SPAN[pk]} before ({usd(p.prev_cost)})</>
                : p.cost > 0 ? <> · nothing billed in the {PERIOD_SPAN[pk]} before</> : null}
            </p>
          )}
          {s.budget ? (
            <div className="mt-4 max-w-xl">
              <div className="flex justify-between text-[0.8125rem]"><span className="text-ink-2">Monthly budget {usd(s.budget, 0)}</span><span className={cn("font-semibold", (pct ?? 0) >= 1 ? "text-[var(--critical)]" : (pct ?? 0) >= 0.8 ? "text-[var(--warning)]" : "text-ink")}>{((pct ?? 0) * 100).toFixed(0)}% used</span></div>
              <div className="cc-budget mt-1.5" role="img" aria-label={`${((pct ?? 0) * 100).toFixed(0)}% of the budget used; heading for ${((fpct ?? 0) * 100).toFixed(0)}%`}>
                <span style={{ width: `${Math.min(100, (fpct ?? 0) * 100)}%` }} className="cc-budget-fc" />
                <span style={{ width: `${Math.min(100, (pct ?? 0) * 100)}%`, background: (pct ?? 0) >= 1 ? "var(--critical)" : (pct ?? 0) >= 0.8 ? "var(--warning)" : "var(--good)" }} className="ai-grow-x" />
              </div>
              <p className="mt-1 text-[0.75rem] text-ink-3">The lighter bar is where the month is heading{(fpct ?? 0) > 1 ? <b className="text-[var(--critical)]">: over budget</b> : ""}.</p>
            </div>
          ) : (
            <button className="mt-4 inline-flex items-center gap-1.5 text-[0.875rem] font-medium text-accent hover:underline" onClick={() => goto("alerts")}><Bell className="size-4" />Set a monthly budget and get warned at 80%</button>
          )}
        </div>
        <div className="ai-whatif min-w-0 rounded-2xl p-4 sm:p-5">
          <p className="text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">Your Snowflake bill, this month</p>
          <div className="mt-2 flex items-baseline gap-2"><span className="display text-[2rem] font-bold leading-none tabular-nums text-ink">{usd(s.bill_forecast, 0)}</span><span className="text-sm text-ink-2">expected</span></div>
          <div className="mt-3 space-y-1.5 text-[0.8125rem]">
            <BillLine color="var(--cat-1)" label="Compute (warehouses and more)" value={usd(s.forecast, 0)} />
            <BillLine color="var(--cat-3)" label={`Storage${s.storage_tb != null ? ` · ${s.storage_tb.toFixed(2)} TB` : ""}`} value={s.storage_month != null ? usd(s.storage_month, 0) : "–"} />
          </div>
          <p className="mt-3 text-[0.71875rem] leading-4 text-ink-3">Storage at ${a.storage_tb_price}/TB a month. Data transfer isn't included.</p>
        </div>
      </div>
    </section>
  );
}
const BillLine = ({ color, label, value }: { color: string; label: string; value: string }) => (
  <p className="flex items-center justify-between gap-3"><span className="flex items-center gap-2 text-ink-2"><span className="size-2.5 rounded-[3px]" style={{ background: color }} />{label}</span><b className="tabular-nums text-ink">{value}</b></p>
);

function PriceChip({ a, onChange }: { a: Account; onChange: (a: Account) => void }) {
  const [open, setOpen] = useState(false);
  const [v, setV] = useState(String(a.price));
  const [busy, setBusy] = useState(false);
  useEffect(() => setV(String(a.price)), [a.price]);
  const save = async (price: number | null) => {
    if (price != null && (!Number.isFinite(price) || price <= 0 || price > 100)) { toast.error("A price between $0 and $100 per credit"); return; }
    setBusy(true);
    try { onChange(await api<Account>(`/api/compute/${a.id}/settings`, { method: "PUT", body: JSON.stringify({ price }) })); setOpen(false); toast.success("Price saved: every dollar figure uses it now"); }
    catch (e) { toast.error("Couldn't save the price", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  return (
    <div className="relative">
      <button className="cc-price" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        1 credit = <b>${a.price.toFixed(2)}</b><span className={cn("ai-chip", a.price_from !== "list" && "good")}>{a.price_from === "yours" ? "yours" : a.price_from === "contract" ? "contract" : "list price"}</span><ChevronDown className="size-3.5" />
      </button>
      {open && (
        <div className="cc-pop" role="dialog" aria-label="Price per credit">
          <p className="text-[0.8125rem] font-semibold text-ink">What you pay per credit</p>
          <p className="mt-0.5 text-[0.75rem] leading-[1.1rem] text-ink-3">Now: {a.price_note}.</p>
          <div className="mt-2.5 flex items-center gap-1.5">
            <span className="text-ink-3">$</span><Input autoFocus className="h-9 w-24" inputMode="decimal" value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void save(Number(v)); }} aria-label="Dollars per credit" />
            <Button size="sm" variant="primary" onClick={() => void save(Number(v))} disabled={busy}>{busy ? <Loader2 className="animate-spin" /> : <Check />}Save</Button>
            {a.price_from === "yours" && <Button size="sm" variant="ghost" onClick={() => void save(null)} disabled={busy}>Undo</Button>}
          </div>
          <PriceHelp />
        </div>
      )}
    </div>
  );
}

function Change({ v, usual }: { v: number | null; usual: number }) {
  if (v == null) return <>learning what's usual</>;
  const up = v > 0.05, down = v < -0.05;
  const I = up ? ArrowUpRight : down ? ArrowDownRight : null;
  return <span className={cn("inline-flex items-center gap-0.5", up ? "text-[var(--critical)]" : down ? "text-[var(--good)]" : "")}>{I && <I className="size-3.5" />}{up || down ? `${Math.abs(v * 100).toFixed(0)}% ${up ? "more" : "less"} than` : "same as"} a usual day ({usd(usual)})</span>;
}

// ── how it works, in one card ────────────────────────────────────────────────
function Explainer({ price, sizes, open: initial }: { price: number; sizes: Record<string, number>; open?: boolean }) {
  const [open, setOpen] = useState(() => initial ?? stored("alertbot-cc-explainer") !== "closed");
  const toggle = () => { setOpen((o) => { store("alertbot-cc-explainer", o ? "closed" : "open"); return !o; }); };
  return (
    <section className="mb-5 overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]">
      <button className="flex w-full items-center gap-3 px-5 py-3.5 text-left sm:px-6" onClick={toggle} aria-expanded={open}>
        <span className="flex size-8 items-center justify-center rounded-xl bg-accent-soft text-accent"><Info className="size-4" /></span>
        <span className="min-w-0 flex-1"><b className="text-[0.9375rem] text-ink">How Snowflake charges for compute</b><span className="block text-[0.75rem] text-ink-3">In 30 seconds: credits, warehouse sizes, and why idle time costs money</span></span>
        <ChevronDown className={cn("size-5 text-ink-3 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="grid gap-4 border-t border-line p-5 sm:p-6 lg:grid-cols-3">
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-[0.875rem] font-semibold text-ink"><Gauge className="size-4 text-accent" />You pay by the second while a warehouse is on</p>
            <p className="mt-1 text-[0.8125rem] leading-5 text-ink-2">A warehouse is the computer that runs queries. Bigger ones are faster and cost more: each size up doubles the credits per hour.</p>
            <div className="cc-ladder mt-3">
              {Object.entries(sizes).slice(0, 7).map(([s, c]) => (
                <div key={s} className="cc-rung"><span className="w-16 shrink-0 text-ink-2">{s}</span><span className="cc-rung-bar"><span style={{ width: `${Math.max(3, (Math.log2(c) + 1) / 7 * 100)}%` }} /></span>
                  <span className="w-28 shrink-0 text-right tabular-nums text-ink-2">{c} cr/h · <b className="text-ink">{usd(c * price, 0)}</b>/h</span></div>
              ))}
            </div>
          </div>
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-[0.875rem] font-semibold text-ink"><Moon className="size-4 text-[var(--warning)]" />Idle time costs the same</p>
            <p className="mt-1 text-[0.8125rem] leading-5 text-ink-2">After its last query, a warehouse stays on until its <b>auto-suspend</b> time runs out (often 10 minutes) and bills all of it. Many short query bursts → a lot of paid idle time. Setting auto-suspend to 60 seconds usually fixes it.</p>
            <div className="cc-idle mt-3" aria-hidden><span className="busy">query</span><span className="idle">idle, still billed</span><span className="busy">query</span><span className="idle">idle, still billed</span><span className="off">asleep: free</span></div>
          </div>
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-[0.875rem] font-semibold text-ink"><Zap className="size-4 text-[var(--cat-2)]" />Some things run without a warehouse</p>
            <p className="mt-1 text-[0.8125rem] leading-5 text-ink-2">Scheduled tasks, Snowpipe loading, automatic clustering and materialized views use "serverless" credits. They're easy to forget and show up under <b>Other</b> in the chart.</p>
            <p className="mt-2 text-[0.8125rem] leading-5 text-ink-2">A credit costs <b>{usd(price)}</b> here. Every dollar on this page is credits × that price.</p>
          </div>
        </div>
      )}
    </section>
  );
}

function Sections({ section, setSection, counts }: { section: Section; setSection: (s: Section) => void; counts: Record<string, number> }) {
  const items: [Section, string, LucideIcon][] = [["overview", "Overview", Layers], ["warehouses", "Warehouses", Database], ["who", "Who's spending", Users],
    ["queries", "Expensive queries", Timer], ["pipelines", "Pipelines", Workflow], ["savings", "Savings", PiggyBank], ["alerts", "Alerts & budget", Bell]];
  return (
    <nav className="cc-tabs mb-4" aria-label="Sections">
      <div className="cc-tabs-inner" role="tablist">
        {items.map(([id, label, I]) => (
          <button key={id} role="tab" aria-selected={section === id} onClick={() => setSection(id)} className={cn("cc-tab", section === id && "is-on")}>
            <I className="size-4 shrink-0" />{label}{counts[id] ? <span className={cn("cc-badge", id === "alerts" && "warn")}>{counts[id]}</span> : null}
          </button>
        ))}
      </div>
    </nav>
  );
}

export const Card = ({ title, hint, icon: I, children, actions }: { title: string; hint?: ReactNode; icon: LucideIcon; children: ReactNode; actions?: ReactNode }) => (
  <section className="card-in mb-5 overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]">
    <header className="flex flex-wrap items-start gap-3 border-b border-line px-5 py-4 sm:px-6">
      <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><I className="size-[1.125rem]" /></span>
      <div className="min-w-0 flex-1"><h2 className="display text-[1rem] font-semibold text-ink">{title}</h2>{hint && <p className="text-[0.8125rem] leading-5 text-ink-3">{hint}</p>}</div>
      {actions}
    </header>
    {children}
  </section>
);

// ── overview: the month, day by day ──────────────────────────────────────────
function Overview({ a, pk, show, hide }: { a: Account; pk: PeriodKey; show: (e: React.MouseEvent, b: ReactNode) => void; hide: () => void }) {
  const period = a.periods?.[pk];
  const days = period?.chart ?? a.chart;
  // 12 months: one bar a week, so the bars stay readable
  const chart = useMemo<(ChartDay & { label: string })[]>(() => {
    if (pk !== "365") return days.map((d) => ({ ...d, label: dm.format(dayOf(d.date)) }));
    const weeks: (ChartDay & { label: string })[] = [];
    for (let i = 0; i < days.length; i += 7) {
      const chunk = days.slice(i, i + 7);
      const wh: Record<string, number> = {};
      for (const d of chunk) for (const [w, v] of Object.entries(d.warehouses)) wh[w] = (wh[w] ?? 0) + v;
      weeks.push({ date: chunk[0]!.date, label: `${dm.format(dayOf(chunk[0]!.date))} – ${dm.format(dayOf(chunk[chunk.length - 1]!.date))}`, warehouses: wh,
        credits: chunk.reduce((x, y) => x + y.credits, 0), cost: chunk.reduce((x, y) => x + y.cost, 0), other: chunk.reduce((x, y) => x + y.other, 0) });
    }
    return weeks;
  }, [days, pk]);
  const services = period?.services ?? a.services;
  const top = useMemo(() => {
    const tot: Record<string, number> = {};
    for (const d of chart) for (const [w, v] of Object.entries(d.warehouses)) tot[w] = (tot[w] ?? 0) + v;
    return Object.entries(tot).sort((x, y) => y[1] - x[1]).slice(0, 6).map(([w]) => w);
  }, [chart]);
  const color = useColors(a.warehouses.map((w) => w.name));
  const series = [...top, "Other warehouses", "Serverless & other"];
  const colorOf = (s: string) => (s === "Other warehouses" ? "var(--ink-3)" : s === "Serverless & other" ? "color-mix(in srgb, var(--ink-3) 45%, var(--surface-3))" : color(s));
  const rows = chart.map((d) => {
    const parts: Record<string, number> = {};
    let rest = 0;
    for (const [w, v] of Object.entries(d.warehouses)) { if (top.includes(w)) parts[w] = v; else rest += v; }
    parts["Other warehouses"] = rest;
    parts["Serverless & other"] = Math.max(0, d.other);
    return { ...d, parts };
  });
  const max = Math.max(1, ...rows.map((r) => Object.values(r.parts).reduce((x, y) => x + y, 0)));
  const avg = pk === "365" ? null : a.summary.usual_day;
  return (
    <>
      <Card icon={Layers} title={pk === "365" ? "Spend, week by week" : "Spend, day by day"}
        hint={`The ${PERIOD_LABEL[pk]} in dollars${pk === "365" ? ", one bar a week" : ""}, split by warehouse: ${usd(period?.cost ?? 0)} in all. Today is still filling in.`}>
        <div className="p-5 sm:p-6">
          <div className="cc-chart" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0, 1fr))` }}>
            {rows.map((r) => {
              const total = Object.values(r.parts).reduce((x, y) => x + y, 0);
              return (
                <div key={r.date} className="cc-col" onMouseMove={(e) => show(e, <>
                  <p className="font-semibold text-ink">{r.label} · {usd(total)}</p>
                  {series.filter((s) => r.parts[s]).map((s) => <p key={s} className="flex justify-between gap-4"><span className="flex items-center gap-1.5 text-ink-2"><span className="size-2 rounded-[2px]" style={{ background: colorOf(s) }} />{s}</span><b className="text-ink">{usd(r.parts[s])}</b></p>)}
                </>)} onMouseLeave={hide}>
                  <span className="cc-stack" style={{ height: `${(total / max) * 100}%` }}>
                    {series.map((s) => r.parts[s] ? <span key={s} className="ai-grow" style={{ flexGrow: r.parts[s], background: colorOf(s) }} /> : null)}
                  </span>
                </div>
              );
            })}
            {avg ? <i className="cc-avg" style={{ bottom: `${(avg / max) * 100}%` }} title={`a usual day: ${usd(avg)}`}><span>usual day {usd(avg)}</span></i> : null}
          </div>
          <div className="mt-1 flex justify-between text-[0.6875rem] text-ink-3"><span>{rows[0]?.label.split(" – ")[0]}</span><span>today</span></div>
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[0.75rem] text-ink-2">
            {series.map((s) => <span key={s} className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-[3px]" style={{ background: colorOf(s) }} />{s}</span>)}
          </div>
        </div>
      </Card>
      <Card icon={Zap} title="What the credits went on" hint={`The ${PERIOD_LABEL[pk]}, by kind of work. Warehouses run queries; the rest is Snowflake working on its own.`}>
        <ul className="divide-y divide-[var(--line)]">
          {services.map((s) => {
            const total = services.reduce((x, y) => x + y.cost, 0) || 1;
            return (
              <li key={s.service} className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6">
                <span className="min-w-0 flex-1"><b className="text-[0.875rem] text-ink">{s.name}</b><span className="block text-[0.75rem] text-ink-3">{SERVICE_HELP[s.service] ?? "Snowflake's own background work."}</span></span>
                <span className="cc-share w-32"><span style={{ width: `${(s.cost / total) * 100}%` }} /></span>
                <span className="w-24 text-right tabular-nums"><b className="text-ink">{usd(s.cost)}</b><span className="block text-[0.6875rem] text-ink-3">{cr(s.credits)} cr</span></span>
              </li>
            );
          })}
        </ul>
      </Card>
    </>
  );
}
const SERVICE_HELP: Record<string, string> = {
  WAREHOUSE_METERING: "Queries, loads and anything else run on your warehouses, including their idle time.", SERVERLESS_TASK: "Scheduled tasks that run on Snowflake's own compute.",
  PIPE: "Snowpipe loading files as they arrive.", AUTO_CLUSTERING: "Snowflake re-sorting clustered tables in the background.", MATERIALIZED_VIEW: "Keeping materialized views up to date.",
  SEARCH_OPTIMIZATION: "Keeping search optimization indexes up to date.", CLOUD_SERVICES: "Logins, query planning and metadata: only billed above 10% of the day's warehouse use.",
  AI_SERVICES: "Cortex AI functions.", QUERY_ACCELERATION: "Extra compute that speeds up large scans.", REPLICATION: "Copying databases to other regions or accounts.",
};

// ── warehouses ───────────────────────────────────────────────────────────────
function Warehouses({ a, pk, show, hide, onChange }: { a: Account; pk: PeriodKey; show: (e: React.MouseEvent, b: ReactNode) => void; hide: () => void; onChange: (a: Account) => void }) {
  const color = useColors(a.warehouses.map((w) => w.name));
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {a.warehouses.map((w) => {
        const busy = w.idle_share != null ? 1 - w.idle_share : null;
        return (
          <section key={w.name} className="card-in overflow-hidden rounded-[1.5rem] border border-line bg-surface p-5 shadow-[var(--elev-1)]">
            <div className="flex flex-wrap items-start gap-3">
              <span className="mt-1 size-3 shrink-0 rounded-[3px]" style={{ background: color(w.name) }} />
              <div className="min-w-0 flex-1">
                <h3 className="font-mono text-[1rem] font-semibold text-ink [overflow-wrap:anywhere]">{w.name}</h3>
                <p className="mt-0.5 flex flex-wrap gap-1.5 text-[0.75rem]">
                  {w.size && <span className="ai-chip">{w.size}{w.credits_per_hour ? ` · ${w.credits_per_hour} cr/h ≈ ${usd(w.credits_per_hour * a.price, 0)}/h` : ""}</span>}
                  <span className={cn("ai-chip", (w.auto_suspend ?? 0) > 120 || !w.auto_suspend ? "warn" : "good")}>{sleeps(w.auto_suspend)}</span>
                  {w.resource_monitor ? <span className="ai-chip good">capped: {w.resource_monitor}</span> : <span className="ai-chip">no spending cap</span>}
                  {(w.max_clusters ?? 1) > 1 && <span className="ai-chip">up to {w.max_clusters} clusters</span>}
                </p>
              </div>
              <div className="text-right"><p className="display text-[1.375rem] font-bold tabular-nums text-ink">{usd(a.periods?.[pk]?.warehouses[w.name]?.cost ?? w.cost, 0)}</p><p className="text-[0.71875rem] text-ink-3">{PERIOD_SPAN[pk]} · {cr(a.periods?.[pk]?.warehouses[w.name]?.credits ?? w.credits)} cr</p></div>
            </div>
            {busy != null && (
              <div className="mt-4">
                <div className="flex justify-between text-[0.75rem]"><span className="text-ink-2">Working <b className="text-ink">{(busy * 100).toFixed(0)}%</b></span><span className="text-ink-2">Idle but billed (last 30 days) <b className={w.idle_share! > 0.4 ? "text-[var(--warning)]" : "text-ink"}>{(w.idle_share! * 100).toFixed(0)}% · {usd(w.idle_cost, 0)}</b></span></div>
                <div className="cc-util mt-1"><span style={{ width: `${busy * 100}%`, background: color(w.name) }} /><span style={{ flex: 1 }} className="cc-util-idle" /></div>
              </div>
            )}
            <dl className="mt-4 grid grid-cols-3 gap-3 text-[0.8125rem]">
              <div><dt className="text-[0.6875rem] uppercase tracking-wide text-ink-3">Queries (14 d)</dt><dd className="font-semibold tabular-nums text-ink">{w.queries != null ? nf.format(w.queries) : "–"}</dd></div>
              <div><dt className="text-[0.6875rem] uppercase tracking-wide text-ink-3">9 in 10 finish in</dt><dd className="font-semibold tabular-nums text-ink">{secs(w.p90_ms)}</dd></div>
              <div><dt className="text-[0.6875rem] uppercase tracking-wide text-ink-3">Had to queue</dt><dd className={cn("font-semibold tabular-nums", (w.queued ?? 0) > 0 ? "text-[var(--warning)]" : "text-ink")}>{w.queued != null ? nf.format(w.queued) : "–"}</dd></div>
            </dl>
            {w.heat && <Heat heat={w.heat} color={color(w.name)} price={a.price} name={w.name} show={show} hide={hide} />}
            <WhBudget a={a} w={w} onChange={onChange} />
          </section>
        );
      })}
    </div>
  );
}

const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function Heat({ heat, color, price, name, show, hide }: { heat: number[][]; color: string; price: number; name: string; show: (e: React.MouseEvent, b: ReactNode) => void; hide: () => void }) {
  const max = Math.max(0.0001, ...heat.flat());
  return (
    <div className="mt-4">
      <p className="mb-1.5 text-[0.6875rem] font-semibold uppercase tracking-wide text-ink-3">When it ran, last 7 days (UTC)</p>
      <div className="cc-heat">
        <span />{Array.from({ length: 24 }, (_, h) => <span key={h} className="cc-hh">{h % 6 === 0 ? h : ""}</span>)}
        {heat.map((row, d) => (
          <div key={d} className="contents"><span className="cc-hd">{WEEK[d]}</span>
            {row.map((v, h) => <span key={h} className="cc-hc" style={{ background: v ? `color-mix(in srgb, ${color} ${Math.round(15 + (v / max) * 80)}%, var(--surface-2))` : undefined }}
              onMouseMove={(e) => show(e, <><p className="font-semibold text-ink">{name} · {WEEK[d]} {String(h).padStart(2, "0")}:00</p><p className="text-ink-2">{v ? `${cr(v)} credits · ${usd(v * price)}` : "asleep"}</p></>)} onMouseLeave={hide} />)}
          </div>
        ))}
      </div>
    </div>
  );
}

function WhBudget({ a, w, onChange }: { a: Account; w: Wh; onChange: (a: Account) => void }) {
  const [v, setV] = useState(w.budget ? String(w.budget) : "");
  const [edit, setEdit] = useState(false);
  const save = async () => {
    const n = v.trim() === "" ? 0 : Number(v);
    if (!Number.isFinite(n) || n < 0) { toast.error("A budget in dollars"); return; }
    try {
      onChange(await api<Account>(`/api/compute/${a.id}/settings`, { method: "PUT", body: JSON.stringify({ warehouse_budgets: { ...a.settings.warehouse_budgets, [w.name]: n } }) }));
      setEdit(false); toast.success(n ? `${w.name}: warned at 80% and 100% of ${usd(n, 0)} a month` : `${w.name}: no budget`);
    } catch (e) { toast.error("Couldn't save", { description: (e as Error).message }); }
  };
  return edit ? (
    <div className="mt-4 flex flex-wrap items-center gap-2 text-[0.8125rem]"><span className="text-ink-2">Monthly budget $</span><Input autoFocus className="h-8 w-24" inputMode="decimal" value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void save(); }} />
      <Button size="sm" variant="primary" onClick={() => void save()}><Check />Save</Button><Button size="sm" variant="ghost" onClick={() => setEdit(false)}><X /></Button></div>
  ) : (
    <button className="mt-4 inline-flex items-center gap-1.5 text-[0.8125rem] text-accent hover:underline" onClick={() => setEdit(true)}><Bell className="size-3.5" />{w.budget ? `Budget ${usd(w.budget, 0)}/month: change` : "Set a monthly budget for this warehouse"}</button>
  );
}

// ── who's spending ───────────────────────────────────────────────────────────
function Who({ a }: { a: Account }) {
  return (
    <>
      {a.summary.own_cost > 0 && (
        <Notice className="mb-4"><Bot className="mr-1 inline size-4 text-accent" />AlertBot's own checks cost <b>{usd(a.summary.own_cost)}</b> in the last 30 days ({nf.format(a.summary.own_queries)} queries, tagged "alertbot-…" so you can always see them).</Notice>
      )}
      <div className="grid gap-4 xl:grid-cols-3">
        <Ranked icon={Users} title="People and logins" hint="Credits their queries used (idle time isn't anyone's)." rows={a.users} badge={(r) => r.service ? "pipeline login" : null} />
        <Ranked icon={Layers} title="Roles" hint="The role each query ran as." rows={a.roles} />
        <Ranked icon={Tag} title="Query tags" hint="Labels apps and tools put on their queries." rows={a.tags} badge={(r) => r.alertbot ? "that's AlertBot" : null} />
      </div>
      {a.errors.roles && <p className="mt-2 text-[0.75rem] text-ink-3">Roles couldn't be read: {a.errors.roles}</p>}
    </>
  );
}
function Ranked({ icon: I, title, hint, rows, badge }: { icon: LucideIcon; title: string; hint: string; rows: Named[]; badge?: (r: Named) => string | null }) {
  const [all, setAll] = useState(false);
  const max = Math.max(0.0001, ...rows.map((r) => r.cost));
  const shown = all ? rows : rows.slice(0, 8);
  return (
    <section className="card-in overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]">
      <header className="flex items-start gap-2.5 border-b border-line px-5 py-3.5"><I className="mt-0.5 size-4 text-accent" /><div><h3 className="text-[0.9375rem] font-semibold text-ink">{title}</h3><p className="text-[0.75rem] text-ink-3">{hint} Last 30 days.</p></div></header>
      {rows.length === 0 ? <p className="px-5 py-6 text-[0.875rem] text-ink-3">Nothing yet.</p> : (
        <ul className="space-y-2.5 p-5">
          {shown.map((r) => (
            <li key={r.name}>
              <div className="flex items-baseline justify-between gap-2 text-[0.8125rem]">
                <span className="min-w-0 truncate font-medium text-ink" title={r.name}>{r.name}{badge?.(r) && <span className="ai-chip ml-1.5">{badge(r)}</span>}</span>
                <b className="shrink-0 tabular-nums text-ink">{usd(r.cost)}</b>
              </div>
              <div className="mt-1 flex items-center gap-2"><span className="cc-share flex-1"><span style={{ width: `${(r.cost / max) * 100}%` }} /></span><span className="shrink-0 text-[0.6875rem] tabular-nums text-ink-3">{nf.format(r.queries)} q</span></div>
            </li>
          ))}
          {rows.length > 8 && <li><button className="text-[0.8125rem] text-accent hover:underline" onClick={() => setAll((x) => !x)}>{all ? "Show fewer" : `Show all ${rows.length}`}</button></li>}
        </ul>
      )}
    </section>
  );
}

// ── expensive queries ────────────────────────────────────────────────────────
function Queries({ a }: { a: Account }) {
  const max = Math.max(0.0001, ...a.shapes.map((s) => s.cost));
  return (
    <Card icon={Timer} title="The queries that cost the most" hint={<>The same query run again and again counts as one "shape". The last 30 days. AlertBot never reads a query's text: open one in Snowsight to see it.</>}>
      {a.shapes.length === 0 ? <p className="px-6 py-8 text-[0.875rem] text-ink-3">{a.errors.shapes ? `Couldn't be read: ${a.errors.shapes}` : "Nothing yet."}</p> : (
        <ol className="divide-y divide-[var(--line)]">
          {a.shapes.map((s, i) => (
            <li key={s.hash} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5 sm:px-6">
              <span className="w-6 shrink-0 text-[0.8125rem] font-bold text-ink-3">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-1.5 text-[0.875rem] text-ink">
                  <b>{s.type || "Query"}</b>{s.database && <span className="text-ink-2">on {s.database}{s.schema ? `.${s.schema}` : ""}</span>}
                  <span className="text-ink-3">· {s.wh} · {s.user}</span>{s.tag && <span className="ai-chip">{s.tag}</span>}
                </p>
                <p className="mt-0.5 text-[0.75rem] text-ink-3">Ran {nf.format(s.runs)} times · usually {secs(s.avg_ms)} · {s.per_run != null ? `${usd(s.per_run)} a run` : ""} · last {s.last_at ? ago(s.last_at) : "–"}</p>
                <span className="cc-share mt-1.5 block max-w-md"><span style={{ width: `${(s.cost / max) * 100}%` }} /></span>
              </div>
              <span className="w-24 text-right"><b className="text-[1rem] tabular-nums text-ink">{usd(s.cost)}</b><span className="block text-[0.6875rem] text-ink-3">30 days</span></span>
              {s.link ? <Button asChild size="sm" variant="ghost"><a href={s.link} target="_blank" rel="noreferrer"><ExternalLink />Snowsight</a></Button> : <span className="w-24" />}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

// ── pipelines ────────────────────────────────────────────────────────────────
function Pipelines({ a, onChange }: { a: Account; onChange: (a: Account) => void }) {
  const p = a.pipelines;
  const max = Math.max(0.0001, ...p.rows.map((r) => r.cost));
  const [adding, setAdding] = useState("");
  const setUsers = async (users: string[] | null) => {
    try {
      onChange(await api<Account>(`/api/compute/${a.id}/settings`, { method: "PUT", body: JSON.stringify(users === null ? { reset_pipeline_users: true } : { pipeline_users: users }) }));
      toast.success("Saved", { description: "Pipelines' costs are worked out again on the next refresh." });
    } catch (e) { toast.error("Couldn't save", { description: (e as Error).message }); }
  };
  return (
    <>
      <Card icon={Workflow} title="What each pipeline costs in Snowflake" hint="The last 30 days. Credits of the pipelines' logins, matched to the pipeline that was running at the time (or named in the query tag).">
        {p.rows.length === 0 ? (
          <p className="px-6 py-8 text-[0.875rem] text-ink-2">{p.linked_pipelines === 0 ? "None of your pipelines reads or writes Snowflake (from their definitions)." : "No credits matched a pipeline run yet: check the pipelines' logins below."}</p>
        ) : (
          <ul className="divide-y divide-[var(--line)]">
            {p.rows.map((r) => (
              <li key={r.project + r.pipeline} className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-5 py-3.5 sm:px-6">
                <Workflow className="size-4 shrink-0 text-accent" />
                <div className="min-w-0 flex-1">
                  <Link to={`/p/${r.project}/runs?pipeline=${encodeURIComponent(r.pipeline)}`} className="font-semibold text-ink hover:underline">{r.pipeline}</Link>
                  <span className="ml-2 text-[0.75rem] text-ink-3">{r.project_name} · {nf.format(r.runs)} runs · matched by {r.how === "tag" ? "query tag" : "time"}</span>
                  <span className="cc-share mt-1.5 block max-w-md"><span style={{ width: `${(r.cost / max) * 100}%` }} /></span>
                </div>
                <span className="w-24 text-right"><b className="tabular-nums text-ink">{usd(r.per_run_cost)}</b><span className="block text-[0.6875rem] text-ink-3">a run</span></span>
                <span className="w-24 text-right"><b className="tabular-nums text-ink">{usd(r.cost)}</b><span className="block text-[0.6875rem] text-ink-3">30 days</span></span>
              </li>
            ))}
          </ul>
        )}
        {p.unlinked_cost > 0 && <p className="border-t border-line px-6 py-3 text-[0.8125rem] text-ink-3">{usd(p.unlinked_cost)} by the pipelines' logins didn't match a run (other jobs, or runs the bot doesn't watch).</p>}
      </Card>
      <Card icon={Users} title="The logins your pipelines use" hint={p.users_set ? "Set by you." : "The bot's guess, from login names (ADF, SVC, ETL…). Correct it if needed."}>
        <div className="flex flex-wrap items-center gap-2 p-5 sm:p-6">
          {p.users.map((u) => <span key={u} className="cc-chip">{u}<button aria-label={`Remove ${u}`} onClick={() => void setUsers(p.users.filter((x) => x !== u))}><X className="size-3" /></button></span>)}
          {p.users.length === 0 && <span className="text-[0.875rem] text-ink-3">None found.</span>}
          <form className="flex items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); if (adding.trim()) { void setUsers([...p.users, adding.trim().toUpperCase()]); setAdding(""); } }}>
            <Input className="h-8 w-44" value={adding} onChange={(e) => setAdding(e.target.value)} placeholder="Add a login, e.g. ADF_SVC" aria-label="Add a login" list="cc-users" />
            <datalist id="cc-users">{a.users.map((u) => <option key={u.name} value={u.name} />)}</datalist>
            <Button size="sm" variant="secondary" type="submit"><Plus />Add</Button>
          </form>
          {p.users_set && <Button size="sm" variant="ghost" onClick={() => void setUsers(null)}>Back to the bot's guess</Button>}
        </div>
        <p className="border-t border-line px-6 py-3 text-[0.75rem] text-ink-3"><Sparkles className="mr-1 inline size-3.5 text-accent" />Most exact: give each pipeline's Snowflake linked service a query tag with the pipeline's name (QUERY_TAG in its connection settings). Then credits are matched by tag, not by time.</p>
      </Card>
    </>
  );
}

// ── savings ──────────────────────────────────────────────────────────────────
const REC_ICON: Record<Rec["kind"], LucideIcon> = { suspend: Moon, downsize: ArrowDownRight, upsize: Hourglass, monitor: Wallet };
function Savings({ a }: { a: Account }) {
  const [picked, setPicked] = useState<string[]>([]);
  const [script, setScript] = useState<{ sql: string; filename: string; monthly: number; skipped: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const make = async (ids: string[]) => {
    setBusy(true);
    try { setScript(await post(`/api/compute/${a.id}/script`, { ids })); } catch (e) { toast.error("Couldn't write the script", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  const total = a.recommendations.filter((r) => picked.includes(r.id)).reduce((x, r) => x + r.monthly, 0);
  return (
    <Card icon={PiggyBank} title="Changes that save money" hint="Worked out from the last 30 days. Pick the ones you want; AlertBot writes the script and never runs it."
      actions={a.recommendations.length > 0 && <Button size="sm" variant="primary" disabled={!picked.length || busy} onClick={() => void make(picked)}>{busy ? <Loader2 className="animate-spin" /> : <FileCode2 />}Script for {picked.length || "the"} chosen{total ? ` · ${usd(total, 0)}/mo` : ""}</Button>}>
      {a.recommendations.length === 0 ? <p className="flex items-center gap-2 px-6 py-8 text-[0.875rem] text-ink-2"><Check className="size-5 text-[var(--good)]" />Nothing to change: the warehouses look well set up.</p> : (
        <ul className="divide-y divide-[var(--line)]">
          {a.recommendations.map((r) => {
            const I = REC_ICON[r.kind];
            const on = picked.includes(r.id);
            return (
              <li key={r.id} className={cn("cc-rec", on && "is-on")}>
                <input type="checkbox" className="wh-check mt-1" checked={on} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, r.id] : p.filter((x) => x !== r.id)))} aria-label={`Choose: ${r.title}`} />
                <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><I className="size-4" /></span>
                <div className="min-w-0 flex-1">
                  <p className="text-[0.9375rem] font-semibold text-ink">{r.title}</p>
                  <p className="mt-0.5 text-[0.8125rem] leading-5 text-ink-2">{r.why}</p>
                  <p className="mt-1 flex flex-wrap gap-1.5 text-[0.75rem]"><span className="ai-chip">{r.change}</span>{r.confidence !== "high" && <span className="ai-chip">{r.confidence} confidence</span>}</p>
                </div>
                <div className="text-right">
                  {r.monthly > 0 ? <><b className="text-[1.125rem] tabular-nums text-[var(--good)]">{usd(r.monthly, 0)}</b><span className="block text-[0.6875rem] text-ink-3">a month, about</span></>
                    : <span className="text-[0.75rem] text-ink-3">{r.kind === "monitor" ? "protects the budget" : "faster, costs more"}</span>}
                  <Button size="sm" variant="ghost" className="mt-1" onClick={() => void make([r.id])}><FileCode2 />Script</Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <ScriptModal script={script} onClose={() => setScript(null)} />
    </Card>
  );
}
function ScriptModal({ script, onClose }: { script: { sql: string; filename: string; monthly: number; skipped: string[] } | null; onClose: () => void }) {
  const copy = async () => { if (!script) return; try { await navigator.clipboard.writeText(script.sql); toast.success("Script copied"); } catch { toast.error("Couldn't copy: select it and copy by hand"); } };
  const download = () => {
    if (!script) return;
    const url = URL.createObjectURL(new Blob([script.sql], { type: "application/sql;charset=utf-8" }));
    const el = Object.assign(document.createElement("a"), { href: url, download: script.filename });
    document.body.appendChild(el); el.click(); el.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <Modal open={script != null} onClose={onClose} wide icon={FileCode2} title="Script for Snowflake" subtitle="AlertBot never runs this. Run it in a Snowsight worksheet with a role allowed to change warehouses; each change says how to undo it."
      footer={<><span className="mr-auto text-[0.8125rem] text-ink-3">{script?.monthly ? `Saves about ${usd(script.monthly, 0)} a month` : ""}</span><Button variant="secondary" onClick={download}><Download />Download .sql</Button><Button variant="primary" onClick={() => void copy()}><Copy />Copy</Button></>}>
      {script && <pre className="wh-code scrollbar-thin"><code>{script.sql}</code></pre>}
    </Modal>
  );
}

// ── alerts & budget ──────────────────────────────────────────────────────────
function Alerts({ a, onChange }: { a: Account; onChange: (a: Account) => void }) {
  const s = a.settings;
  const [budget, setBudget] = useState(s.budget ? String(s.budget) : "");
  const [runaway, setRunaway] = useState(String(s.runaway_minutes));
  const [factor, setFactor] = useState(s.spike_factor);
  const [busy, setBusy] = useState(false);
  const save = async (body: Record<string, unknown>) => {
    setBusy(true);
    try { onChange(await api<Account>(`/api/compute/${a.id}/settings`, { method: "PUT", body: JSON.stringify(body) })); toast.success("Saved"); }
    catch (e) { toast.error("Couldn't save", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  const long = a.running.filter((q) => q.long);
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <Card icon={Bell} title="When to tell you" hint="Alerts show here and go to Teams through the bot's default flow.">
        <div className="space-y-5 p-5 sm:p-6">
          <label className="block"><span className="text-[0.875rem] font-semibold text-ink">Monthly budget for compute</span>
            <span className="block text-[0.75rem] text-ink-3">Warned at 80% and 100%, and when the month is heading over it.</span>
            <span className="mt-2 flex items-center gap-1.5"><span className="text-ink-3">$</span><Input className="h-9 w-32" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="no budget" />
              <Button size="sm" variant="primary" disabled={busy} onClick={() => void save({ budget: budget.trim() ? Number(budget) : null })}><Check />Save</Button>
              {a.summary.last_month > 0 && !s.budget && <button className="text-[0.75rem] text-accent hover:underline" onClick={() => setBudget(String(Math.ceil(a.summary.last_month * 1.1 / 10) * 10))}>Use last month + 10%</button>}</span></label>
          <label className="block"><span className="text-[0.875rem] font-semibold text-ink">A query running too long</span>
            <span className="block text-[0.75rem] text-ink-3">A stuck query keeps its warehouse on, billing every second.</span>
            <span className="mt-2 flex items-center gap-1.5 text-[0.875rem]">after <Input className="h-9 w-20" inputMode="numeric" value={runaway} onChange={(e) => setRunaway(e.target.value)} /> minutes
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => void save({ runaway_minutes: Number(runaway) })}><Check />Save</Button></span></label>
          <div><span className="text-[0.875rem] font-semibold text-ink">A warehouse suddenly spending more</span>
            <span className="block text-[0.75rem] text-ink-3">Compared with the same hour on the 14 days before.</span>
            <div className="mt-2"><Segmented<number> size="md" label="Spike sensitivity" value={factor} onChange={(v) => { setFactor(v); void save({ spike_factor: v }); }}
              options={[{ value: 2, label: "2× usual (sensitive)" }, { value: 3, label: "3× usual" }, { value: 5, label: "5× usual (calm)" }]} /></div></div>
          <label className="flex items-center gap-2 text-[0.875rem] text-ink"><input type="checkbox" className="wh-check" checked={s.alerts_on} onChange={(e) => void save({ alerts_on: e.target.checked })} />Send alerts to Teams</label>
        </div>
      </Card>
      <div className="space-y-4">
        <Card icon={Timer} title="Running now" hint={`Checked every 10 minutes${a.running_at ? `, last ${ago(a.running_at)}` : ""}.`}>
          {a.running.length === 0 ? <p className="px-6 py-6 text-[0.875rem] text-ink-3">{a.errors.running ? `Couldn't be read: ${a.errors.running}` : "Nothing running."}</p> : (
            <ul className="divide-y divide-[var(--line)]">
              {a.running.slice(0, 8).map((q) => (
                <li key={q.id} className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6">
                  {q.long ? <AlertTriangle className="size-4 text-[var(--warning)]" /> : <Loader2 className="size-4 animate-spin text-accent" />}
                  <span className="min-w-0 flex-1 text-[0.8125rem]"><b className="text-ink">{Math.floor(q.seconds / 60)} min</b> <span className="text-ink-2">on {q.wh} ({q.size}) · {q.user}</span></span>
                  {q.link && <Button asChild size="sm" variant="ghost"><a href={q.link} target="_blank" rel="noreferrer"><ExternalLink />Snowsight</a></Button>}
                </li>
              ))}
            </ul>
          )}
          {long.length > 0 && <p className="border-t border-line px-6 py-2.5 text-[0.75rem] text-[var(--warning)]">{long.length} running longer than {s.runaway_minutes} minutes.</p>}
        </Card>
        <Card icon={AlertTriangle} title="Alerts" hint="The last 30.">
          {a.alerts.length === 0 ? <p className="flex items-center gap-2 px-6 py-6 text-[0.875rem] text-ink-2"><Check className="size-4 text-[var(--good)]" />No alerts.</p> : (
            <ul className="divide-y divide-[var(--line)]">
              {a.alerts.map((x) => (
                <li key={x.key} className="flex gap-3 px-5 py-3.5 sm:px-6">
                  <span className={cn("mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg", x.severity === "critical" ? "bg-[var(--critical-soft,var(--warning-soft))] text-[var(--critical)]" : "bg-[var(--warning-soft)] text-[var(--warning)]")}>
                    {x.kind === "spike" ? <Zap className="size-4" /> : x.kind === "runaway" ? <Timer className="size-4" /> : <Wallet className="size-4" />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-[0.875rem] font-semibold text-ink">{x.title}</p>
                    <p className="mt-0.5 text-[0.8125rem] leading-5 text-ink-2">{x.detail}</p>
                    <p className="mt-1 text-[0.71875rem] text-ink-3">{ago(x.at)} · {x.delivery === "sent" ? "sent to Teams" : x.delivery === "no_flow" ? "Teams: no flow set up" : x.delivery === "off" ? "Teams alerts off" : x.delivery === "failed" ? "Teams: couldn't send" : "sending…"}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
