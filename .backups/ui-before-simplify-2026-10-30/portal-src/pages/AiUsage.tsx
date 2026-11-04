// AI usage: every model call the bot made, in the provider's own token counts, what it actually cost (free models:
// nothing), and what exactly the same work would have cost on OpenAI, Anthropic, Google and others at today's prices.
// Colours follow the entity (a provider, a feature, a token kind), never its rank.
import {
  Activity, ArrowDownRight, ArrowUpRight, Bot, ChartNoAxesColumn, Check, Coins, Cpu, Gauge, Info, Loader2, Mic, Plus, Search, Sparkles, X, type LucideIcon,
} from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChartCard, Metric, MetricStrip } from "@/components/Cards";
import { Button } from "@/components/ui/button";
import { Input, Segmented, Skeleton } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { ago, time } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import { cn } from "@/lib/utils";

// ── shapes ───────────────────────────────────────────────────────────────────
interface Cost { input: number; output: number; embeddings: number; total: number }
interface Scenario {
  id: string; name: string; provider: string; provider_name: string; input: number; output: number; cached_input: number | null; context: number | null;
  embedding_model: { id: string; name: string; input: number } | null; batch: { input: number; output: number } | null;
  cost: Cost; per_month: number; per_1k_calls: number | null;
}
interface Bucket {
  calls: number; answered: number; failed: number; prompt_tokens: number; completion_tokens: number; embedding_tokens: number; reasoning_tokens: number;
  tokens: number; estimated_calls: number; actual_cost: number; avg_latency_ms: number | null; audio_seconds: number; characters: number;
}
interface Usage {
  days: number; generated_at: string; first_call_at: string | null; span_days: number;
  totals: Bucket & { cost: Record<string, number>; tokens_per_day: number; generation: { prompt_tokens: number; completion_tokens: number }; embedding_tokens: number;
    voice: { calls: number; characters: number; audio_seconds: number } };
  features: (Bucket & { key: string; label: string; hint: string; cost: Record<string, number> })[];
  models: (Bucket & { model: string; free: boolean; price: { input: number; output: number } | null })[];
  projects: (Bucket & { project: string | null })[];
  daily: { date: string; prompt_tokens: number; completion_tokens: number; embedding_tokens: number; calls: number }[];
  recent: { at: string; feature: string; kind: string; model: string; served_model: string | null; status: string; http_status: number | null;
    prompt_tokens: number; completion_tokens: number; reasoning_tokens: number; estimated: boolean; latency_ms: number; project: string | null;
    cost_usd: number | null; audio_seconds: number | null; error: string | null }[];
  scenarios: Scenario[];
  prices: { fetched_at: string | null; source: string | null; error: string | null; count: number };
  features_catalog: Record<string, { label: string; hint: string }>;
}
interface PriceRow { id: string; name: string; provider: string; provider_name: string; input: number; output: number; context: number | null }

// ── colour by entity ─────────────────────────────────────────────────────────
const PROVIDER_COLOR: Record<string, string> = {
  openai: "var(--cat-1)", anthropic: "var(--cat-2)", google: "var(--cat-3)", "x-ai": "var(--cat-4)", deepseek: "var(--cat-5)",
  mistralai: "var(--cat-6)", "meta-llama": "var(--cat-7)", qwen: "var(--cat-8)",
};
const providerColor = (p: string) => PROVIDER_COLOR[p] ?? "var(--ink-3)";
const FEATURE_ORDER = ["incident_analysis", "ask", "dq", "charts", "report", "triage", "similar", "voice", "fixes", "rules", "other"];
const featureColor = (k: string) => `var(--cat-${(FEATURE_ORDER.indexOf(k) % 8) + 1})`;
const FEATURE_ICON: Record<string, LucideIcon> = {
  incident_analysis: Sparkles, triage: Gauge, similar: Search, ask: Bot, charts: ChartNoAxesColumn, dq: Check, report: Activity, voice: Mic, other: Cpu,
};
const TOKEN_KIND = [
  { key: "prompt_tokens", label: "Input (prompt)", color: "var(--cat-1)" },
  { key: "completion_tokens", label: "Output (answer)", color: "var(--cat-2)" },
  { key: "embedding_tokens", label: "Embeddings", color: "var(--cat-3)" },
] as const;

// ── numbers ──────────────────────────────────────────────────────────────────
const nf = new Intl.NumberFormat();
const cf = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });  // tokens: K / M / B, as every provider counts them
const tokens = (n: number) => (n < 10000 ? nf.format(Math.round(n)) : cf.format(n));
export function usd(v: number) {
  if (v === 0) return "$0.00";
  const a = Math.abs(v);
  if (a < 0.01) return `$${v.toFixed(a < 0.0001 ? 6 : 4)}`;
  if (a < 100) return `$${v.toFixed(2)}`;
  return `$${nf.format(Math.round(v))}`;
}
/** "OpenAI: GPT-6 Astra" → "GPT-6 Astra" (the provider is shown beside it). */
const shortName = (m: { name: string }) => m.name.replace(/^[^:]{1,40}:\s*/, "");
const perM = (v: number) => (v < 1 ? `$${v.toFixed(v < 0.1 ? 3 : 2)}` : `$${v.toFixed(v % 1 ? 2 : 0)}`);
const secs = (ms: number | null) => (ms == null ? "–" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);

/** Counts up to a value, smoothly (60+ fps, rAF), and settles on it; reduced motion jumps straight there. */
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

/** A width that follows its container (charts redraw crisp at any size). */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.round(e!.contentRect.width)));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

/** The hover card every chart shares: follows the pointer, stays inside the window. */
function HoverCard({ at, children }: { at: { x: number; y: number } | null; children: ReactNode }) {
  if (!at) return null;
  const left = Math.min(at.x + 14, window.innerWidth - 300);
  const top = Math.max(8, at.y - 12);
  return <div className="ai-tip pointer-events-none fixed z-[80]" style={{ left, top, transform: "translateY(-100%)" }}>{children}</div>;
}

// ── the page ─────────────────────────────────────────────────────────────────
type Range = 1 | 7 | 30 | 90;
type Basis = "period" | "month" | "per1k";

export function AiUsage() {
  const [days, setDays] = useState<Range>(30);
  const [picked, setPicked] = useState<string[] | null>(null);
  const qs = new URLSearchParams({ days: String(days), ...(picked ? { models: picked.join(",") } : {}) });
  const { data, error, reload, loading } = useApi<Usage>(`/api/ai/usage?${qs}`, { refreshMs: 15000 });
  const [basis, setBasis] = useState<Basis>("period");
  const [priceAs, setPriceAs] = useState<string | null>(null);
  useNow();

  const scenarios = data?.scenarios ?? [];
  const as = scenarios.find((s) => s.id === priceAs) ?? scenarios.find((s) => s.provider === "anthropic") ?? scenarios[0];
  const save = (ids: string[]) => {
    setPicked(ids);
    void api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ ai_scenarios: ids }) }).catch(() => undefined);
  };

  if (error && !data) return <div className="rounded-2xl bg-[var(--warning-soft)] p-4 text-sm text-ink">Couldn't load AI usage: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></div>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-56 w-full rounded-[1.75rem]" /><Skeleton className="h-28 w-full" /><Skeleton className="h-96 w-full" /></div>;

  const t = data.totals;
  const priciest = scenarios[scenarios.length - 1];
  const cheapest = scenarios[0];
  const none = t.calls === 0;

  return (
    <div className="ai-usage">
      <Hero data={data} days={days} setDays={setDays} priciest={priciest} cheapest={cheapest} />

      <MetricStrip className="mb-5" cols="grid-cols-2 lg:grid-cols-5">
        <Metric icon={Coins} iconColor="var(--cat-1)" label="Tokens" value={<Count value={t.tokens} fmt={tokens} />}
          footnote={<TokenSplit inT={t.generation.prompt_tokens} outT={t.generation.completion_tokens} embT={t.embedding_tokens} />} />
        <Metric icon={Activity} iconColor="var(--cat-2)" label="AI calls" value={<Count value={t.calls} fmt={(n) => nf.format(Math.round(n))} />}
          footnote={`${t.answered} answered · ${t.failed} fell back to the next model`} />
        <Metric icon={Check} iconColor="var(--good)" label="Actually spent" value={usd(t.actual_cost)}
          footnote={t.actual_cost === 0 ? "free models: the provider charged nothing" : "as charged by OpenRouter"} />
        <Metric icon={Gauge} iconColor="var(--cat-4)" label="Average answer time" value={secs(t.avg_latency_ms)}
          footnote={t.estimated_calls ? `${t.estimated_calls} call${t.estimated_calls === 1 ? "" : "s"} without counts: estimated` : "every count is the provider's own"} />
        <Metric icon={ArrowUpRight} iconColor="var(--cat-5)" label="At this pace" value={`${tokens(t.tokens_per_day * 30)}`}
          footnote={`tokens a month · ${tokens(t.tokens_per_day)} a day`} />
      </MetricStrip>

      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12">
          <ScenarioCard data={data} basis={basis} setBasis={setBasis} chosen={scenarios.map((s) => s.id)} onChange={save} loading={loading} />
        </div>
        <div className="col-span-12 xl:col-span-7">
          <ChartCard icon={ChartNoAxesColumn} title="Tokens, day by day" subtitle={`Last ${days === 1 ? "24 hours" : `${days} days`} · input, output and embeddings`}
            empty={none} emptyText="No AI calls in this period yet.">
            <DailyChart daily={data.daily} />
          </ChartCard>
        </div>
        <div className="col-span-12 xl:col-span-5">
          <ChartCard icon={Sparkles} title="Where the tokens go" subtitle="Share of all tokens, by the part of the app that asked" empty={none} emptyText="Nothing yet.">
            <FeatureDonut data={data} />
          </ChartCard>
        </div>
        <div className="col-span-12">
          <FeatureTable data={data} as={as} setPriceAs={setPriceAs} />
        </div>
        <div className="col-span-12 xl:col-span-7">
          <ModelsCard data={data} />
        </div>
        <div className="col-span-12 xl:col-span-5 space-y-4">
          <ProjectsCard data={data} />
          <VoiceCard data={data} />
        </div>
        <div className="col-span-12">
          <RecentCalls data={data} />
        </div>
        <div className="col-span-12">
          <HowCounted data={data} />
        </div>
      </div>
    </div>
  );
}

// ── hero ─────────────────────────────────────────────────────────────────────
function Hero({ data, days, setDays, priciest, cheapest }: { data: Usage; days: Range; setDays: (d: Range) => void; priciest?: Scenario; cheapest?: Scenario }) {
  const t = data.totals;
  return (
    <section className="ai-hero card-in relative mb-5 overflow-hidden rounded-[1.75rem] p-6 sm:p-8">
      <div className="ai-hero-glow" aria-hidden />
      <div className="relative flex flex-wrap items-start justify-between gap-4">
        <p className="eyebrow flex items-center gap-2 font-semibold text-accent"><Coins className="size-3.5" /> AI usage</p>
        <Segmented<Range> size="sm" label="Period" value={days} onChange={setDays}
          options={[{ value: 1, label: "24 h" }, { value: 7, label: "7 days" }, { value: 30, label: "30 days" }, { value: 90, label: "90 days" }]} />
      </div>
      <div className="relative mt-3 grid gap-6 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] lg:items-end">
        <div className="min-w-0">
          <h1 className="display text-[2.5rem] font-bold leading-[1.05] tracking-[-0.03em] text-ink sm:text-[3.25rem]">
            <span className="tabular-nums"><Count value={t.tokens} fmt={tokens} /></span>
            <span className="ml-2 text-[1.125rem] font-semibold tracking-normal text-ink-2 sm:text-[1.25rem]">tokens</span>
          </h1>
          <p className="mt-2 flex flex-wrap items-center gap-2 text-[0.9375rem] text-ink-2">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--good-soft)] px-2.5 py-0.5 text-[0.8125rem] font-semibold text-ink shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--good)_35%,transparent)]">
              <Check className="size-3.5 text-[var(--good)]" />{usd(t.actual_cost)} spent
            </span>
            across <b className="font-semibold text-ink">{nf.format(t.calls)} calls</b> in the last {days === 1 ? "24 hours" : `${days} days`}
          </p>
          <p className="mt-3 max-w-xl text-[0.8125rem] leading-6 text-ink-3">
            Real token counts, as each provider reported them for each call: incident write-ups, triage, questions, charts, data-quality notes,
            the morning report. Never the prompts or the answers.
          </p>
        </div>
        {priciest && cheapest ? (
          <div className="ai-whatif min-w-0 rounded-2xl p-4 sm:p-5">
            <p className="text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-ink-3">The same work, paid for</p>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="display text-[2rem] font-bold leading-none tabular-nums text-ink"><Count value={priciest.cost.total} fmt={usd} /></span>
              <span className="min-w-0 truncate text-sm text-ink-2">on <ProviderDot p={priciest.provider} /> {shortName(priciest)}</span>
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-[1.125rem] font-semibold tabular-nums text-ink"><Count value={cheapest.cost.total} fmt={usd} /></span>
              <span className="min-w-0 truncate text-[0.8125rem] text-ink-2">on <ProviderDot p={cheapest.provider} /> {shortName(cheapest)}</span>
            </div>
            <p className="mt-2.5 text-[0.71875rem] text-ink-3">
              About <b className="text-ink-2">{usd(priciest.per_month)}</b> a month at this pace on {priciest.provider_name}'s top model ·
              live prices for {data.prices.count} models{data.prices.fetched_at ? `, updated ${ago(data.prices.fetched_at)}` : ""}
            </p>
          </div>
        ) : null}
      </div>
    </section>
  );
}

const ProviderDot = ({ p, className }: { p: string; className?: string }) => (
  <span className={cn("inline-block size-2.5 shrink-0 rounded-full align-middle", className)} style={{ background: providerColor(p), boxShadow: `0 0 0 2px color-mix(in srgb, ${providerColor(p)} 25%, transparent)` }} aria-hidden />
);

function TokenSplit({ inT, outT, embT }: { inT: number; outT: number; embT: number }) {
  const all = inT + outT + embT || 1;
  return (
    <span className="block">
      <span className="flex h-1.5 w-full gap-[2px] overflow-hidden rounded-full" aria-hidden>
        {[inT, outT, embT].map((v, i) => v > 0 && <span key={i} className="h-full rounded-full" style={{ width: `${(v / all) * 100}%`, background: TOKEN_KIND[i]!.color }} />)}
      </span>
      <span className="mt-1 block">{tokens(inT)} in · {tokens(outT)} out{embT ? ` · ${tokens(embT)} embed` : ""}</span>
    </span>
  );
}

// ── scenarios ────────────────────────────────────────────────────────────────
function ScenarioCard({ data, basis, setBasis, chosen, onChange, loading }: {
  data: Usage; basis: Basis; setBasis: (b: Basis) => void; chosen: string[]; onChange: (ids: string[]) => void; loading: boolean;
}) {
  const [hover, setHover] = useState<{ s: Scenario; x: number; y: number } | null>(null);
  const value = (s: Scenario) => (basis === "period" ? s.cost.total : basis === "month" ? s.per_month : s.per_1k_calls ?? 0);
  const rows = [...data.scenarios].sort((a, b) => value(a) - value(b));
  const max = Math.max(...rows.map(value), 0.000001);
  const label = basis === "period" ? `for the last ${data.days === 1 ? "24 hours" : `${data.days} days`}` : basis === "month" ? "a month, at this pace" : "per 1,000 answers";
  return (
    <ChartCard icon={Coins} title="What the same work would cost elsewhere"
      subtitle={`Every token the bot used, priced at each model's live price ${label}. Free now: you paid ${usd(data.totals.actual_cost)}.`}
      actions={<ModelPicker chosen={chosen} onAdd={(id) => onChange([...chosen, id])} />}
      toolbar={<div className="flex flex-wrap items-center gap-2">
        <Segmented<Basis> size="sm" label="Show" value={basis} onChange={setBasis}
          options={[{ value: "period", label: "This period" }, { value: "month", label: "Per month at this pace" }, { value: "per1k", label: "Per 1,000 answers" }]} />
        {loading && <Loader2 className="size-3.5 animate-spin text-ink-3" aria-label="Updating" />}
      </div>}
      empty={!rows.length} emptyText={data.prices.error ?? "No models to compare yet: add one."}>
      <ul className="ai-scen space-y-1.5" onMouseLeave={() => setHover(null)}>
        <li className="ai-scen-row is-now">
          <span className="ai-scen-name"><span className="inline-block size-2.5 rounded-full bg-[var(--good)]" aria-hidden /><b>What you use now</b><span className="text-ink-3">free models</span></span>
          <span className="ai-scen-bar"><span className="ai-scen-fill" style={{ width: "0.6%", background: "var(--good)" }} /></span>
          <span className="ai-scen-val">{usd(basis === "period" ? data.totals.actual_cost : 0)}</span>
          <span className="w-7" />
        </li>
        {rows.map((s, i) => {
          const v = value(s);
          return (
            <li key={s.id} className="ai-scen-row group" onMouseMove={(e) => setHover({ s, x: e.clientX, y: e.clientY })}>
              <span className="ai-scen-name">
                <ProviderDot p={s.provider} />
                <b className="truncate" title={s.id}>{shortName(s)}</b>
                <span className="hidden shrink-0 text-ink-3 sm:inline">{s.provider_name}</span>
                {i === 0 && rows.length > 1 && <span className="ai-chip good">cheapest</span>}
                {i === rows.length - 1 && rows.length > 1 && <span className="ai-chip">priciest</span>}
              </span>
              <span className="ai-scen-bar" aria-hidden>
                <span className="ai-scen-fill" style={{ width: `${Math.max(0.6, (v / max) * 100)}%`, background: providerColor(s.provider), animationDelay: `${i * 40}ms` }} />
              </span>
              <span className="ai-scen-val tabular-nums"><Count value={v} fmt={usd} /></span>
              <button type="button" onClick={() => onChange(rows.filter((x) => x.id !== s.id).map((x) => x.id))}
                className="flex size-7 items-center justify-center rounded-lg text-ink-3 opacity-0 transition-opacity hover:bg-surface-3 hover:text-ink focus-visible:opacity-100 group-hover:opacity-100" aria-label={`Remove ${s.name}`}>
                <X className="size-3.5" />
              </button>
            </li>
          );
        })}
      </ul>
      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-3 text-[0.6875rem] text-ink-3">
        {Array.from(new Set(rows.map((r) => r.provider))).map((p) => (
          <span key={p} className="inline-flex items-center gap-1.5"><ProviderDot p={p} />{rows.find((r) => r.provider === p)!.provider_name}</span>
        ))}
        <span className="ml-auto">Hover a bar for input, output and embedding costs</span>
      </div>
      <HoverCard at={hover}>
        {hover && (
          <div className="w-72">
            <p className="flex items-center gap-1.5 text-[0.8125rem] font-semibold text-ink"><ProviderDot p={hover.s.provider} />{shortName(hover.s)}<span className="font-normal text-ink-3">{hover.s.provider_name}</span></p>
            <p className="mt-0.5 text-[0.6875rem] text-ink-3">{perM(hover.s.input)} in · {perM(hover.s.output)} out per million tokens{hover.s.context ? ` · ${cf.format(hover.s.context)} context` : ""}</p>
            <dl className="mt-2 grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-[0.75rem]">
              <dt className="text-ink-2">Input ({tokens(data.totals.generation.prompt_tokens)} tokens)</dt><dd className="tabular-nums text-ink">{usd(hover.s.cost.input)}</dd>
              <dt className="text-ink-2">Output ({tokens(data.totals.generation.completion_tokens)} tokens)</dt><dd className="tabular-nums text-ink">{usd(hover.s.cost.output)}</dd>
              {data.totals.embedding_tokens > 0 && <><dt className="text-ink-2">Embeddings on {hover.s.embedding_model?.name ?? "–"}</dt><dd className="tabular-nums text-ink">{usd(hover.s.cost.embeddings)}</dd></>}
              <dt className="border-t border-line pt-0.5 font-semibold text-ink">This period</dt><dd className="border-t border-line pt-0.5 font-semibold tabular-nums text-ink">{usd(hover.s.cost.total)}</dd>
              <dt className="text-ink-2">A month at this pace</dt><dd className="tabular-nums text-ink">{usd(hover.s.per_month)}</dd>
              {hover.s.per_1k_calls != null && <><dt className="text-ink-2">Per 1,000 answers</dt><dd className="tabular-nums text-ink">{usd(hover.s.per_1k_calls)}</dd></>}
            </dl>
            {hover.s.batch && <p className="mt-1.5 text-[0.6875rem] text-ink-3">Batch (not real-time): {perM(hover.s.batch.input)} in · {perM(hover.s.batch.output)} out per million</p>}
          </div>
        )}
      </HoverCard>
    </ChartCard>
  );
}

function ModelPicker({ chosen, onAdd }: { chosen: string[]; onAdd: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PriceRow[] | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const id = setTimeout(() => { void api<{ models: PriceRow[] }>(`/api/ai/models?q=${encodeURIComponent(q)}`).then((r) => setRows(r.models)).catch(() => setRows([])); }, 160);
    return () => clearTimeout(id);
  }, [open, q]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", esc);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", esc); };
  }, [open]);
  return (
    <div ref={box} className="relative">
      <Button size="sm" variant="secondary" onClick={() => setOpen((o) => !o)} aria-expanded={open}><Plus />Compare a model</Button>
      {open && (
        <div className="anim-fade absolute right-0 top-10 z-[60] w-[22rem] max-w-[calc(100vw-2rem)] rounded-2xl border border-line-strong bg-surface p-2 shadow-float">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
            <Input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search any model: gpt, claude, gemini…" className="w-full pl-8" aria-label="Search models" />
          </div>
          <ul className="scrollbar-thin mt-1.5 max-h-80 overflow-y-auto">
            {rows === null ? <li className="px-2 py-3 text-xs text-ink-3">Loading live prices…</li>
              : !rows.length ? <li className="px-2 py-3 text-xs text-ink-3">No model matches.</li>
              : rows.map((m) => {
                const has = chosen.includes(m.id);
                return (
                  <li key={m.id}>
                    <button type="button" disabled={has} onClick={() => { onAdd(m.id); setOpen(false); }}
                      className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-surface-3 disabled:opacity-50">
                      <ProviderDot p={m.provider} />
                      <span className="min-w-0 flex-1"><span className="block truncate text-[0.8125rem] text-ink">{shortName(m)}</span>
                        <span className="block truncate text-[0.6875rem] text-ink-3">{m.provider_name} · {perM(m.input)} in · {perM(m.output)} out per M</span></span>
                      {has ? <Check className="size-3.5 text-accent" /> : <Plus className="size-3.5 text-ink-3" />}
                    </button>
                  </li>
                );
              })}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── daily tokens (stacked bars) ──────────────────────────────────────────────
function DailyChart({ daily }: { daily: Usage["daily"] }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const H = 220, pad = { l: 44, r: 8, t: 10, b: 24 };
  const totals = daily.map((d) => d.prompt_tokens + d.completion_tokens + d.embedding_tokens);
  const max = niceMax(Math.max(...totals, 1));
  const n = daily.length, iw = Math.max(0, w - pad.l - pad.r), step = n ? iw / n : 0, bw = Math.max(2, Math.min(28, step * 0.62));
  const y = (v: number) => pad.t + (H - pad.t - pad.b) * (1 - v / max);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((k) => k * max);
  const every = Math.ceil(n / Math.max(1, Math.floor(iw / 64)));
  return (
    <div ref={ref} className="relative">
      <Legend items={TOKEN_KIND.map((k) => ({ label: k.label, color: k.color }))} />
      {w > 0 && (
        <svg width={w} height={H} className="mt-2 block" role="img" aria-label="Tokens per day" onMouseLeave={() => setHover(null)}>
          {ticks.map((v, i) => (
            <g key={i}>
              <line x1={pad.l} x2={w - pad.r} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeDasharray={i ? "2 4" : undefined} />
              <text x={pad.l - 8} y={y(v) + 3.5} textAnchor="end" className="fill-[var(--ink-3)] text-[10px] tabular-nums">{tokens(v)}</text>
            </g>
          ))}
          {daily.map((d, i) => {
            const x = pad.l + i * step + (step - bw) / 2;
            let acc = 0;
            const segs = TOKEN_KIND.map((k) => ({ v: d[k.key], color: k.color })).filter((s) => s.v > 0);
            return (
              <g key={d.date} onMouseMove={(e) => setHover({ i, x: e.clientX, y: e.clientY })}>
                <rect x={pad.l + i * step} y={pad.t} width={step} height={H - pad.t - pad.b} fill="transparent" />
                {hover?.i === i && <rect x={pad.l + i * step} y={pad.t} width={step} height={H - pad.t - pad.b} fill="var(--row-hover)" rx={6} />}
                {segs.map((s, k) => {
                  const top = y(acc + s.v), bottom = y(acc);
                  acc += s.v;
                  const h = Math.max(0, bottom - top - (k < segs.length - 1 ? 2 : 0));
                  const last = k === segs.length - 1;
                  return <path key={k} className="ai-grow" style={{ animationDelay: `${i * 12}ms` }} fill={s.color}
                    d={last ? roundTop(x, top, bw, h, Math.min(4, bw / 2)) : `M${x},${top + (bottom - top - h)}h${bw}v${h}h${-bw}z`} />;
                })}
              </g>
            );
          })}
          {daily.map((d, i) => (i % every === 0 || i === n - 1) && (
            <text key={d.date} x={pad.l + i * step + step / 2} y={H - 6} textAnchor="middle" className="fill-[var(--ink-3)] text-[10px]">
              {new Date(`${d.date}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" })}
            </text>
          ))}
        </svg>
      )}
      <HoverCard at={hover}>
        {hover && daily[hover.i] && (() => {
          const d = daily[hover.i]!;
          return (
            <div className="w-56">
              <p className="text-[0.8125rem] font-semibold text-ink">{new Date(`${d.date}T00:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}</p>
              <dl className="mt-1.5 grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-[0.75rem]">
                {TOKEN_KIND.map((k) => <Row key={k.key} color={k.color} label={k.label} value={tokens(d[k.key])} />)}
                <dt className="border-t border-line pt-0.5 text-ink-2">{d.calls} call{d.calls === 1 ? "" : "s"}</dt>
                <dd className="border-t border-line pt-0.5 font-semibold tabular-nums text-ink">{tokens(d.prompt_tokens + d.completion_tokens + d.embedding_tokens)}</dd>
              </dl>
            </div>
          );
        })()}
      </HoverCard>
    </div>
  );
}

const Row = ({ color, label, value }: { color: string; label: string; value: string }) => (
  <><dt className="flex items-center gap-1.5 text-ink-2"><span className="size-2 rounded-sm" style={{ background: color }} />{label}</dt><dd className="tabular-nums text-ink">{value}</dd></>
);

function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.71875rem] text-ink-2">
      {items.map((i) => <span key={i.label} className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-[3px]" style={{ background: i.color }} />{i.label}</span>)}
    </div>
  );
}

const roundTop = (x: number, y: number, w: number, h: number, r: number) =>
  h <= 0 ? "" : `M${x},${y + h}V${y + Math.min(r, h)}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + Math.min(r, h)}V${y + h}Z`;

function niceMax(v: number) {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}

// ── by feature ───────────────────────────────────────────────────────────────
function FeatureDonut({ data }: { data: Usage }) {
  const feats = data.features.filter((f) => f.tokens > 0);
  const all = feats.reduce((a, f) => a + f.tokens, 0) || 1;
  const [hover, setHover] = useState<string | null>(null);
  const R = 82, r = 56, C = 100;
  let a0 = -Math.PI / 2;
  const arcs = feats.map((f) => {
    const a1 = a0 + (f.tokens / all) * Math.PI * 2;
    const gap = feats.length > 1 ? 0.012 : 0;
    const path = arc(C, C, R, r, a0 + gap, a1 - gap);
    a0 = a1;
    return { f, path };
  });
  const focus = feats.find((f) => f.key === hover);
  return (
    <div className="flex flex-col items-center gap-5 sm:flex-row">
      <svg viewBox="0 0 200 200" className="size-48 shrink-0" role="img" aria-label="Tokens by feature">
        {arcs.map(({ f, path }) => (
          <path key={f.key} d={path} fill={featureColor(f.key)} className="ai-arc" opacity={hover && hover !== f.key ? 0.28 : 1}
            onMouseEnter={() => setHover(f.key)} onMouseLeave={() => setHover(null)} />
        ))}
        <text x={C} y={C - 4} textAnchor="middle" className="fill-[var(--ink)] text-[22px] font-bold tabular-nums">{tokens(focus ? focus.tokens : all)}</text>
        <text x={C} y={C + 15} textAnchor="middle" className="fill-[var(--ink-3)] text-[10px]">{focus ? `${Math.round((focus.tokens / all) * 100)}% · ${focus.label}` : "tokens"}</text>
      </svg>
      <ul className="w-full min-w-0 space-y-1.5">
        {feats.map((f) => {
          const I = FEATURE_ICON[f.key] ?? Cpu;
          return (
            <li key={f.key} onMouseEnter={() => setHover(f.key)} onMouseLeave={() => setHover(null)}
              className={cn("flex items-center gap-2 rounded-lg px-1.5 py-1 text-[0.8125rem] transition-colors", hover === f.key && "bg-surface-2")}>
              <span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: featureColor(f.key) }} />
              <I className="size-3.5 shrink-0 text-ink-3" aria-hidden />
              <span className="min-w-0 flex-1 truncate text-ink">{f.label}</span>
              <span className="shrink-0 tabular-nums text-ink-2">{Math.round((f.tokens / all) * 100)}%</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function arc(cx: number, cy: number, R: number, r: number, a0: number, a1: number) {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const p = (rad: number, a: number) => `${cx + rad * Math.cos(a)},${cy + rad * Math.sin(a)}`;
  if (a1 - a0 >= Math.PI * 2 - 0.001) return `M${cx - R},${cy}A${R},${R} 0 1 1 ${cx + R},${cy}A${R},${R} 0 1 1 ${cx - R},${cy}M${cx - r},${cy}A${r},${r} 0 1 0 ${cx + r},${cy}A${r},${r} 0 1 0 ${cx - r},${cy}Z`;
  return `M${p(R, a0)}A${R},${R} 0 ${large} 1 ${p(R, a1)}L${p(r, a1)}A${r},${r} 0 ${large} 0 ${p(r, a0)}Z`;
}

function FeatureTable({ data, as, setPriceAs }: { data: Usage; as?: Scenario; setPriceAs: (id: string) => void }) {
  const all = data.features.reduce((a, f) => a + f.tokens, 0) || 1;
  return (
    <ChartCard flush icon={Cpu} title="Every part of the app that uses AI" subtitle="Calls, tokens, speed and what each would cost on the model you choose"
      actions={data.scenarios.length ? (
        <label className="flex items-center gap-2 text-xs text-ink-2">Price as
          <select value={as?.id ?? ""} onChange={(e) => setPriceAs(e.target.value)}
            className="h-8 max-w-[14rem] rounded-lg border border-line-strong bg-surface-2 px-2 text-xs text-ink light:bg-surface">
            {data.scenarios.map((s) => <option key={s.id} value={s.id}>{shortName(s)} ({s.provider_name})</option>)}
          </select>
        </label>
      ) : undefined}
      empty={!data.features.length} emptyText="No AI calls yet.">
      <div className="scrollbar-thin overflow-x-auto">
        <table className="data-table min-w-[52rem]">
          <thead><tr><th>Feature</th><th className="text-right">Calls</th><th className="text-right">Input</th><th className="text-right">Output</th><th>Share of tokens</th><th className="text-right">Avg time</th><th className="text-right">{as ? `On ${shortName(as)}` : "Cost"}</th></tr></thead>
          <tbody>
            {data.features.map((f) => {
              const I = FEATURE_ICON[f.key] ?? Cpu;
              const share = f.tokens / all;
              return (
                <tr key={f.key}>
                  <td>
                    <span className="flex items-start gap-2.5">
                      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg" style={{ background: `color-mix(in srgb, ${featureColor(f.key)} 16%, transparent)` }}>
                        <I className="size-3.5" style={{ color: featureColor(f.key) }} aria-hidden />
                      </span>
                      <span className="min-w-0"><span className="block font-semibold text-ink">{f.label}</span><span className="block max-w-[22rem] text-[0.6875rem] leading-4 text-ink-3">{f.hint}</span></span>
                    </span>
                  </td>
                  <td className="text-right tabular-nums">{nf.format(f.calls)}{f.failed ? <span className="block text-[0.6875rem] text-ink-3">{f.failed} fell back</span> : null}</td>
                  <td className="text-right tabular-nums">{tokens(f.prompt_tokens + f.embedding_tokens)}</td>
                  <td className="text-right tabular-nums">{tokens(f.completion_tokens)}</td>
                  <td>
                    <span className="flex items-center gap-2">
                      <span className="h-1.5 w-28 overflow-hidden rounded-full bg-surface-3"><span className="ai-grow-x block h-full rounded-full" style={{ width: `${Math.max(1, share * 100)}%`, background: featureColor(f.key) }} /></span>
                      <span className="tabular-nums text-ink-2">{(share * 100).toFixed(share < 0.1 ? 1 : 0)}%</span>
                    </span>
                  </td>
                  <td className="text-right tabular-nums">{secs(f.avg_latency_ms)}</td>
                  <td className="text-right font-semibold tabular-nums text-ink">{f.key === "voice" ? <span className="font-normal text-ink-3">billed per minute</span> : as && f.cost[as.id] != null ? usd(f.cost[as.id]!) : "–"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </ChartCard>
  );
}

// ── models, projects, voice ──────────────────────────────────────────────────
function ModelsCard({ data }: { data: Usage }) {
  const max = Math.max(...data.models.map((m) => m.tokens), 1);
  return (
    <ChartCard icon={Bot} title="Models that answered" subtitle="Tried in order; a model that's rate-limited or down is rested and the next one answers"
      empty={!data.models.length} emptyText="No calls yet.">
      <ul className="space-y-3">
        {data.models.map((m) => (
          <li key={m.model}>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate font-mono text-[0.78125rem] text-ink" title={m.model}>{m.model}</span>
                {m.free && <span className="ai-chip good">free</span>}
              </span>
              <span className="text-[0.71875rem] tabular-nums text-ink-2">{tokens(m.tokens)} tokens · {m.answered}/{m.calls} answered · {secs(m.avg_latency_ms)}</span>
            </div>
            <div className="mt-1.5 flex h-1.5 gap-[2px] overflow-hidden rounded-full bg-surface-3" aria-hidden>
              <span className="ai-grow-x h-full rounded-full bg-[var(--cat-1)]" style={{ width: `${(m.prompt_tokens + m.embedding_tokens) / max * 100}%` }} />
              <span className="ai-grow-x h-full rounded-full bg-[var(--cat-2)]" style={{ width: `${m.completion_tokens / max * 100}%` }} />
            </div>
          </li>
        ))}
      </ul>
    </ChartCard>
  );
}

function ProjectsCard({ data }: { data: Usage }) {
  const rows = [...data.projects].sort((a, b) => b.tokens - a.tokens);
  const all = rows.reduce((a, p) => a + p.tokens, 0) || 1;
  return (
    <ChartCard icon={Activity} title="By project" subtitle="Calls with no project are questions and voice, asked across everything" empty={!rows.length} emptyText="No calls yet.">
      <ul className="space-y-2">
        {rows.map((p) => (
          <li key={p.project ?? "-"} className="text-[0.8125rem]">
            <div className="flex items-baseline justify-between gap-2"><span className="truncate text-ink">{p.project ?? "Across projects"}</span><span className="shrink-0 tabular-nums text-ink-2">{tokens(p.tokens)} · {p.calls} calls</span></div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-3"><span className="ai-grow-x block h-full rounded-full bg-accent" style={{ width: `${Math.max(1, p.tokens / all * 100)}%` }} /></div>
          </li>
        ))}
      </ul>
    </ChartCard>
  );
}

function VoiceCard({ data }: { data: Usage }) {
  const v = data.totals.voice;
  return (
    <div className="flex items-start gap-3 rounded-2xl border border-line bg-surface p-4 shadow-card">
      <span className="flex size-9 shrink-0 items-center justify-center rounded-xl" style={{ background: `color-mix(in srgb, ${featureColor("voice")} 16%, transparent)` }}><Mic className="size-4" style={{ color: featureColor("voice") }} /></span>
      <div className="min-w-0 text-[0.8125rem]">
        <p className="font-semibold text-ink">Robot voice</p>
        <p className="mt-0.5 text-ink-2">{v.calls ? `${v.calls} clip${v.calls === 1 ? "" : "s"} · ${v.audio_seconds ? `${v.audio_seconds.toFixed(0)} s of audio` : "length unknown"} · ${nf.format(v.characters)} characters read` : "Not used in this period."}</p>
        <p className="mt-1 text-[0.6875rem] text-ink-3">Voice is billed per minute of audio, not per token, so it's counted here and left out of the dollar comparison.</p>
      </div>
    </div>
  );
}

// ── recent calls ─────────────────────────────────────────────────────────────
const STATUS: Record<string, { label: string; color: string }> = {
  ok: { label: "Answered", color: "var(--good)" }, retry: { label: "Answered on retry", color: "var(--good)" },
  empty: { label: "Empty answer", color: "var(--warning)" }, error: { label: "Failed", color: "var(--critical)" },
};

function RecentCalls({ data }: { data: Usage }) {
  return (
    <ChartCard flush icon={Activity} title="Every call, live" subtitle="The latest 60 · refreshes every 15 seconds · ≈ means the provider didn't report counts, so they're estimated"
      empty={!data.recent.length} emptyText="No AI calls yet. They appear here the moment the bot makes one.">
      <div className="scrollbar-thin max-h-[26rem] overflow-auto">
        <table className="data-table min-w-[58rem]">
          <thead><tr><th>When</th><th>Feature</th><th>Model</th><th>Result</th><th className="text-right">In</th><th className="text-right">Out</th><th className="text-right">Time</th><th>Project</th></tr></thead>
          <tbody>
            {data.recent.map((c, i) => {
              const s = STATUS[c.status] ?? STATUS.error!;
              const I = FEATURE_ICON[c.feature] ?? Cpu;
              return (
                <tr key={`${c.at}-${i}`} className={cn(i === 0 && "ai-new")}>
                  <td className="whitespace-nowrap tabular-nums text-ink-2" title={new Date(c.at).toString()}>{time(c.at)}<span className="ml-1.5 text-[0.6875rem] text-ink-3">{ago(c.at)}</span></td>
                  <td className="whitespace-nowrap"><span className="inline-flex items-center gap-1.5 text-ink"><I className="size-3.5" style={{ color: featureColor(c.feature) }} />{data.features_catalog[c.feature]?.label ?? c.feature}</span></td>
                  <td className="max-w-[16rem] truncate font-mono text-[0.71875rem] text-ink-2" title={c.served_model ?? c.model}>{c.served_model ?? c.model}</td>
                  <td className="whitespace-nowrap"><span className="inline-flex items-center gap-1.5 text-[0.75rem]" title={c.error ?? undefined}><span className="size-1.5 rounded-full" style={{ background: s.color }} />{s.label}{c.http_status && c.status === "error" ? ` (${c.http_status})` : ""}</span></td>
                  <td className="text-right tabular-nums">{c.kind === "speech" ? "–" : `${c.estimated ? "≈" : ""}${tokens(c.prompt_tokens)}`}</td>
                  <td className="text-right tabular-nums">{c.kind === "speech" ? (c.audio_seconds ? `${c.audio_seconds.toFixed(1)} s` : "audio") : c.kind === "embed" ? "–" : `${c.estimated ? "≈" : ""}${tokens(c.completion_tokens)}`}</td>
                  <td className="text-right tabular-nums text-ink-2">{secs(c.latency_ms)}</td>
                  <td className="truncate text-ink-3">{c.project ?? "–"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </ChartCard>
  );
}

function HowCounted({ data }: { data: Usage }) {
  return (
    <section className="rounded-2xl border border-line bg-surface p-5 text-[0.8125rem] leading-6 text-ink-2 shadow-card">
      <p className="mb-2 flex items-center gap-2 font-semibold text-ink"><Info className="size-4 text-accent" />How this is counted</p>
      <ul className="grid gap-x-8 gap-y-1.5 md:grid-cols-2">
        <li><b className="text-ink">Real tokens.</b> Each call asks OpenRouter for its usage, and the input and output tokens it reports are stored per call. Every attempt counts, including the ones that failed over to the next model.</li>
        <li><b className="text-ink">Real cost.</b> What OpenRouter charged. The free models charge $0, so the actual spend is $0.</li>
        <li><b className="text-ink">Same work, other models.</b> The same input and output tokens at each model's live price from OpenRouter's catalogue{data.prices.fetched_at ? `, updated ${ago(data.prices.fetched_at)}` : ""}. Embeddings use the provider's own embedding model (Voyage AI for Anthropic, which recommends it).</li>
        <li><b className="text-ink">Good to know.</b> Each provider's tokenizer counts the same text a little differently (often within 10–20%). Reasoning ("thinking") tokens are off in this app, so none are billed.</li>
        <li><b className="text-ink">Private.</b> Only counts, timings and the model's name are kept, never a prompt or an answer.</li>
        <li><b className="text-ink">Pace.</b> "A month at this pace" scales this period's usage to 30 days, from the first call in the period.</li>
      </ul>
      {data.prices.error && <p className="mt-2 flex items-center gap-1.5 text-[var(--warning-ink)]"><ArrowDownRight className="size-3.5" />{data.prices.error}</p>}
    </section>
  );
}
