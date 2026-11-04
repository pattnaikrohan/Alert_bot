// How long a run took against how long its pipeline normally takes (learned from its own runs).
// Finished: a track with the normal range shaded, the median ticked, and this run's marker. Running: progress towards
// the normal finish, and when it should be done. Colour carries the verdict, and the words always say it too.
import { Timer } from "lucide-react";
import { duration, time } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface VsNormal {
  median_s: number; low_s: number; high_s: number; samples: number; basis: string;
  running?: boolean; actual_s?: number; pct?: number; delta_s?: number; verdict?: "faster" | "normal" | "slower" | "much_slower" | "on_track" | "overdue";
  elapsed_s?: number; expected_end?: string; remaining_s?: number; progress?: number | null;
}

const TONE: Record<string, { color: string; label: (r: VsNormal) => string }> = {
  faster: { color: "var(--good)", label: (r) => `${Math.abs(r.pct ?? 0)}% faster than normal` },
  normal: { color: "var(--good)", label: (r) => ((r.pct ?? 0) === 0 ? "right on normal" : `normal (${(r.pct ?? 0) > 0 ? "+" : ""}${r.pct}%)`) },
  slower: { color: "var(--warning)", label: (r) => `${r.pct}% slower than normal` },
  much_slower: { color: "var(--critical)", label: (r) => `${r.pct}% slower than normal` },
  on_track: { color: "var(--accent)", label: (r) => `on track · done ~${time(r.expected_end)}` },
  overdue: { color: "var(--critical)", label: (r) => `running past its normal (~${duration(r.median_s)})` },
};

const range = (r: VsNormal) => `${duration(r.low_s)}–${duration(r.high_s)}`;

/** A one-line verdict for tables: "+190% vs normal" (with the normal range in its tooltip). */
export function RuntimeChip({ r, className }: { r?: VsNormal | null; className?: string }) {
  if (!r?.verdict) return r ? <span className={cn("text-[0.6875rem] text-ink-3", className)} title={`Learned from ${r.samples} runs`}>normal {range(r)}</span> : null;
  const t = TONE[r.verdict]!;
  const text = r.running ? (r.verdict === "overdue" ? "overdue" : `~${duration(r.remaining_s ?? 0)} left`)
    : r.verdict === "normal" ? "normal" : `${(r.pct ?? 0) > 0 ? "+" : ""}${r.pct}%`;
  return (
    <span className={cn("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-1.5 py-px text-[0.65625rem] font-semibold", className)}
      style={{ color: `color-mix(in srgb, ${t.color} 75%, var(--ink))`, background: `color-mix(in srgb, ${t.color} 13%, transparent)` }}
      title={`${t.label(r)} · normal ${range(r)} (median ${duration(r.median_s)}, from ${r.samples} runs)`}>
      <span className="size-1.5 rounded-full" style={{ background: t.color }} aria-hidden />{text}
    </span>
  );
}

/** The full picture: the track, the marker, and the words. */
export function RuntimeBar({ r, className }: { r?: VsNormal | null; className?: string }) {
  if (!r) return <p className={cn("text-xs text-ink-3", className)}>Still learning this pipeline's normal run time.</p>;
  const t = TONE[r.verdict ?? "normal"]!;
  if (r.running) {
    const p = Math.min(1, r.progress ?? 0);
    return (
      <div className={cn("min-w-0", className)}>
        <div className="flex items-baseline justify-between gap-2 text-[0.75rem]">
          <span className="flex items-center gap-1.5 font-semibold text-ink"><Timer className="size-3.5 text-ink-3" />{duration(r.elapsed_s)} so far</span>
          <span className="text-ink-2">{t.label(r)}</span>
        </div>
        <div className="rt-track mt-1.5" aria-hidden>
          <span className="rt-fill" style={{ width: `${p * 100}%`, background: t.color }} />
          {(r.progress ?? 0) > 1 && <span className="rt-over" />}
        </div>
        <p className="mt-1 text-[0.6875rem] text-ink-3">Normally takes {range(r)} (median {duration(r.median_s)}, from {r.samples} runs)</p>
      </div>
    );
  }
  const max = Math.max(r.high_s * 1.6, (r.actual_s ?? 0) * 1.08, 1);
  const at = (v: number) => `${Math.min(100, (v / max) * 100)}%`;
  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex items-baseline justify-between gap-2 text-[0.75rem]">
        <span className="flex items-center gap-1.5 font-semibold text-ink"><Timer className="size-3.5 text-ink-3" />{duration(r.actual_s)}</span>
        <span style={{ color: `color-mix(in srgb, ${t.color} 70%, var(--ink))` }} className="font-medium">{t.label(r)}</span>
      </div>
      <div className="rt-track mt-2" aria-label={`Took ${duration(r.actual_s)}; normal is ${range(r)}`} role="img">
        <span className="rt-band" style={{ left: at(r.low_s), width: `calc(${at(r.high_s)} - ${at(r.low_s)})` }} />
        <span className="rt-median" style={{ left: at(r.median_s) }} />
        <span className="rt-dot" style={{ left: at(r.actual_s ?? 0), background: t.color, boxShadow: `0 0 0 3px color-mix(in srgb, ${t.color} 25%, transparent)` }} />
      </div>
      <p className="mt-1.5 text-[0.6875rem] text-ink-3">Normal {range(r)} · median {duration(r.median_s)} · learned from {r.samples} runs</p>
    </div>
  );
}
