import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { SeriesPoint } from "@/lib/types";
import { dateTime, day, num } from "@/lib/format";

/* Charts follow the house rules: one axis, 2px lines, a 10% wash for the learned
 * baseline band, status red only for out-of-range points (with a 2px surface ring),
 * recessive hairline grid, and a hover layer on every chart. */

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(120, Math.round(e!.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (max <= min) max = min + 1;
  const span = max - min;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const norm = step0 / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(min / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

/** A tooltip that stays inside its chart: near the right edge it opens to the left, near the left to the right, near the
 * top below the point (so it's never clipped by, or hidden under, the next card). */
function Tooltip({ x, y, w, children }: { x: number; y: number; w?: number; children: ReactNode }) {
  const tx = w && x > w - 120 ? "calc(-100% + 10px)" : x < 120 ? "-10px" : "-50%";
  const ty = y < 96 ? "14px" : "calc(-100% - 12px)";
  return (
    <div className="chart-tip" style={{ left: x, top: y, transform: `translate(${tx}, ${ty})` }} role="status">
      {children}
    </div>
  );
}

// ── Metric history with learned band ─────────────────────────────────────────

export function MetricChart({
  points,
  format = num,
  height = 220,
  label,
}: {
  points: SeriesPoint[];
  format?: (v: number) => string;
  height?: number;
  label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const m = { top: 12, right: 16, bottom: 26, left: 56 };
  const w = width - m.left - m.right;
  const h = height - m.top - m.bottom;

  const geo = useMemo(() => {
    const vals = points.flatMap((p) => [p.value, p.low ?? p.value, p.high ?? p.value]);
    const lo = Math.min(0, ...vals);
    const hi = Math.max(...vals, lo + 1e-9);
    const ticks = niceTicks(lo, hi * 1.05);
    const yMax = ticks[ticks.length - 1]!;
    const yMin = ticks[0]!;
    const x = (i: number) => (points.length <= 1 ? w / 2 : (i / (points.length - 1)) * w);
    const y = (v: number) => h - ((v - yMin) / (yMax - yMin || 1)) * h;
    return { ticks, x, y };
  }, [points, w, h]);

  if (!points.length) return <div className="subtle small">No measurements yet.</div>;
  const { ticks, x, y } = geo;
  const out = (p: SeriesPoint) => p.high != null && p.low != null && (p.value > p.high || p.value < p.low);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join("");
  const banded = points.map((p, i) => ({ i, p })).filter(({ p }) => p.low != null && p.high != null);
  const band =
    banded.length > 1
      ? banded.map(({ i, p }, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(p.high!).toFixed(1)}`).join("") +
        [...banded].reverse().map(({ i, p }) => `L${x(i).toFixed(1)},${y(p.low!).toFixed(1)}`).join("") + "Z"
      : "";
  const xLabels = [0, Math.floor((points.length - 1) / 2), points.length - 1].filter((v, i, a) => a.indexOf(v) === i);

  const onMove = (e: React.MouseEvent<SVGRectElement>) => {
    const bx = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - bx.left) / bx.width) * w;
    const i = points.length <= 1 ? 0 : Math.round((px / w) * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, i)));
  };
  const hp = hover != null ? points[hover]! : null;

  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={height} role="img" aria-label={`${label}: ${points.length} runs with the learned normal range`}>
        <g transform={`translate(${m.left},${m.top})`}>
          {ticks.map((t) => (
            <g key={t}>
              <line className={t === ticks[0] ? "baseline" : "gridline"} x1={0} x2={w} y1={y(t)} y2={y(t)} />
              <text x={-10} y={y(t)} dy="0.32em" textAnchor="end">{format(t)}</text>
            </g>
          ))}
          {band && <path d={band} fill="color-mix(in srgb, var(--accent) 14%, transparent)" />}
          <path d={line} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          {points.map((p, i) =>
            out(p) ? (
              <circle key={p.run + i} cx={x(i)} cy={y(p.value)} r={5} fill="var(--critical)" stroke="var(--surface)" strokeWidth={2} />
            ) : null,
          )}
          {!out(points[points.length - 1]!) && (
            <circle cx={x(points.length - 1)} cy={y(points[points.length - 1]!.value)} r={4.5} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} />
          )}
          {xLabels.map((i) => (
            <text key={i} x={x(i)} y={h + 18} textAnchor={i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}>
              {day(points[i]!.t)}
            </text>
          ))}
          {hp && (
            <>
              <line x1={x(hover!)} x2={x(hover!)} y1={0} y2={h} stroke="var(--line-strong)" strokeWidth={1} />
              <circle cx={x(hover!)} cy={y(hp.value)} r={5} fill={out(hp) ? "var(--critical)" : "var(--accent)"} stroke="var(--surface)" strokeWidth={2} />
            </>
          )}
          <rect x={0} y={0} width={w} height={h} fill="transparent" onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
        </g>
      </svg>
      {hp && (
        <Tooltip x={m.left + x(hover!)} y={m.top + y(hp.value)} w={width}>
          <div className="tip-title">{dateTime(hp.t)}</div>
          <div className="tip-row"><span>Value</span><b>{format(hp.value)}</b></div>
          <div className="tip-row">
            <span>Normal for {hp.weekday}</span>
            <b>{hp.low != null && hp.high != null ? `${format(hp.low)} – ${format(hp.high)}` : "learning"}</b>
          </div>
          {out(hp) && <div className="tip-row" style={{ color: "var(--critical-ink)" }}><span>Outside the normal range</span></div>}
        </Tooltip>
      )}
      <div className="legend" style={{ marginTop: 6 }}>
        <span><i className="line" style={{ background: "var(--accent)" }} />{label}</span>
        <span><i style={{ background: "color-mix(in srgb, var(--accent) 14%, transparent)", outline: "1px solid var(--accent)", outlineOffset: -1 }} />Learned normal range</span>
        <span><i style={{ background: "var(--critical)", borderRadius: "50%" }} />Outside range</span>
      </div>
    </div>
  );
}

// ── Sparkline for site cards ─────────────────────────────────────────────────

export function Sparkline({
  values,
  band,
  status,
  height = 40,
}: {
  values: number[];
  band?: { low: number; high: number } | null;
  status: string;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  if (values.length < 2) return <div style={{ height }} className="subtle small">Learning…</div>;
  const lo = Math.min(...values, band?.low ?? Infinity, 0);
  const hi = Math.max(...values, band?.high ?? -Infinity) * 1.08 || 1;
  const pad = 4;
  const x = (i: number) => pad + (i / (values.length - 1)) * (width - pad * 2);
  const y = (v: number) => height - pad - ((v - lo) / (hi - lo || 1)) * (height - pad * 2);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = values[values.length - 1]!;
  const lastOut = band ? last < band.low || last > band.high : false;
  const endColor = lastOut ? (status === "warning" ? "var(--warning)" : "var(--critical)") : "var(--accent)";
  return (
    <div className="chart" ref={ref} style={{ height }}>
      <svg width={width} height={height} aria-hidden="true">
        {band && (
          <rect x={pad} width={width - pad * 2} y={y(band.high)} height={Math.max(1, y(band.low) - y(band.high))} fill="color-mix(in srgb, var(--accent) 14%, transparent)" rx={2} />
        )}
        <path d={d} fill="none" stroke="var(--ink-3)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={x(values.length - 1)} cy={y(last)} r={4} fill={endColor} stroke="var(--surface)" strokeWidth={2} />
        {hover != null && <circle cx={x(hover)} cy={y(values[hover]!)} r={3.5} fill="var(--ink-2)" stroke="var(--surface)" strokeWidth={2} />}
        <rect width={width} height={height} fill="transparent"
          onMouseMove={(e) => {
            const bx = e.currentTarget.getBoundingClientRect();
            setHover(Math.max(0, Math.min(values.length - 1, Math.round(((e.clientX - bx.left) / bx.width) * (values.length - 1)))));
          }}
          onMouseLeave={() => setHover(null)} />
      </svg>
      {hover != null && (
        <Tooltip x={x(hover)} y={y(values[hover]!)} w={width}>
          <div className="tip-row"><span>{hover === values.length - 1 ? "Latest run" : `${values.length - 1 - hover} runs ago`}</span><b>{num(values[hover]!)} rows</b></div>
        </Tooltip>
      )}
    </div>
  );
}

// ── 14-day activity (stacked columns) ────────────────────────────────────────

export function ActivityChart({ days, height = 170 }: { days: { date: string; runs: number; failed: number; rows: number }[]; height?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const m = { top: 10, right: 8, bottom: 24, left: 36 };
  const w = width - m.left - m.right;
  const h = height - m.top - m.bottom;
  const max = Math.max(1, ...days.map((d) => d.runs));
  const ticks = niceTicks(0, max, 3);
  const yMax = ticks[ticks.length - 1]!;
  const y = (v: number) => h - (v / yMax) * h;
  const slot = w / Math.max(1, days.length);
  const bw = Math.min(24, slot * 0.62);
  const r = 4;

  // Column with a rounded data-end only; square at the baseline.
  const col = (x0: number, top: number, bottom: number, round: boolean) => {
    const hh = bottom - top;
    if (hh <= 0) return "";
    const rr = round ? Math.min(r, hh, bw / 2) : 0;
    return `M${x0},${bottom}V${top + rr}${rr ? `Q${x0},${top} ${x0 + rr},${top}` : ""}H${x0 + bw - rr}${rr ? `Q${x0 + bw},${top} ${x0 + bw},${top + rr}` : ""}V${bottom}Z`;
  };

  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={height} role="img" aria-label="Runs per day over the last 14 days, split into succeeded and failed">
        <g transform={`translate(${m.left},${m.top})`}>
          {ticks.map((t) => (
            <g key={t}>
              <line className={t === 0 ? "baseline" : "gridline"} x1={0} x2={w} y1={y(t)} y2={y(t)} />
              <text x={-8} y={y(t)} dy="0.32em" textAnchor="end">{num(t)}</text>
            </g>
          ))}
          {days.map((d, i) => {
            const x0 = i * slot + (slot - bw) / 2;
            const ok = d.runs - d.failed;
            const okTop = y(ok);
            const failTop = y(d.runs);
            const gap = d.failed && ok ? 2 : 0;
            return (
              <g key={d.date} opacity={hover == null || hover === i ? 1 : 0.55}>
                <path d={col(x0, okTop, h, !d.failed)} fill="var(--accent)" />
                {d.failed > 0 && <path d={col(x0, failTop, okTop - gap, true)} fill="var(--critical)" />}
                {(i % 2 === 1 || days.length <= 8) && (
                  <text x={x0 + bw / 2} y={h + 17} textAnchor="middle">{new Date(d.date + "T12:00:00").getDate()}</text>
                )}
                <rect x={i * slot} y={0} width={slot} height={h} fill="transparent"
                  onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} />
              </g>
            );
          })}
        </g>
      </svg>
      {hover != null && (
        <Tooltip x={m.left + hover * slot + slot / 2} y={m.top + y(days[hover]!.runs)} w={width}>
          <div className="tip-title">{day(days[hover]!.date + "T12:00:00")}</div>
          <div className="tip-row"><span>Succeeded</span><b>{days[hover]!.runs - days[hover]!.failed}</b></div>
          <div className="tip-row"><span>Failed</span><b>{days[hover]!.failed}</b></div>
          <div className="tip-row"><span>Rows loaded</span><b>{num(days[hover]!.rows)}</b></div>
        </Tooltip>
      )}
      <div className="legend" style={{ marginTop: 4 }}>
        <span><i style={{ background: "var(--accent)" }} />Succeeded</span>
        <span><i style={{ background: "var(--critical)" }} />Failed</span>
      </div>
    </div>
  );
}
