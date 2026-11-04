// Cortex AI usage: what Snowflake Cortex AI costs (Agents, Cortex Code, AI SQL functions, Analyst, Search…), who used it
// and on which models. Every dollar comes from Snowflake's own bill (METERING_DAILY_HISTORY, the AI service lines); the
// Cortex usage views explain where it went, and the page says how much of the bill they account for. Nothing is
// estimated: what can't be read is shown as unavailable. Same look as Compute cost (the cards, charts and colours).
import {
  Activity, BrainCircuit, CalendarDays, Database, Info, Layers, Loader2, MessagesSquare, RefreshCw, ShieldCheck, Sparkles, Users, type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Metric, MetricStrip, Notice } from "@/components/Cards";
import { DbPicker } from "@/components/DbPicker";
import { Disclosure } from "@/components/Disclosure";
import { Button } from "@/components/ui/button";
import { EmptyState, Segmented, Skeleton } from "@/components/ui/primitives";
import { post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { Card, Count, dayOf, dm, HoverCard, usd, type Tip } from "./Compute";

// ── shapes (as /api/cortex sends them) ───────────────────────────────────────
interface Named { name: string; credits: number; tokens: number; requests: number; cost: number }
interface Product {
  id: string; name: string; what: string; has_credits: boolean; credits: number; cost: number; tokens: number; requests: number; pages: number | null;
  active_days: number; first: string; last: string; users: Named[]; models: Named[]; agents: Named[]; functions: Named[]; surfaces: Named[]; services: Named[];
}
interface Period {
  days: number; credits: number; cost: number; prev_cost: number; active_days: number; tokens: number; requests: number; users_n: number; models_n: number;
  detail_credits: number; covered: number | null; services: { service: string; name: string; help: string; credits: number; cost: number }[];
  products: Product[]; users: Named[]; models: Named[]; agents: Named[];
}
interface Account {
  id: number; name: string; account: string; scanned_at: string | null; error: string | null; price: number; price_from: string; price_note: string;
  month: { mtd_credits: number; mtd: number; forecast: number; last_month: number; yesterday: number };
  first_used: string | null; last_used: string | null; chart: { date: string; credits: number; cost: number; services: Record<string, number> }[];
  periods: Record<string, Period>; default_period: string; sources: Record<string, string>; unavailable: string[]; errors: Record<string, string>;
}
interface Report { accounts: Account[]; generated_at: string }

type PeriodKey = "30" | "90" | "365";
const PERIOD_LABEL: Record<PeriodKey, string> = { "30": "last 30 days", "90": "last 90 days", "365": "last 12 months" };
const tok = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const nf = new Intl.NumberFormat("en-US");
/** Credits with two decimals (Cortex amounts are small; the shared format rounds above 10). */
const crd = (v: number) => (v < 1000 ? v.toFixed(2) : nf.format(Math.round(v)));
/** A share as a percentage that never rounds up to 100% unless it is 100%. */
const share = (v: number) => (v >= 1 ? "100" : (Math.floor(v * 10000) / 100).toFixed(v >= 0.99 ? 2 : 0));
const date = (s: string | null) => (s ? dm.format(dayOf(s)) : "–");
const PRODUCT_ICON: Record<string, LucideIcon> = {
  agents: Sparkles, intelligence: MessagesSquare, code: BrainCircuit, aisql: Database, analyst: MessagesSquare, search: Layers, documents: Layers,
  fine_tuning: BrainCircuit, guardrails: ShieldCheck, throughput: Activity, rest: Activity,
};
const SOURCE_LABEL: Record<string, string> = {
  bill: "METERING_DAILY_HISTORY (the bill)", agents: "CORTEX_AGENT_USAGE_HISTORY", intelligence: "SNOWFLAKE_INTELLIGENCE_USAGE_HISTORY",
  code_snowsight: "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", code_cli: "CORTEX_CODE_CLI_USAGE_HISTORY", code_desktop: "CORTEX_CODE_DESKTOP_USAGE_HISTORY",
  aisql: "CORTEX_AISQL_USAGE_HISTORY", functions: "CORTEX_FUNCTIONS_USAGE_HISTORY (older accounts)", analyst: "CORTEX_ANALYST_USAGE_HISTORY",
  search: "CORTEX_SEARCH_DAILY_USAGE_HISTORY", documents: "CORTEX_DOCUMENT_PROCESSING_USAGE_HISTORY", document_ai: "DOCUMENT_AI_USAGE_HISTORY",
  fine_tuning: "CORTEX_FINE_TUNING_USAGE_HISTORY", guardrails: "CORTEX_AI_GUARDRAILS_USAGE_HISTORY", throughput: "CORTEX_PROVISIONED_THROUGHPUT_USAGE_HISTORY",
  rest: "CORTEX_REST_API_USAGE_HISTORY",
};
/** One colour per Cortex product, the same in every chart; a bill line takes its product's colour. */
const FAMILY_COLOR: Record<string, string> = {
  agents: "var(--cat-3)", code: "var(--cat-2)", aisql: "var(--cat-1)", analyst: "var(--cat-7)", rest: "var(--cat-5)", intelligence: "var(--cat-4)",
  search: "var(--cat-6)", documents: "var(--cat-4)", fine_tuning: "var(--cat-4)", guardrails: "var(--cat-6)", throughput: "var(--cat-5)",
};
const familyOf = (service: string) => {
  const s = service.toUpperCase();
  return s === "CORTEX_AGENTS" ? "agents" : s.startsWith("SNOWFLAKE_COCO") ? "code" : s === "AI_FUNCTIONS" ? "aisql" : s === "AI_SERVICES" ? "analyst"
    : s === "AI_INFERENCE" ? "rest" : s.startsWith("SNOWFLAKE_INTELLIGENCE") ? "intelligence" : s.includes("SEARCH") ? "search" : s.includes("DOCUMENT") ? "documents" : "";
};
const productColor = (id: string) => FAMILY_COLOR[id] ?? "var(--ink-3)";
/** Cortex Code in Snowsight / CLI / desktop share a colour: the CLI and desktop are lighter shades of it. */
const serviceColor = (service: string) => {
  const c = productColor(familyOf(service));
  return /COCO_(CLI|DESKTOP)/i.test(service) ? `color-mix(in srgb, ${c} ${/CLI/i.test(service) ? 60 : 35}%, var(--surface))` : c;
};
const store = (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* blocked */ } };
const stored = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };

// ── the page ─────────────────────────────────────────────────────────────────
export function CortexUsage() {
  const { data, error, reload, setData } = useApi<Report>("/api/cortex", { refreshMs: 120000 });
  const [which, setWhichRaw] = useState<number | null>(() => Number(stored("alertbot-cx-db")) || null);
  const setWhich = (id: number | null) => { setWhichRaw(id); if (id !== null) store("alertbot-cx-db", String(id)); };
  const [period, setPeriod] = useState<PeriodKey | null>(null);
  const [scanning, setScanning] = useState(false);
  const [tip, setTip] = useState<Tip>(null);
  const autoScanned = useRef(false);
  useNow();

  const scan = async (quiet = false) => {
    setScanning(true);
    const id = quiet ? undefined : toast.loading("Reading Snowflake's Cortex usage…", { description: "The bill's AI lines and every Cortex usage view. Never a prompt or an answer." });
    try {
      const r = await post<Report>("/api/cortex/scan");
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

  if (error && !data) return <Notice tone="warn">Couldn't load Cortex AI usage: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-64 w-full rounded-[1.75rem]" /><Skeleton className="h-28 w-full" /><Skeleton className="h-96 w-full" /></div>;
  if (!data.accounts.length) {
    return (
      <EmptyState icon={BrainCircuit} action={<Button asChild variant="primary"><Link to="/databases">Connect Snowflake</Link></Button>}>
        Connect your Snowflake account and the bot reads what Cortex AI costs: agents, Cortex Code, AI functions, Analyst and the rest, by user and model.
      </EmptyState>
    );
  }
  const a = data.accounts.find((x) => x.id === which) ?? data.accounts[0]!;
  const pk = (period ?? a.default_period) as PeriodKey;
  const p = a.periods[pk]!;
  const show = (e: React.MouseEvent, body: ReactNode) => setTip({ x: e.clientX, y: e.clientY, body });
  const hide = () => setTip(null);
  const empty = !p.credits && !p.tokens;

  return (
    <div className="cc pb-24" onMouseLeave={hide}>
      <Hero a={a} p={p} pk={pk} setPeriod={setPeriod} scan={() => void scan()} scanning={scanning}
        picker={data.accounts.length > 1 ? <DbPicker label="Snowflake account" all={false} value={a.id} onChange={setWhich}
          options={data.accounts.map((x) => ({ id: x.id, name: x.name, kind: "snowflake", note: x.account }))} /> : null} />
      {a.error && <Notice tone="warn" className="mb-4">{a.error} {a.scanned_at ? "The last good reading is shown." : ""}</Notice>}
      {!a.scanned_at ? (
        <div className="flex items-center gap-3 rounded-2xl bg-surface p-6 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]"><Loader2 className="size-5 animate-spin text-accent" />Reading Snowflake's Cortex usage for the first time…</div>
      ) : empty ? (
        <EmptyState icon={BrainCircuit} action={pk !== "365" ? <Button variant="secondary" onClick={() => setPeriod("365")}>Show the last 12 months</Button> : undefined}>
          No Cortex AI use in the {PERIOD_LABEL[pk]}.{a.last_used ? ` It was last used on ${date(a.last_used)}.` : " Nothing in the last 12 months either."}
        </EmptyState>
      ) : (
        <>
          <MetricStrip className="mb-5" cols="grid-cols-2 lg:grid-cols-4">
            <Metric icon={Sparkles} iconColor="var(--cat-1)" label="Tokens" value={<Count value={p.tokens} fmt={(v) => tok.format(v)} />}
              footnote={`${PERIOD_LABEL[pk]} · what the models read and wrote`} />
            <Metric icon={MessagesSquare} iconColor="var(--cat-7)" label="Requests" value={nf.format(Math.round(p.requests))} footnote="agent turns, Cortex Code messages, AI function queries…" />
            <Metric icon={CalendarDays} iconColor="var(--cat-3)" label="Days with Cortex use" value={nf.format(p.active_days)}
              footnote={a.last_used ? `last used ${date(a.last_used)}` : "–"} />
            <Metric icon={Users} iconColor="var(--cat-2)" label="People · models" value={`${p.users_n} · ${p.models_n}`} footnote="who used it, and the models named in the usage" />
          </MetricStrip>
          <DailyChart a={a} pk={pk} show={show} hide={hide} />
          <Products p={p} />
          <div className="mb-5 grid gap-4 xl:grid-cols-3">
            <Ranked icon={Users} title="People" hint="Cortex credits each person used, all products together." rows={p.users} />
            <Ranked icon={BrainCircuit} title="Models" hint="Models named in the usage. Agents and Cortex Code bill per request, so they're under products." rows={p.models} tokens />
            <Ranked icon={Sparkles} title="Agents" hint="Cortex Agents and Snowflake Intelligence, by agent." rows={p.agents} />
          </div>
          <TokensByProduct p={p} />
        </>
      )}
      {a.scanned_at && <Sources a={a} />}
      <HoverCard tip={tip} />
    </div>
  );
}

// ── hero: the period's cost, and the bill it comes from ──────────────────────
function Hero({ a, p, pk, setPeriod, scan, scanning, picker }: {
  a: Account; p: Period; pk: PeriodKey; setPeriod: (k: PeriodKey) => void; scan: () => void; scanning: boolean; picker: ReactNode;
}) {
  const change = p.prev_cost > 0 ? p.cost / p.prev_cost - 1 : null;
  const end = new Date(); end.setMonth(end.getMonth() + 1, 0);
  return (
    <section className="ai-hero cc-hero card-in relative mb-4 overflow-hidden rounded-[1.75rem] p-6 sm:p-8">
      <div className="ai-hero-glow cc-glow" aria-hidden />
      <div className="relative flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <p className="eyebrow flex items-center gap-2 font-semibold text-accent"><BrainCircuit className="size-3.5" /> Cortex AI usage</p>
          {picker}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<PeriodKey> size="sm" label="Period" value={pk} onChange={setPeriod}
            options={[{ value: "30", label: "30 days" }, { value: "90", label: "90 days" }, { value: "365", label: "12 months" }]} />
          <Button size="sm" variant="secondary" onClick={scan} disabled={scanning}>{scanning ? <Loader2 className="animate-spin" /> : <RefreshCw />}{scanning ? "Reading…" : "Refresh"}</Button>
        </div>
      </div>
      <div className="relative mt-4 grid gap-6 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] lg:items-center">
        <div className="min-w-0">
          <p className="text-[0.9375rem] font-medium text-ink-2">Cortex AI, {PERIOD_LABEL[pk]}</p>
          <h1 className="display mt-1 flex flex-wrap items-baseline gap-x-3 text-[2.75rem] font-bold leading-[1.02] tracking-[-0.03em] text-ink sm:text-[3.5rem]">
            <span className="tabular-nums"><Count value={p.cost} fmt={(v) => usd(v)} /></span>
            <span className="text-[1.0625rem] font-semibold tracking-normal text-ink-2">{crd(p.credits)} credits</span>
          </h1>
          <p className="mt-2 text-[0.9375rem] leading-6 text-ink-2">
            {change != null ? <><span className={change > 0.05 ? "text-[var(--critical)]" : change < -0.05 ? "text-[var(--good)]" : ""}>{change >= 0 ? "+" : ""}{(change * 100).toFixed(0)}%</span> on the {pk === "365" ? "12 months" : `${pk} days`} before ({usd(p.prev_cost)}) · </>
              : p.cost > 0 ? <>Nothing in the {pk === "365" ? "12 months" : `${pk} days`} before · </> : null}
            this month so far <b className="font-semibold text-ink">{usd(a.month.mtd)}</b>
            {a.month.mtd > 0 ? <>, heading for about <b className="font-semibold text-ink">{usd(a.month.forecast)}</b> by {dm.format(end)}</>
              : a.last_used ? <> · last used <b className="font-semibold text-ink">{date(a.last_used)}</b></> : null}
          </p>
          <p className="mt-2 text-[0.8125rem] text-ink-3">
            {usd(a.price)} per credit ({a.price_from === "yours" ? "your price" : a.price_from === "contract" ? "your contract's rate" : "Snowflake's list price"}): change it on{" "}
            <Link to="/compute" className="font-medium text-accent hover:underline">Compute cost</Link>.
          </p>
        </div>
        <div className="ai-whatif min-w-0 rounded-2xl p-4 sm:p-5">
          <p className="text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">On your Snowflake bill</p>
          {p.services.length === 0 ? <p className="mt-3 text-[0.875rem] text-ink-3">No AI lines on the bill in this period.</p> : (
            <ul className="mt-3 space-y-1.5 text-[0.8125rem]">
              {p.services.map((s) => (
                <li key={s.service} className="flex items-center justify-between gap-3" title={s.help}>
                  <span className="flex min-w-0 items-center gap-2 text-ink-2"><span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: serviceColor(s.service) }} /><span className="truncate">{s.name}</span></span>
                  <b className="shrink-0 tabular-nums text-ink">{usd(s.cost)}</b>
                </li>
              ))}
            </ul>
          )}
          {p.covered != null && (
            <div className="mt-3 border-t border-[var(--line)] pt-3 text-[0.75rem] text-ink-3">
              <div className="flex items-center justify-between gap-2">
                <span>Explained by the usage detail below</span>
                <b className={cn("tabular-nums", p.covered >= 0.98 ? "text-[var(--good)]" : "text-ink")}>{share(p.covered)}%</b>
              </div>
              <span className="cc-share mt-1.5 block"><span style={{ width: `${Math.min(100, p.covered * 100)}%`, background: "var(--good)" }} /></span>
              {p.covered < 0.995 && <p className="mt-1.5">The rest is billed but not itemised by Snowflake's usage views (or they're still catching up).</p>}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

// ── day by day, from the bill ────────────────────────────────────────────────
function DailyChart({ a, pk, show, hide }: { a: Account; pk: PeriodKey; show: (e: React.MouseEvent, b: ReactNode) => void; hide: () => void }) {
  const n = Number(pk);
  const services = useMemo(() => {
    const tot: Record<string, number> = {};
    for (const d of a.chart.slice(-n)) for (const [s, v] of Object.entries(d.services)) tot[s] = (tot[s] ?? 0) + v;
    return Object.entries(tot).filter(([, v]) => v > 0).sort((x, y) => y[1] - x[1]).map(([s]) => s);
  }, [a.chart, n]);
  const nameOf = (s: string) => a.periods["365"]!.services.find((x) => x.service === s)?.name ?? s;
  // 12 months: one bar a week, so the bars stay readable
  const rows = useMemo(() => {
    const days = a.chart.slice(-n);
    if (n <= 90) return days.map((d) => ({ key: d.date, label: date(d.date), parts: d.services, total: d.cost }));
    const weeks: { key: string; label: string; parts: Record<string, number>; total: number }[] = [];
    for (let i = 0; i < days.length; i += 7) {
      const chunk = days.slice(i, i + 7);
      const parts: Record<string, number> = {};
      for (const d of chunk) for (const [s, v] of Object.entries(d.services)) parts[s] = (parts[s] ?? 0) + v;
      weeks.push({ key: chunk[0]!.date, label: `${date(chunk[0]!.date)} – ${date(chunk[chunk.length - 1]!.date)}`, parts, total: chunk.reduce((x, y) => x + y.cost, 0) });
    }
    return weeks;
  }, [a.chart, n]);
  const max = Math.max(0.0001, ...rows.map((r) => r.total));
  return (
    <Card icon={Layers} title={n <= 90 ? "Cortex spend, day by day" : "Cortex spend, week by week"}
      hint={`From the bill, split by its AI lines. The ${PERIOD_LABEL[pk]}${n > 90 ? ", one bar a week" : ""}; hover a bar for the detail.`}>
      <div className="p-5 sm:p-6">
        <div className="cc-chart" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0, 1fr))` }}>
          {rows.map((r) => (
            <div key={r.key} className="cc-col" onMouseLeave={hide} onMouseMove={(e) => show(e, <>
              <p className="font-semibold text-ink">{r.label} · {usd(r.total)}</p>
              {r.total === 0 ? <p className="text-ink-3">No Cortex use</p> : services.filter((s) => r.parts[s]).map((s) => (
                <p key={s} className="flex justify-between gap-4"><span className="flex items-center gap-1.5 text-ink-2"><span className="size-2 rounded-[2px]" style={{ background: serviceColor(s) }} />{nameOf(s)}</span><b className="text-ink">{usd(r.parts[s])}</b></p>
              ))}
            </>)}>
              <span className="cc-stack" style={{ height: `${(r.total / max) * 100}%` }}>
                {services.map((s) => r.parts[s] ? <span key={s} className="ai-grow" style={{ flexGrow: r.parts[s], background: serviceColor(s) }} /> : null)}
              </span>
            </div>
          ))}
        </div>
        <div className="mt-1 flex justify-between text-[0.6875rem] text-ink-3"><span>{rows[0]?.label.split(" – ")[0]}</span><span>today</span></div>
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[0.75rem] text-ink-2">
          {services.map((s) => <span key={s} className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-[3px]" style={{ background: serviceColor(s) }} />{nameOf(s)}</span>)}
        </div>
      </div>
    </Card>
  );
}

// ── by product ───────────────────────────────────────────────────────────────
function Products({ p }: { p: Period }) {
  const total = Math.max(0.0001, p.products.reduce((x, y) => x + y.cost, 0));
  return (
    <Card icon={BrainCircuit} title="Where it went, by Cortex product"
      hint="From Snowflake's Cortex usage views: credits, tokens and requests for each product, and who used it with which models.">
      <div className="grid gap-3 p-4 sm:p-5 md:grid-cols-2">
        {p.products.map((x) => {
          const I = PRODUCT_ICON[x.id] ?? Sparkles;
          const chips: [string, Named[]][] = [["Where", x.surfaces], ["Functions", x.functions], ["Models", x.models], ["Agents", x.agents], ["Services", x.services], ["People", x.users]];
          return (
            <section key={x.id} className="cx-product">
              <div className="flex items-start gap-3">
                <span className="cx-product-icon"><I className="size-[1.125rem]" aria-hidden /></span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <h3 className="text-[0.9375rem] font-semibold text-ink">{x.name}</h3>
                    <b className="text-[1.0625rem] tabular-nums text-ink">{x.has_credits ? usd(x.cost) : "on the bill"}</b>
                  </div>
                  <p className="text-[0.75rem] text-ink-3">{x.what}</p>
                </div>
              </div>
              {x.has_credits && <span className="cc-share mt-3 block"><span style={{ width: `${(x.cost / total) * 100}%`, background: productColor(x.id) }} /></span>}
              <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[0.75rem] sm:grid-cols-4">
                {x.has_credits && <div><dt className="text-ink-3">Credits</dt><dd className="font-semibold tabular-nums text-ink">{crd(x.credits)}</dd></div>}
                <div><dt className="text-ink-3">Tokens</dt><dd className="font-semibold tabular-nums text-ink">{x.tokens ? tok.format(x.tokens) : "–"}</dd></div>
                <div><dt className="text-ink-3">{x.pages ? "Pages" : "Requests"}</dt><dd className="font-semibold tabular-nums text-ink">{x.pages ? nf.format(x.pages) : x.requests ? nf.format(Math.round(x.requests)) : "–"}</dd></div>
                <div><dt className="text-ink-3">Used</dt><dd className="font-semibold text-ink">{x.first === x.last ? date(x.first) : `${date(x.first)} – ${date(x.last)}`}</dd></div>
              </dl>
              <div className="mt-3 space-y-1.5">
                {chips.filter(([, list]) => list.length).map(([label, list]) => (
                  <p key={label} className="flex flex-wrap items-center gap-1.5 text-[0.75rem]">
                    <span className="w-[4.25rem] shrink-0 text-ink-3">{label}</span>
                    {list.slice(0, 4).map((n) => (
                      <span key={n.name} className="ai-chip normal-case tracking-normal" title={`${n.name}: ${n.credits ? `${usd(n.cost)} · ` : ""}${n.tokens ? `${tok.format(n.tokens)} tokens` : ""}`}>
                        {n.name}{x.has_credits && n.cost >= 0.005 ? ` · ${usd(n.cost)}` : n.tokens ? ` · ${tok.format(n.tokens)}` : ""}
                      </span>
                    ))}
                  </p>
                ))}
              </div>
              {!x.has_credits && <p className="mt-2 text-[0.6875rem] text-ink-3">Snowflake's view for this has tokens but no credits: its cost is the bill's "{x.name}" line above.</p>}
            </section>
          );
        })}
      </div>
    </Card>
  );
}

function Ranked({ icon: I, title, hint, rows, tokens = false }: { icon: LucideIcon; title: string; hint: string; rows: Named[]; tokens?: boolean }) {
  const [all, setAll] = useState(false);
  const by = (r: Named) => (tokens ? r.tokens : r.cost);
  const max = Math.max(0.0001, ...rows.map(by));
  const shown = all ? rows : rows.slice(0, 8);
  return (
    <section className="card-in overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]">
      <header className="flex items-start gap-2.5 border-b border-line px-5 py-3.5"><I className="mt-0.5 size-4 text-accent" /><div><h3 className="text-[0.9375rem] font-semibold text-ink">{title}</h3><p className="text-[0.75rem] text-ink-3">{hint}</p></div></header>
      {rows.length === 0 ? <p className="px-5 py-6 text-[0.875rem] text-ink-3">None in this period.</p> : (
        <ul className="space-y-2.5 p-5">
          {shown.map((r) => (
            <li key={r.name}>
              <div className="flex items-baseline justify-between gap-2 text-[0.8125rem]">
                <span className="min-w-0 truncate font-medium text-ink" title={r.name}>{r.name}</span>
                <b className="shrink-0 tabular-nums text-ink">{r.cost >= 0.005 ? usd(r.cost) : r.tokens ? `${tok.format(r.tokens)} tokens` : usd(r.cost)}</b>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <span className="cc-share flex-1"><span style={{ width: `${(by(r) / max) * 100}%` }} /></span>
                <span className="shrink-0 text-[0.6875rem] tabular-nums text-ink-3">{r.tokens ? `${tok.format(r.tokens)} tok` : ""}{r.requests ? ` · ${nf.format(Math.round(r.requests))} req` : ""}</span>
              </div>
            </li>
          ))}
          {rows.length > 8 && <li><button className="text-[0.8125rem] text-accent hover:underline" onClick={() => setAll((x) => !x)}>{all ? "Show fewer" : `Show all ${rows.length}`}</button></li>}
        </ul>
      )}
    </section>
  );
}

function TokensByProduct({ p }: { p: Period }) {
  const rows = p.products.filter((x) => x.tokens > 0);
  const max = Math.max(1, ...rows.map((x) => x.tokens));
  const total = rows.reduce((s, x) => s + x.tokens, 0);
  if (!rows.length) return null;
  return (
    <Card icon={Sparkles} title="Tokens, by product" hint={`${tok.format(total)} tokens in the ${PERIOD_LABEL[String(p.days) as PeriodKey]}: what the models read and wrote, as Snowflake counted them.`}>
      <ul className="space-y-3 p-5 sm:p-6">
        {rows.map((x) => (
          <li key={x.id}>
            <div className="flex items-baseline justify-between gap-3 text-[0.8125rem]">
              <span className="flex items-center gap-2 font-medium text-ink"><span className="size-2.5 rounded-[3px]" style={{ background: productColor(x.id) }} />{x.name}</span>
              <span className="tabular-nums text-ink-2"><b className="text-ink">{tok.format(x.tokens)}</b> · {((x.tokens / total) * 100).toFixed(0)}%
                {x.has_credits && x.tokens ? <span className="text-ink-3"> · {usd((x.cost / x.tokens) * 1e6)} per million</span> : null}</span>
            </div>
            <span className="cx-bar mt-1.5 block"><span className="ai-grow-x" style={{ width: `${(x.tokens / max) * 100}%`, background: productColor(x.id) }} /></span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ── where every number comes from ────────────────────────────────────────────
function Sources({ a }: { a: Account }) {
  const keys = Object.keys(a.sources).filter((k) => k !== "functions" || a.sources[k] === "ok");
  return (
    <div className="mt-2">
      <Disclosure title="Where these numbers come from" hint={`The bill and ${keys.length - 1} Cortex usage views · read ${a.scanned_at ? ago(a.scanned_at) : "never"} · Snowflake runs up to about 3 hours behind`}>
        <div className="rounded-[1.25rem] bg-surface p-5 text-[0.8125rem] leading-6 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">
          <p className="flex items-start gap-2"><Info className="mt-1 size-4 shrink-0 text-accent" />
            <span>Every dollar is a credit Snowflake <b className="text-ink">billed</b> (METERING_DAILY_HISTORY, its AI lines) times {usd(a.price)} per credit ({a.price_note}). The usage views
              say where those credits went; days are UTC days, like the bill. The bot reads names, counts, credits and tokens only: never a prompt, an answer or a query's text.</span></p>
          <ul className="mt-3 grid gap-1 sm:grid-cols-2">
            {keys.map((k) => (
              <li key={k} className="flex items-center gap-2 text-[0.75rem]">
                <span className={cn("size-2 shrink-0 rounded-full", a.sources[k] === "ok" ? "bg-[var(--good)]" : "bg-[var(--warning)]")} aria-hidden />
                <code className="truncate">{SOURCE_LABEL[k] ?? k}</code>
                <span className="text-ink-3">{a.sources[k] === "ok" ? "read" : "not available"}</span>
              </li>
            ))}
          </ul>
          {a.unavailable.length > 0 && <p className="mt-2 text-[0.75rem] text-ink-3">Not available on this account (or not readable by its role), so nothing from them is shown: {a.unavailable.map((k) => SOURCE_LABEL[k] ?? k).join(", ")}.</p>}
        </div>
      </Disclosure>
    </div>
  );
}
