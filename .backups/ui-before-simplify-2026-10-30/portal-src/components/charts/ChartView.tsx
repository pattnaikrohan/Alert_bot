// A chart someone asked the bot for, drawn from the bot's own numbers: a number card, bars (vertical or horizontal),
// a line or area, a pie or donut, a gauge, or a table. Categorical colours are a validated 8-hue order (CVD-safe on
// adjacent pairs, light and dark); every slice is also labelled in the legend, so colour is never the only cue.
import { TrendingDown, TrendingUp, X } from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface ChartSpec {
  title: string; viz: "stat" | "bar" | "hbar" | "line" | "area" | "pie" | "donut" | "gauge" | "table"; metric: string; period_days: number;
  group_by: string; filters: { pipeline?: string | null; table?: string | null; column?: string | null; status?: string | null; severity?: string | null };
}
export interface ChartData {
  value: number | null; previous: number | null; unit: string; label: string; period_days: number;
  series: { label: string; value: number | null }[];
}

/** Metrics where a rise is bad news, and where it's good news (the delta's colour). Red bars only for counts of trouble. */
const UP_IS_BAD = new Set(["failed_runs", "incidents_opened", "open_incidents", "checks_failed", "avg_duration_min", "max_duration_min",
  "time_to_resolve_h", "empty_pct", "freshness_h"]);
const UP_IS_GOOD = new Set(["success_rate", "succeeded_runs", "incidents_resolved"]);
const PROBLEMS = new Set(["failed_runs", "incidents_opened", "open_incidents", "checks_failed"]);
const CAT = (i: number) => `var(--cat-${(i % 8) + 1})`;

export function fmtValue(v: number | null | undefined, unit: string): string {
  if (v == null) return "–";
  if (unit === "%") return `${v.toFixed(v >= 10 || v === 0 ? 0 : 1)}%`;
  if (unit === "min") return v >= 90 ? `${(v / 60).toFixed(1)} h` : `${v.toFixed(v < 10 ? 1 : 0)} min`;
  if (unit === "h") return v >= 48 ? `${(v / 24).toFixed(1)} days` : `${v.toFixed(1)} h`;
  const a = Math.abs(v);
  return a >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : a >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : a >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1);
}

const GROUP: Record<string, string> = { day: "per day", week: "per week", month: "per month", pipeline: "by pipeline", table: "by table", status: "by status", check: "by check", severity: "by severity", none: "" };

export function describe(spec: ChartSpec): string {
  const f = spec.filters ?? {};
  const bits = [spec.metric === "open_incidents" ? "right now" : spec.period_days === 1 ? "last 24 hours" : `last ${spec.period_days} days`, GROUP[spec.group_by] ?? ""];
  for (const v of [f.pipeline, f.table && f.column ? `${f.table.split(".").pop()}.${f.column}` : f.table?.split(".").pop(), f.status, f.severity]) if (v) bits.push(v);
  return bits.filter(Boolean).join(" · ");
}

export function ChartView({ spec, data, onRemove, compact, className }: {
  spec: ChartSpec; data: ChartData | null; onRemove?: () => void; compact?: boolean; className?: string;
}) {
  const color = PROBLEMS.has(spec.metric) ? "var(--critical)" : "var(--accent)";
  const body: ReactNode = !data ? <p className="text-sm text-ink-3">This chart can't be shown right now.</p>
    : spec.viz === "stat" ? <Stat spec={spec} data={data} />
    : spec.viz === "gauge" ? <Gauge spec={spec} data={data} />
    : spec.viz === "pie" || spec.viz === "donut" ? <Pie data={data} donut={spec.viz === "donut"} />
    : spec.viz === "line" || spec.viz === "area" ? <Line data={data} color={color} area={spec.viz === "area"} />
    : spec.viz === "table" ? <Table data={data} />
    : spec.viz === "hbar" ? <HBars data={data} color={color} />
    : <VBars data={data} color={color} />;
  return (
    <section className={cn("chart-card group relative flex min-w-0 flex-col overflow-hidden rounded-[1.25rem] border border-line bg-surface shadow-card", compact ? "p-4" : "p-5", className)}>
      <span className="chart-glow" style={{ background: `radial-gradient(60% 80% at 100% 0%, color-mix(in srgb, ${color} 10%, transparent), transparent 70%)` }} aria-hidden />
      <header className="relative flex items-start gap-2 pr-7">
        <div className="min-w-0">
          <h3 className="dq-display truncate text-[0.96875rem] text-ink">{spec.title}</h3>
          <p className="truncate text-xs text-ink-3">{describe(spec)}</p>
        </div>
      </header>
      {onRemove && (
        <button type="button" onClick={onRemove} aria-label={`Remove ${spec.title}`} title="Remove from your overview"
          className="absolute right-3 top-3 z-10 flex size-7 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink focus-visible:bg-surface-3">
          <X className="size-4" />
        </button>
      )}
      <div className="relative mt-3 min-h-0 flex-1">{body}</div>
    </section>
  );
}

// ── number card ──
function Stat({ spec, data }: { spec: ChartSpec; data: ChartData }) {
  const delta = data.value != null && data.previous != null ? data.value - data.previous : null;
  const good = delta == null || delta === 0 ? null : (delta > 0) === UP_IS_GOOD.has(spec.metric) || (delta < 0 && UP_IS_BAD.has(spec.metric));
  const neutral = delta != null && delta !== 0 && !UP_IS_BAD.has(spec.metric) && !UP_IS_GOOD.has(spec.metric);
  const Icon = delta != null && delta < 0 ? TrendingDown : TrendingUp;
  return (
    <div className="flex h-full flex-col justify-between gap-4">
      <div>
        <p className="dq-num chart-pop text-[3rem] font-bold leading-none tracking-tight text-ink">{fmtValue(data.value, data.unit)}</p>
        <p className="mt-1.5 text-sm text-ink-3">{["%", "min", "h"].includes(data.unit) ? data.label.toLowerCase() : data.unit}</p>
      </div>
      {delta != null && (
        <p className={cn("inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold",
          delta === 0 || neutral ? "bg-surface-2 text-ink-2" : good ? "bg-[var(--good-soft)] text-[var(--good-ink)]" : "bg-[var(--critical-soft)] text-[var(--critical-ink)]")}>
          <Icon className="size-3.5" />
          {delta === 0 ? "Same as" : `${delta > 0 ? "+" : "−"}${fmtValue(Math.abs(delta), data.unit)} vs`} the previous {data.period_days} days ({fmtValue(data.previous, data.unit)})
        </p>
      )}
    </div>
  );
}

// ── gauge (percentages) ──
function Gauge({ spec, data }: { spec: ChartSpec; data: ChartData }) {
  const id = useId();
  const v = Math.max(0, Math.min(100, data.value ?? 0));
  const goodHigh = !UP_IS_BAD.has(spec.metric);
  const score = goodHigh ? v : 100 - v;
  const tone = score >= 90 ? "var(--good)" : score >= 70 ? "var(--warning)" : "var(--critical)";
  const r = 80, cx = 100, cy = 96, len = Math.PI * r;
  const arc = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`;
  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 200 112" className="w-full max-w-[17rem]" role="img" aria-label={`${data.label}: ${fmtValue(data.value, "%")}`}>
        <defs><linearGradient id={`${id}g`} x1="0" x2="1"><stop offset="0" stopColor={tone} stopOpacity="0.55" /><stop offset="1" stopColor={tone} /></linearGradient></defs>
        <path d={arc} fill="none" stroke="var(--surface-3)" strokeWidth="16" strokeLinecap="round" />
        <path d={arc} fill="none" stroke={`url(#${id}g)`} strokeWidth="16" strokeLinecap="round" strokeDasharray={`${(v / 100) * len} ${len}`} className="chart-gauge" />
        <text x={cx} y={cy - 14} textAnchor="middle" className="dq-num" style={{ fill: "var(--ink)", fontSize: 34, fontWeight: 700 }}>{fmtValue(data.value, "%")}</text>
        <text x={cx - r} y={cy + 14} textAnchor="middle" style={{ fill: "var(--ink-3)", fontSize: 10 }}>0%</text>
        <text x={cx + r} y={cy + 14} textAnchor="middle" style={{ fill: "var(--ink-3)", fontSize: 10 }}>100%</text>
      </svg>
      {data.previous != null && <p className="mt-1 text-xs text-ink-3">previous {data.period_days} days: {fmtValue(data.previous, "%")}</p>}
    </div>
  );
}

// ── vertical bars ──
function VBars({ data, color }: { data: ChartData; color: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const vals = data.series.map((s) => s.value ?? 0);
  const max = niceMax(Math.max(...vals, 0));
  const n = data.series.length;
  const categorical = n > 0 && !/^\d{2} \w{3}$|^wk |^\w{3} \d{4}$/.test(data.series[0]!.label);
  const marks = categorical ? data.series.map((_, i) => i) : n <= 1 ? [0] : [0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1];
  if (!n) return <p className="text-sm text-ink-3">Nothing in this period.</p>;
  return (
    <div>
      <div className="relative flex h-40 gap-2">
        <div className="flex w-12 shrink-0 flex-col justify-between whitespace-nowrap text-right text-[0.625rem] text-ink-3"><span>{fmtValue(max, data.unit)}</span><span>{fmtValue(max / 2, data.unit)}</span><span>0</span></div>
        <div className="relative min-w-0 flex-1">
          {[0, 0.5, 1].map((g) => <span key={g} className="absolute inset-x-0 border-t border-dashed border-line" style={{ top: `${g * 100}%` }} />)}
          <div className="absolute inset-0 flex items-end" style={{ gap: n > 40 ? 1 : n > 20 ? 2 : 4 }} onMouseLeave={() => setHover(null)}>
            {data.series.map((s, i) => {
              const h = ((s.value ?? 0) / max) * 100;
              return (
                <div key={i} className="relative flex h-full min-w-0 flex-1 cursor-default items-end" onMouseEnter={() => setHover(i)}>
                  <div className="chart-grow w-full rounded-t-md transition-[filter,opacity] duration-150"
                    style={{ height: `${Math.max(s.value ? 1.5 : 0, h)}%`, background: `linear-gradient(180deg, color-mix(in srgb, ${color} 72%, white), ${color})`,
                      opacity: hover == null || hover === i ? 1 : 0.45, animationDelay: `${Math.min(i, 30) * 12}ms` }} />
                  {categorical && n <= 8 && s.value != null && <span className="dq-num absolute inset-x-0 text-center text-[0.6875rem] font-semibold text-ink" style={{ bottom: `calc(${h}% + 4px)` }}>{fmtValue(s.value, data.unit)}</span>}
                </div>
              );
            })}
          </div>
          {hover != null && <Tip x={((hover + 0.5) / n) * 100} label={data.series[hover]!.label} value={fmtValue(data.series[hover]!.value, data.unit)} />}
        </div>
      </div>
      <div className="relative ml-14 mt-1.5 h-4 text-[0.625rem] text-ink-3">
        {[...new Set(marks)].map((i) => (
          <span key={i} className="absolute max-w-[6rem] truncate whitespace-nowrap" title={data.series[i]?.label}
            style={{ left: `${((i + 0.5) / n) * 100}%`, transform: `translateX(${!categorical && i === 0 ? "-15%" : !categorical && i === n - 1 ? "-85%" : "-50%"})` }}>
            {data.series[i]?.label}
          </span>
        ))}
      </div>
      <Foot data={data} />
    </div>
  );
}

// ── horizontal bars (rankings) ──
function HBars({ data, color }: { data: ChartData; color: string }) {
  const max = Math.max(1e-9, ...data.series.map((s) => s.value ?? 0));
  const total = data.series.reduce((a, s) => a + (s.value ?? 0), 0);
  if (!data.series.length) return <p className="text-sm text-ink-3">Nothing in this period.</p>;
  return (
    <div>
      <ul className="space-y-2.5">
        {data.series.map((s, i) => (
          <li key={s.label} className="text-xs">
            <div className="mb-1 flex items-baseline gap-2"><span className="min-w-0 flex-1 truncate font-medium text-ink" title={s.label}>{s.label}</span>
              <span className="dq-num font-semibold text-ink">{fmtValue(s.value, data.unit)}</span>
              {!["%", "min", "h"].includes(data.unit) && total > 0 && <span className="w-9 text-right text-ink-3">{Math.round(((s.value ?? 0) / total) * 100)}%</span>}</div>
            <span className="block h-2.5 overflow-hidden rounded-full bg-surface-3">
              <span className="chart-grow-x block h-full rounded-full" style={{ width: `${Math.max(s.value ? 2 : 0, ((s.value ?? 0) / max) * 100)}%`, animationDelay: `${i * 40}ms`,
                background: `linear-gradient(90deg, color-mix(in srgb, ${color} 65%, white), ${color})` }} />
            </span>
          </li>
        ))}
      </ul>
      <Foot data={data} />
    </div>
  );
}

// ── line / area, with a crosshair ──
function smooth(pts: [number, number][]): string {
  if (pts.length < 2) return pts.length ? `M${pts[0]![0]},${pts[0]![1]}` : "";
  let d = `M${pts[0]![0]},${pts[0]![1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] ?? pts[i]!, p1 = pts[i]!, p2 = pts[i + 1]!, p3 = pts[i + 2] ?? p2;
    // control points kept between the two points' heights: the curve never overshoots (no dips below zero)
    const lo = Math.min(p1[1], p2[1]), hi = Math.max(p1[1], p2[1]);
    const clamp = (y: number) => Math.min(hi, Math.max(lo, y));
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, clamp(p1[1] + (p2[1] - p0[1]) / 6)], c2 = [p2[0] - (p3[0] - p1[0]) / 6, clamp(p2[1] - (p3[1] - p1[1]) / 6)];
    d += ` C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${p2[0]},${p2[1]}`;
  }
  return d;
}

function Line({ data, color, area }: { data: ChartData; color: string; area: boolean }) {
  const id = useId();
  const [hover, setHover] = useState<number | null>(null);
  const n = data.series.length;
  const known = data.series.map((s, i) => ({ i, v: s.value })).filter((p) => p.v != null) as { i: number; v: number }[];
  const max = niceMax(Math.max(0, ...known.map((p) => p.v)));
  const W = 300, H = 140;
  const xy = (i: number, v: number): [number, number] => [n > 1 ? (i / (n - 1)) * W : W / 2, H - (v / max) * H];
  const pts = known.map((p) => xy(p.i, p.v));
  const path = useMemo(() => smooth(pts), [pts.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!known.length) return <p className="text-sm text-ink-3">Nothing in this period.</p>;
  const last = known[known.length - 1]!;
  const hv = hover != null ? data.series[hover] : null;
  return (
    <div>
      <div className="flex gap-2">
        <div className="flex h-40 w-12 shrink-0 flex-col justify-between whitespace-nowrap text-right text-[0.625rem] text-ink-3"><span>{fmtValue(max, data.unit)}</span><span>{fmtValue(max / 2, data.unit)}</span><span>0</span></div>
        <div className="relative h-40 min-w-0 flex-1"
          onMouseMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); setHover(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1))))); }}
          onMouseLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible" aria-label={`${data.label} over time`} role="img">
            <defs><linearGradient id={`${id}a`} x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor={color} stopOpacity={area ? 0.42 : 0.18} /><stop offset="1" stopColor={color} stopOpacity="0" /></linearGradient></defs>
            {[0, 0.5, 1].map((g) => <line key={g} x1="0" x2={W} y1={H * g} y2={H * g} stroke="var(--line)" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />)}
            <path d={`${path} L${pts[pts.length - 1]![0]},${H} L${pts[0]![0]},${H} Z`} fill={`url(#${id}a)`} />
            <path d={path} fill="none" stroke={color} strokeWidth="2.5" vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" className="chart-draw" />
            {hover != null && <line x1={xy(hover, 0)[0]} x2={xy(hover, 0)[0]} y1="0" y2={H} stroke="var(--ink-3)" strokeDasharray="2 3" vectorEffect="non-scaling-stroke" />}
          </svg>
          <span className="absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-[var(--surface)]" style={{ left: `${(xy(last.i, last.v)[0] / W) * 100}%`, top: `${(xy(last.i, last.v)[1] / H) * 100}%`, background: color }} />
          {hv && hv.value != null && <span className="absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-[var(--surface)]" style={{ left: `${(xy(hover!, hv.value)[0] / W) * 100}%`, top: `${(xy(hover!, hv.value)[1] / H) * 100}%`, background: color }} />}
          {hv && <Tip x={(xy(hover!, 0)[0] / W) * 100} label={hv.label} value={fmtValue(hv.value, data.unit)} />}
        </div>
      </div>
      <div className="ml-14 mt-1.5 flex justify-between text-[0.625rem] text-ink-3"><span>{data.series[0]?.label}</span><span>{data.series[Math.floor(n / 2)]?.label}</span><span>{data.series.at(-1)?.label}</span></div>
      <p className="mt-2 text-xs text-ink-3">Latest <b className="dq-num text-ink">{fmtValue(last.v, data.unit)}</b> · overall {fmtValue(data.value, data.unit)}</p>
    </div>
  );
}

// ── pie / donut ──
function Pie({ data, donut }: { data: ChartData; donut: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const rows = data.series.filter((s) => (s.value ?? 0) > 0);
  const top = rows.slice(0, 7);
  const rest = rows.slice(7).reduce((a, s) => a + (s.value ?? 0), 0);
  const slices = rest > 0 ? [...top, { label: "Other", value: rest }] : top;
  const total = slices.reduce((a, s) => a + (s.value ?? 0), 0);
  if (!total) return <p className="text-sm text-ink-3">Nothing in this period.</p>;
  const R = 70, r = donut ? 44 : 0, C = 80;
  let angle = -Math.PI / 2;
  const arcs = slices.map((s, i) => {
    const a = ((s.value ?? 0) / total) * Math.PI * 2;
    const a0 = angle, a1 = angle + a;
    angle = a1;
    const big = a > Math.PI ? 1 : 0;
    const pop = hover === i ? 5 : 0, mid = (a0 + a1) / 2;
    const ox = Math.cos(mid) * pop, oy = Math.sin(mid) * pop;
    const p = (rad: number, ang: number) => `${C + ox + rad * Math.cos(ang)},${C + oy + rad * Math.sin(ang)}`;
    const d = slices.length === 1
      ? (donut ? `M${C - R},${C} a${R},${R} 0 1,0 ${2 * R},0 a${R},${R} 0 1,0 ${-2 * R},0 M${C - r},${C} a${r},${r} 0 1,1 ${2 * r},0 a${r},${r} 0 1,1 ${-2 * r},0` : `M${C - R},${C} a${R},${R} 0 1,0 ${2 * R},0 a${R},${R} 0 1,0 ${-2 * R},0`)
      : donut ? `M${p(R, a0)} A${R},${R} 0 ${big} 1 ${p(R, a1)} L${p(r, a1)} A${r},${r} 0 ${big} 0 ${p(r, a0)} Z`
        : `M${C + ox},${C + oy} L${p(R, a0)} A${R},${R} 0 ${big} 1 ${p(R, a1)} Z`;
    return { d, i, s };
  });
  const focus = hover != null ? slices[hover] : null;
  return (
    <div className="flex flex-wrap items-center gap-5">
      <svg viewBox="0 0 160 160" className="chart-spin size-40 shrink-0" role="img" aria-label={`${data.label}, ${slices.length} parts`} onMouseLeave={() => setHover(null)}>
        {arcs.map(({ d, i }) => <path key={i} d={d} fill={CAT(i)} fillRule="evenodd" stroke="var(--surface)" strokeWidth="2" onMouseEnter={() => setHover(i)}
          style={{ opacity: hover == null || hover === i ? 1 : 0.55, transition: "opacity 0.15s" }} />)}
        {donut && <>
          <text x={C} y={C - 2} textAnchor="middle" className="dq-num" style={{ fill: "var(--ink)", fontSize: 20, fontWeight: 700 }}>{fmtValue(focus ? focus.value : total, data.unit)}</text>
          <text x={C} y={C + 14} textAnchor="middle" style={{ fill: "var(--ink-3)", fontSize: 9 }}>{focus ? truncate(focus.label, 16) : "total"}</text>
        </>}
      </svg>
      <ul className="min-w-[11rem] flex-1 space-y-1.5 text-xs" aria-label="Legend">
        {slices.map((s, i) => (
          <li key={s.label} className={cn("flex items-center gap-2 rounded-md px-1.5 py-0.5 transition-colors", hover === i && "bg-surface-2")} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
            <i className="size-2.5 shrink-0 rounded-sm" style={{ background: CAT(i) }} />
            <span className="min-w-0 flex-1 truncate text-ink" title={s.label}>{s.label}</span>
            <span className="dq-num font-semibold text-ink">{fmtValue(s.value, data.unit)}</span>
            <span className="w-9 text-right text-ink-3">{Math.round(((s.value ?? 0) / total) * 100)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Table({ data }: { data: ChartData }) {
  const max = Math.max(1e-9, ...data.series.map((s) => s.value ?? 0));
  return (
    <div className="scrollbar-thin max-h-60 overflow-auto">
      <table className="w-full text-xs">
        <tbody>
          {data.series.map((s) => (
            <tr key={s.label} className="border-b border-line last:border-0">
              <td className="py-1.5 pr-3 text-ink-2">{s.label}</td>
              <td className="w-1/3 py-1.5 pr-3"><span className="block h-1.5 rounded-full bg-accent/70" style={{ width: `${((s.value ?? 0) / max) * 100}%` }} /></td>
              <td className="dq-num py-1.5 text-right font-semibold text-ink">{fmtValue(s.value, data.unit)}</td>
            </tr>
          ))}
          <tr><td className="pt-2 font-semibold text-ink">Total</td><td /><td className="dq-num pt-2 text-right font-bold text-ink">{fmtValue(data.value, data.unit)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}

const Tip = ({ x, label, value }: { x: number; label: string; value: string }) => (
  <span className="chart-tooltip pointer-events-none absolute -top-2 z-20 -translate-y-full whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs shadow-[var(--elev-2)]"
    style={{ left: `${Math.min(88, Math.max(12, x))}%`, transform: "translate(-50%, -100%)" }}>
    <span className="block text-[0.625rem] text-ink-3">{label}</span><b className="dq-num text-ink">{value}</b>
  </span>
);

const Foot = ({ data }: { data: ChartData }) => (
  <p className="mt-2 text-xs text-ink-3">{["%", "min", "h"].includes(data.unit) ? "Overall" : "Total"} <b className="dq-num text-ink">{fmtValue(data.value, data.unit)}</b>
    {data.previous != null && ` · previous ${data.period_days} days ${fmtValue(data.previous, data.unit)}`}</p>
);

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return ([1, 2, 2.5, 5, 10].find((m) => m * p >= v) ?? 10) * p;
}

const truncate = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
