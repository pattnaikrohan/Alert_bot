// The run map: every pipeline across the whole period, one cell per hour (or 2 h / 4 h for longer periods), so a
// pipeline's whole story reads left to right: when it ran, what failed and why, how long it took against its usual,
// how many rows it loaded, where incidents opened, and where "now" is. One measure colours the cells at a time;
// failures always show. Hover a cell for everything about that slot; click it to open the run.
import { AlertTriangle, Clock3, Database, Flame, HeartPulse, ListChecks, Snail, type LucideIcon } from "lucide-react";
import { memo, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Segmented } from "@/components/ui/primitives";
import { duration } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface MapCell {
  slot: number; ok: number; failed: number; other: number; avg_s: number | null; max_s: number | null; rows: number; error: string | null; run_id: string | null;
}
export interface MapPipe {
  key: string; project: string; project_name: string; name: string; state: string; ok: number; failed: number; success_rate: number | null;
  duration: { p50: number | null }; grid: MapCell[]; incidents: { slot: number; id: number; title: string; severity: string; status: string }[];
}
export interface MapMeta { slot_h: number; slots: number; now: number }

type Measure = "health" | "runs" | "runtime" | "rows";
type Show = (e: React.MouseEvent, body: ReactNode) => void;

const MEASURES: { value: Measure; label: string; icon: LucideIcon }[] = [
  { value: "health", label: "Health", icon: HeartPulse }, { value: "runs", label: "Runs", icon: ListChecks },
  { value: "runtime", label: "Runtime", icon: Clock3 }, { value: "rows", label: "Rows loaded", icon: Database },
];
const STORE = "alertbot-pr-map";
const readMeasure = (): Measure => { try { const v = localStorage.getItem(STORE); return MEASURES.some((m) => m.value === v) ? (v as Measure) : "health"; } catch { return "health"; } };

const nf = new Intl.NumberFormat("en-US");
const cf = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const n = (v: number) => (Math.abs(v) < 10000 ? nf.format(Math.round(v)) : cf.format(v));
const dayOf = (s: string) => { const [y, m, d] = s.split("-").map(Number); return new Date(y!, m! - 1, d!); };
const wd = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const dm = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const hh = (h: number) => `${String(h % 24).padStart(2, "0")}:00`;
const mix = (color: string, pct: number) => `color-mix(in srgb, ${color} ${Math.round(pct)}%, var(--surface-2))`;

/** A cell's colour for the measure; null leaves the empty-slot look. */
function paint(c: MapCell, m: Measure, row: { runsMax: number; rowsMax: number; p50: number | null }): string | null {
  const runs = c.ok + c.failed + c.other;
  if (m === "health") {
    if (c.failed && !c.ok) return mix("var(--critical)", 88);
    if (c.failed) return mix("var(--warning)", 85);
    if (c.ok) return mix("var(--good)", 38 + 50 * Math.sqrt(c.ok / row.runsMax));
    return runs ? mix("var(--ink-3)", 40) : null;
  }
  if (m === "runs") return runs ? mix("var(--accent)", 18 + 77 * Math.sqrt(runs / row.runsMax)) : null;
  if (m === "rows") {
    if (c.rows > 0) return mix("var(--cat-7)", 20 + 75 * Math.sqrt(c.rows / row.rowsMax));
    return runs ? "var(--surface)" : null;
  }
  // runtime: against the pipeline's own usual (median) — faster is blue (cat-1), usual is grey, slower is orange (cat-2)
  if (c.avg_s == null || !row.p50) return runs ? mix("var(--ink-3)", 22) : null;
  const k = Math.max(-1, Math.min(1, Math.log2(c.avg_s / row.p50)));
  if (Math.abs(k) < 0.15) return mix("var(--ink-3)", 42);
  return k > 0 ? mix("var(--cat-2)", 30 + 65 * k) : mix("var(--cat-1)", 30 + 65 * -k);
}

function RunMapInner({ pipes, meta, calendar, today, colorOf, show, hide, timezone }: {
  pipes: MapPipe[]; meta: MapMeta; calendar: string[]; today: string; colorOf: (k: string) => string; show: Show; hide: () => void; timezone: string;
}) {
  const [measure, setMeasureRaw] = useState<Measure>(readMeasure);
  const setMeasure = (m: Measure) => { setMeasureRaw(m); try { localStorage.setItem(STORE, m); } catch { /* storage blocked */ } };
  // the hovered column is outlined in every row by one style rule, written directly: hovering never re-renders the map
  const colStyle = useRef<HTMLStyleElement>(null);
  const col = useRef<number | null>(null);
  const setCol = (slot: number | null) => {
    if (col.current === slot) return;
    col.current = slot;
    if (colStyle.current) colStyle.current.textContent = slot === null ? "" : `.rm [data-slot="${slot}"] { box-shadow: inset 0 0 0 1.5px var(--ink); }`;
  };
  const navigate = useNavigate();
  const perDay = 24 / meta.slot_h;
  const nowSlot = Math.floor(meta.now);
  const manyProjects = new Set(pipes.map((p) => p.project)).size > 1;

  // every pipeline's cells by slot, its own scale, and one "All pipelines" row on top
  const rows = useMemo(() => pipes.map((p) => {
    const by = new Map(p.grid.map((c) => [c.slot, c]));
    return { p, by, inc: new Map(p.incidents.map((i) => [i.slot, i])),
      runsMax: Math.max(1, ...p.grid.map((c) => c.ok + c.failed + c.other)), rowsMax: Math.max(1, ...p.grid.map((c) => c.rows)),
      loads: p.grid.some((c) => c.rows > 0) };
  }), [pipes]);
  const all = useMemo(() => {
    const by = new Map<number, MapCell & { pipes: number; failing: string[] }>();
    for (const { p } of rows) for (const c of p.grid) {
      const a = by.get(c.slot) ?? { slot: c.slot, ok: 0, failed: 0, other: 0, avg_s: null, max_s: null, rows: 0, error: null, run_id: null, pipes: 0, failing: [] };
      a.ok += c.ok; a.failed += c.failed; a.other += c.other; a.rows += c.rows; a.pipes += 1;
      if (c.failed) a.failing.push(p.name);
      by.set(c.slot, a);
    }
    return { by, runsMax: Math.max(1, ...[...by.values()].map((c) => c.ok + c.failed + c.other)), rowsMax: Math.max(1, ...[...by.values()].map((c) => c.rows)) };
  }, [rows]);

  const when = (slot: number) => {
    const d = calendar[Math.floor(slot / perDay)];
    const h = (slot % perDay) * meta.slot_h;
    if (!d) return "";
    return `${d === today ? "Today" : `${wd.format(dayOf(d))} ${dm.format(dayOf(d))}`}, ${hh(h)}–${hh(h + meta.slot_h)}`;
  };

  // what stands out, worked out from the same cells
  const callouts = useMemo(() => {
    const out: { icon: LucideIcon; color: string; text: ReactNode }[] = [];
    const hot = [...all.by.values()].filter((c) => c.failed).sort((a, b) => b.failing.length - a.failing.length || b.failed - a.failed)[0];
    if (hot) out.push({ icon: Flame, color: "var(--critical)", text: <>Most failures: <b className="text-ink">{when(hot.slot)}</b> · {hot.failed} failed run{hot.failed === 1 ? "" : "s"}{hot.failing.length > 1 ? ` across ${hot.failing.length} pipelines` : ` in ${hot.failing[0]}`}</> });
    let slow: { name: string; slot: number; k: number } | null = null;
    for (const { p } of rows) for (const c of p.grid) {
      const k = c.avg_s != null && p.duration.p50 ? c.avg_s / p.duration.p50 : 0;
      if (k >= 1.5 && (!slow || k > slow.k)) slow = { name: p.name, slot: c.slot, k };
    }
    if (slow) out.push({ icon: Snail, color: "var(--cat-2)", text: <>Slowest: <b className="text-ink">{slow.name}</b> took {slow.k.toFixed(1)}× its usual time on {when(slow.slot)}</> });
    const empty = rows.flatMap(({ p, loads }) => (loads ? p.grid.filter((c) => c.ok && !c.rows).map(() => p.name) : []));
    if (empty.length) {
      const top = Object.entries(empty.reduce<Record<string, number>>((a, k) => ({ ...a, [k]: (a[k] ?? 0) + 1 }), {})).sort((a, b) => b[1] - a[1])[0]!;
      out.push({ icon: AlertTriangle, color: "var(--warning)", text: <>Ran but loaded nothing: <b className="text-ink">{plural(empty.length, "time")}</b>{top[1] > 1 ? `, mostly ${top[0]} (${top[1]})` : ""}</> });
    }
    return out;
  }, [all, rows]); // eslint-disable-line react-hooks/exhaustive-deps

  const tip = (e: React.MouseEvent, name: ReactNode, slot: number, c: MapCell | undefined, extra: { p50?: number | null; inc?: MapPipe["incidents"][number]; pipes?: number; failing?: string[] }) => {
    const runs = c ? c.ok + c.failed + c.other : 0;
    const vs = c?.avg_s != null && extra.p50 ? c.avg_s / extra.p50 - 1 : null;
    show(e, (
      <>
        <p className="font-semibold text-ink">{name}</p>
        <p className="mb-1 text-ink-3">{when(slot)}{slot > meta.now ? " · still to come" : slot === nowSlot ? " · now" : ""}</p>
        {!runs ? <p className="text-ink-2">{slot > meta.now ? "Not yet" : "Nothing ran"}</p> : (
          <>
            <Line label="Succeeded" v={c!.ok} color="var(--good)" />
            {c!.failed > 0 && <Line label="Failed" v={c!.failed} color="var(--critical)" />}
            {c!.other > 0 && <Line label="Running or cancelled" v={c!.other} color="var(--ink-3)" />}
            {extra.pipes != null && <Line label="Pipelines that ran" v={extra.pipes} color="transparent" />}
            {c!.avg_s != null && (
              <p className="flex justify-between gap-4 tabular-nums"><span className="text-ink-2">Runtime</span>
                <b className="text-ink">{duration(c!.avg_s)}{vs != null && Math.abs(vs) >= 0.1 && <span className={vs > 0 ? "text-[var(--cat-2)]" : "text-[var(--cat-1)]"}> {vs > 0 ? "+" : ""}{Math.round(vs * 100)}%</span>}</b></p>
            )}
            {c!.max_s != null && c!.max_s !== c!.avg_s && <p className="flex justify-between gap-4 tabular-nums"><span className="text-ink-2">Longest</span><b className="text-ink">{duration(c!.max_s)}</b></p>}
            {c!.rows > 0 && <Line label="Rows loaded" v={c!.rows} color="var(--cat-7)" />}
            {extra.p50 != null && c!.avg_s != null && <p className="text-[0.6875rem] text-ink-3">usually {duration(extra.p50)}</p>}
            {c!.error && <p className="mt-1 border-t border-line pt-1 text-[var(--critical)]">{c!.error}</p>}
            {extra.failing && extra.failing.length > 0 && <p className="mt-1 border-t border-line pt-1 text-[var(--critical)]">Failed: {extra.failing.slice(0, 4).join(", ")}{extra.failing.length > 4 ? ` +${extra.failing.length - 4}` : ""}</p>}
          </>
        )}
        {extra.inc && <p className="mt-1 border-t border-line pt-1 text-ink-2"><b className="text-[var(--critical)]">Incident opened:</b> {extra.inc.title}</p>}
        {c?.run_id && <p className="mt-1 text-[0.6875rem] text-accent">Click to open {c.failed ? "the failed run" : "the run"}</p>}
      </>
    ));
  };

  const strip = ({ by, inc, paintOf, onCell, onClick, failOn = true, hollow }: {
    by: Map<number, MapCell>; inc?: Map<number, unknown>; paintOf: (c: MapCell) => string | null; onCell: (e: React.MouseEvent, slot: number) => void;
    onClick?: (slot: number) => void; failOn?: boolean; hollow?: (c: MapCell) => boolean;
  }) => (
    <div className="rm-strip" onMouseLeave={() => { setCol(null); hide(); }}>
      {calendar.map((d, di) => (
        <div key={d} className={cn("rm-day", d === today && "is-today")} style={{ gridTemplateColumns: `repeat(${perDay}, minmax(0, 1fr))` }}>
          {Array.from({ length: perDay }, (_, k) => {
            const slot = di * perDay + k;
            const c = by.get(slot);
            const bg = c ? paintOf(c) : null;
            return (
              <span key={k} data-slot={slot}
                className={cn("rm-c", slot > meta.now && "is-future", slot === nowSlot && "is-now", c?.failed && failOn && measure !== "health" && "has-fail",
                  inc?.has(slot) && "has-inc", c && hollow?.(c) && "is-hollow", c?.run_id && onClick && "is-link")}
                style={bg ? { background: bg } : undefined}
                onMouseMove={(e) => { setCol(slot); onCell(e, slot); }}
                onClick={c?.run_id && onClick ? () => onClick(slot) : undefined} />
            );
          })}
        </div>
      ))}
    </div>
  );

  return (
    <div className="rm p-5 sm:p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Segmented<Measure> size="sm" label="Colour the cells by" value={measure} onChange={setMeasure} options={MEASURES.map(({ value, label }) => ({ value, label }))} />
        <Legend measure={measure} />
      </div>
      <style ref={colStyle} />
      <div className="scrollbar-thin overflow-x-auto pb-1">
        <div className="rm-grid" style={{ minWidth: `${24 + meta.slots * 0.42}rem` }}>
          {/* the time axis: days, and a few hours inside each */}
          <div className="rm-row rm-axis">
            <span className="rm-name text-[0.6875rem] font-semibold uppercase tracking-[0.06em] text-ink-3">Pipeline</span>
            <div className="rm-strip">
              {calendar.map((d) => (
                <div key={d} className={cn("rm-dayh", d === today && "is-today")}>
                  <b>{d === today ? "Today" : `${wd.format(dayOf(d))} ${dayOf(d).getDate()}`}</b>
                  <span className="rm-ticks" style={{ gridTemplateColumns: `repeat(${perDay}, minmax(0, 1fr))` }}>
                    {[6, 12, 18].filter((h) => h % meta.slot_h === 0 && (meta.slot_h < 4 || h === 12)).map((h) => <i key={h} style={{ gridColumn: h / meta.slot_h + 1 }}>{h}</i>)}
                  </span>
                </div>
              ))}
            </div>
            <span className="rm-stat text-[0.6875rem] font-semibold uppercase tracking-[0.06em] text-ink-3">Success</span>
          </div>

          {/* all pipelines together */}
          <div className="rm-row rm-all">
            <span className="rm-name"><b className="text-ink">All pipelines</b><small>{pipes.length} · each hour together</small></span>
            {strip({ by: all.by, paintOf: (c) => paint(c, measure === "runtime" ? "runs" : measure, { runsMax: all.runsMax, rowsMax: all.rowsMax, p50: null }),
              onCell: (e, slot) => { const c = all.by.get(slot); tip(e, "All pipelines", slot, c, { pipes: c?.pipes, failing: c?.failing }); } })}
            <span className="rm-stat" />
          </div>

          {rows.map(({ p, by, inc, runsMax, rowsMax, loads }) => {
            const rate = p.success_rate;
            return (
              <div key={p.key} className="rm-row">
                <span className="rm-name" title={`${p.name} · ${p.project_name}`}>
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: colorOf(p.key) }} aria-hidden />
                    <b className="truncate text-ink">{p.name}</b>
                  </span>
                  <small className="truncate">{[manyProjects && p.project_name, p.duration.p50 != null && `usually takes ${duration(p.duration.p50)}`].filter(Boolean).join(" · ") || " "}</small>
                </span>
                {strip({ by, inc, paintOf: (c) => paint(c, measure, { runsMax, rowsMax, p50: p.duration.p50 }),
                  hollow: measure === "rows" && loads ? (c) => c.ok > 0 && !c.rows : undefined,
                  onCell: (e, slot) => tip(e, p.name, slot, by.get(slot), { p50: p.duration.p50, inc: inc.get(slot) }),
                  onClick: (slot) => { const c = by.get(slot); navigate(`/p/${p.project}/runs?pipeline=${encodeURIComponent(p.name)}${c?.run_id ? `&run=${encodeURIComponent(c.run_id)}` : ""}`); } })}
                <span className="rm-stat">
                  <b className={cn("tabular-nums", rate == null ? "text-ink-3" : rate < 0.9 ? "text-[var(--critical)]" : "text-ink")}>{rate == null ? "–" : `${Math.round(rate * 100)}%`}</b>
                  <small>{p.ok}/{p.ok + p.failed}</small>
                </span>
              </div>
            );
          })}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-[0.8125rem] text-ink-2">
        {callouts.map(({ icon: I, color, text }, i) => <span key={i} className="inline-flex items-center gap-1.5"><I className="size-4 shrink-0" style={{ color }} />{text}</span>)}
        <span className="text-[0.75rem] text-ink-3">{meta.slot_h === 1 ? "One cell per hour" : `One cell per ${meta.slot_h} hours`} · {timezone} · click a cell to open its run</span>
      </div>
    </div>
  );
}

// show/hide only set the page's hover card, so a moving tooltip (which re-renders the page) never redraws the map
export const RunMap = memo(RunMapInner, (a, b) => a.pipes === b.pipes && a.meta === b.meta && a.calendar === b.calendar && a.today === b.today
  && a.colorOf === b.colorOf && a.timezone === b.timezone);

const plural = (k: number, w: string) => `${nf.format(k)} ${w}${k === 1 ? "" : "s"}`;

function Line({ label, v, color }: { label: string; v: number; color: string }) {
  return (
    <p className="flex items-center justify-between gap-4 tabular-nums">
      <span className="flex items-center gap-1.5 text-ink-2"><span className="size-2 rounded-[2px]" style={{ background: color }} />{label}</span><b className="text-ink">{n(v)}</b>
    </p>
  );
}

function Legend({ measure }: { measure: Measure }) {
  const sw = (bg: string, label: string, cls?: string) => (
    <span key={label} className="inline-flex items-center gap-1.5"><span className={cn("rm-sw", cls)} style={{ background: bg }} />{label}</span>
  );
  const ramp = (color: string, lo: string, hi: string) => (
    <span className="inline-flex items-center gap-1.5">{lo}<span className="flex gap-[2px]">{[20, 45, 70, 95].map((k) => <span key={k} className="rm-sw" style={{ background: mix(color, k) }} />)}</span>{hi}</span>
  );
  const common = [sw("", "nothing ran"), sw("", "still to come", "is-future"), sw("", "incident opened", "has-inc")];
  const fail = sw("", "a run failed", "has-fail");
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[0.75rem] text-ink-2">
      {measure === "health" && [sw(mix("var(--good)", 70), "all succeeded"), sw(mix("var(--warning)", 85), "some failed"), sw(mix("var(--critical)", 88), "all failed"), sw(mix("var(--ink-3)", 40), "running or cancelled")]}
      {measure === "runs" && <>{ramp("var(--accent)", "fewer", "more runs")}{fail}</>}
      {measure === "runtime" && <>
        <span className="inline-flex items-center gap-1.5">faster<span className="flex gap-[2px]">{[mix("var(--cat-1)", 95), mix("var(--cat-1)", 55), mix("var(--ink-3)", 42), mix("var(--cat-2)", 55), mix("var(--cat-2)", 95)].map((b, i) => <span key={i} className="rm-sw" style={{ background: b }} />)}</span>slower than its usual</span>
        {fail}
      </>}
      {measure === "rows" && <>{ramp("var(--cat-7)", "fewer", "more rows")}{sw("var(--surface)", "ran, loaded nothing", "is-hollow")}{fail}</>}
      {common}
    </div>
  );
}
