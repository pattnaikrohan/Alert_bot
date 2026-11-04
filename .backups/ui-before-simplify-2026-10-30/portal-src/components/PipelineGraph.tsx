// An ADF run drawn the way ADF Studio draws the pipeline: steps as boxes in branches, arrows for "runs after",
// coloured by outcome. Click a step for what it did (folder contents, files kept, rows copied, the error).
import {
  Ban, Box, Braces, CheckCircle2, CircleDashed, Code2, Copy, Filter, GitBranch, Globe, Info, Loader2, Maximize2, Minus,
  Plus, Repeat, Search, Trash2, Workflow, XCircle, type LucideIcon,
} from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { duration } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface StepRun {
  status: string; start: string | null; end: string | null; duration_s: number | null; error: string | null; error_code: string | null;
  copy: { rows_read: number | null; rows_copied: number | null; rows_skipped: number | null } | null; facts?: string[];
}
export interface Step {
  name: string; type: string; branch: string | null; status: string; iterations: number; runs: StepRun[];
  depends_on?: { activity: string; conditions: string[] }[]; writes?: string[];
  children: Step[]; called?: { pipeline: string; run_id: string; steps?: Step[]; error?: string }[];
}

const TYPE: Record<string, { label: string; icon: LucideIcon }> = {
  Copy: { label: "Copy data", icon: Copy }, GetMetadata: { label: "Get Metadata", icon: Info }, Filter: { label: "Filter", icon: Filter },
  Delete: { label: "Delete", icon: Trash2 }, ExecutePipeline: { label: "Execute Pipeline", icon: Workflow },
  ForEach: { label: "ForEach", icon: Repeat }, IfCondition: { label: "If Condition", icon: GitBranch }, Switch: { label: "Switch", icon: GitBranch },
  Until: { label: "Until", icon: Repeat }, Lookup: { label: "Lookup", icon: Search }, SetVariable: { label: "Set variable", icon: Braces },
  AppendVariable: { label: "Append variable", icon: Braces }, WebActivity: { label: "Web", icon: Globe },
  SqlServerStoredProcedure: { label: "Stored procedure", icon: Code2 }, Script: { label: "Script", icon: Code2 }, Wait: { label: "Wait", icon: CircleDashed },
};
const typeOf = (t: string) => TYPE[t] ?? { label: t.replace(/([a-z])([A-Z])/g, "$1 $2"), icon: Box };

const COND_COLOR: Record<string, string> = { Succeeded: "var(--good)", Failed: "var(--critical)", Completed: "var(--accent)", Skipped: "var(--ink-3)" };
const NODE_W = 208, NODE_H = 80, GAP_X = 48, GAP_Y = 20;

function statusIcon(status: string) {
  if (status === "Failed") return <XCircle className="size-4 text-[var(--critical)]" aria-label="failed" />;
  if (status === "Succeeded") return <CheckCircle2 className="size-4 text-[var(--good)]" aria-label="succeeded" />;
  if (status === "InProgress" || status === "Queued") return <Loader2 className="size-4 animate-spin text-accent" aria-label="running" />;
  if (status === "Cancelled") return <Ban className="size-4 text-ink-3" aria-label="cancelled" />;
  return <CircleDashed className="size-4 text-ink-3" aria-label="did not run" />;
}

function headline(s: Step): string {
  const r = s.runs[s.runs.length - 1];
  if (!r) return "Didn't run";
  if (s.status === "Failed") return r.error_code === "InvalidTemplate" ? "Expression failed" : r.error_code || "Failed";
  const kept = r.facts?.find((f) => f.startsWith("Kept "));
  if (kept) return kept.replace(" item(s)", "");
  if (r.copy?.rows_copied != null) return `${r.copy.rows_copied.toLocaleString()} rows`;
  const items = r.facts?.find((f) => /item\(s\)|empty/.test(f));
  if (items) return items.split(":")[0]!;
  return duration(s.runs.reduce((a, x) => a + (x.duration_s ?? 0), 0));
}

/** Layered layout: a step sits one column right of the latest step it waits for; branches keep their own row. */
function layout(steps: Step[]) {
  const byName = new Map(steps.map((s) => [s.name, s]));
  const col = new Map<string, number>();
  const colOf = (s: Step, seen = new Set<string>()): number => {
    if (col.has(s.name)) return col.get(s.name)!;
    if (seen.has(s.name)) return 0;
    seen.add(s.name);
    const deps = (s.depends_on ?? []).map((d) => byName.get(d.activity)).filter(Boolean) as Step[];
    const c = deps.length ? 1 + Math.max(...deps.map((d) => colOf(d, seen))) : 0;
    col.set(s.name, c);
    return c;
  };
  steps.forEach((s) => colOf(s));
  const row = new Map<string, number>();
  const used = new Map<number, Set<number>>();
  let nextRow = 0;
  const order = [...steps].sort((a, b) => col.get(a.name)! - col.get(b.name)!);
  for (const s of order) {
    const c = col.get(s.name)!;
    const taken = used.get(c) ?? new Set<number>();
    const preds = (s.depends_on ?? []).map((d) => row.get(d.activity)).filter((r): r is number => r != null);
    let r = preds.length ? Math.min(...preds) : nextRow;
    while (taken.has(r)) r++;
    taken.add(r);
    used.set(c, taken);
    row.set(s.name, r);
    nextRow = Math.max(nextRow, r + 1);
  }
  const cols = Math.max(0, ...col.values()) + 1;
  return { col, row, width: cols * NODE_W + (cols - 1) * GAP_X, height: nextRow * NODE_H + Math.max(0, nextRow - 1) * GAP_Y };
}

/** The steps a step waits for, nearest first, up to three levels back. */
function feeders(step: Step, byName: Map<string, Step>): { step: Step; depth: number }[] {
  const out: { step: Step; depth: number }[] = [];
  const seen = new Set<string>();
  let frontier: Step[] = [step];
  for (let depth = 1; depth <= 3 && frontier.length; depth++) {
    const next: Step[] = [];
    for (const s of frontier) {
      for (const d of s.depends_on ?? []) {
        const f = byName.get(d.activity);
        if (f && !seen.has(f.name)) { seen.add(f.name); out.push({ step: f, depth }); next.push(f); }
      }
    }
    frontier = next;
  }
  return out;
}

function StepDetails({ step, byName }: { step: Step; byName: Map<string, Step> }) {
  const t = typeOf(step.type);
  const failed = step.runs.filter((r) => r.status === "Failed");
  const last = step.runs[step.runs.length - 1];
  const facts = [...new Set(step.runs.flatMap((r) => r.facts ?? []))];
  return (
    <div className="rounded-xl bg-surface-2 p-4 shadow-[inset_0_0_0_1px_var(--line)]">
      <div className="flex flex-wrap items-center gap-2">
        {statusIcon(step.status)}
        <span className="text-[0.9375rem] font-semibold text-ink">{step.name}</span>
        <span className="rounded bg-surface-3 px-1.5 py-px text-[0.6875rem] text-ink-3">{t.label}</span>
        <span className="text-xs text-ink-3">
          {step.status === "NotRun" ? "Didn't run: a step it waits for didn't end the way it needed" : step.status}
          {last?.start ? ` · ${new Date(last.start).toLocaleTimeString()}` : ""}
          {step.runs.length ? ` · ${duration(step.runs.reduce((a, r) => a + (r.duration_s ?? 0), 0))}` : ""}
          {step.iterations > 1 ? ` · ran ${step.iterations}×` : ""}
        </span>
      </div>
      {step.depends_on && step.depends_on.length > 0 && (
        <p className="mt-2 text-xs text-ink-3">Runs after {step.depends_on.map((d) => `'${d.activity}' (${d.conditions.join(" or ").toLowerCase()})`).join(", ")}</p>
      )}
      {(step.status === "Failed" || step.status === "NotRun") && feeders(step, byName).some((f) => f.step.runs.some((r) => r.facts?.length)) && (
        <div className="mt-3 rounded-lg bg-surface px-3 py-2 shadow-[inset_0_0_0_1px_var(--line)] light:bg-white">
          <p className="text-xs font-semibold text-ink-2">What led up to it</p>
          <ul className="mt-1 space-y-1 text-[0.8125rem] text-ink-2">
            {feeders(step, byName).map(({ step: f, depth }) => (
              <li key={f.name} style={{ paddingLeft: (depth - 1) * 14 }}>
                <span className="text-ink-3">{depth === 1 ? "← " : "↖ "}</span>
                <b className="text-ink">{f.name}</b> <span className="text-ink-3">({typeOf(f.type).label}, {f.status === "NotRun" ? "didn't run" : f.status.toLowerCase()})</span>
                {[...new Set(f.runs.flatMap((r) => r.facts ?? []))].length > 0 && <>: {[...new Set(f.runs.flatMap((r) => r.facts ?? []))].join(" · ")}</>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {facts.length > 0 && <ul className="mt-2 space-y-0.5 text-[0.8125rem] text-ink-2">{facts.map((f) => <li key={f}>• {f}</li>)}</ul>}
      {step.writes && step.writes.length > 0 && <p className="mt-1 text-[0.8125rem] text-ink-2">• Target dataset: {step.writes.join(", ")}</p>}
      {failed.length > 0 && failed[failed.length - 1]!.error && (
        <p className="mt-3 whitespace-pre-wrap break-words rounded-lg bg-[var(--critical-soft)] px-3 py-2 font-mono text-xs leading-5 text-[var(--critical-ink)]">
          {failed[failed.length - 1]!.error_code ? `${failed[failed.length - 1]!.error_code}: ` : ""}{failed[failed.length - 1]!.error}
        </p>
      )}
      {step.children.length > 0 && (
        <div className="mt-3">
          <p className="mb-2 text-xs font-semibold text-ink-2">Inside {step.name} ({step.children.length} steps)</p>
          <PipelineGraph steps={step.children} compact />
        </div>
      )}
      {step.called?.map((c) => (
        <div key={c.run_id} className="mt-3">
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-ink-2"><Workflow className="size-3.5 text-accent" /> Child pipeline {c.pipeline}</p>
          {c.error ? <p className="text-xs text-[var(--warning-ink)]">{c.error}</p> : <PipelineGraph steps={c.steps ?? []} compact />}
        </div>
      ))}
    </div>
  );
}

export function PipelineGraph({ steps, compact = false }: { steps: Step[]; compact?: boolean }) {
  const { col, row, width, height } = useMemo(() => layout(steps), [steps]);
  const byName = useMemo(() => new Map(steps.map((s) => [s.name, s])), [steps]);
  const firstFailed = useMemo(() => [...steps].sort((a, b) => (row.get(a.name)! - row.get(b.name)!) || (col.get(a.name)! - col.get(b.name)!))
    .find((s) => s.status === "Failed")?.name ?? null, [steps, row, col]);
  const [selected, setSelected] = useState<string | null>(firstFailed);
  const [scale, setScale] = useState(1);
  const [fit, setFit] = useState(true);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => setSelected(firstFailed), [firstFailed]);
  useLayoutEffect(() => {
    if (!fit || !box.current) return;
    const measure = () => {
      const w = box.current!.clientWidth - 24;
      setScale(Math.max(0.45, Math.min(1, w / width)));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(box.current);
    return () => ro.disconnect();
  }, [fit, width]);

  const counts = useMemo(() => {
    const c = { Failed: 0, Succeeded: 0, NotRun: 0, other: 0 };
    steps.forEach((s) => { if (s.status in c) c[s.status as "Failed"]++; else c.other++; });
    return c;
  }, [steps]);
  const pos = (n: string) => ({ x: col.get(n)! * (NODE_W + GAP_X), y: row.get(n)! * (NODE_H + GAP_Y) });
  const sel = selected ? byName.get(selected) : null;

  return (
    <div>
      {!compact && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3 text-xs text-ink-2">
            <span className="inline-flex items-center gap-1.5"><XCircle className="size-3.5 text-[var(--critical)]" /><b className="text-ink">{counts.Failed}</b> failed</span>
            <span className="inline-flex items-center gap-1.5"><CheckCircle2 className="size-3.5 text-[var(--good)]" /><b className="text-ink">{counts.Succeeded}</b> succeeded</span>
            <span className="inline-flex items-center gap-1.5"><CircleDashed className="size-3.5 text-ink-3" /><b className="text-ink">{counts.NotRun}</b> didn't run</span>
            <span className="hidden items-center gap-3 border-l border-line pl-3 sm:inline-flex">
              {(["Succeeded", "Failed", "Completed"] as const).map((k) => (
                <span key={k} className="inline-flex items-center gap-1"><span className="h-0.5 w-4 rounded" style={{ background: COND_COLOR[k] }} />on {k === "Completed" ? "completion" : k.toLowerCase()}</span>
              ))}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button type="button" className="grid size-7 place-items-center rounded-lg text-ink-2 hover:bg-surface-3" aria-label="Zoom out" onClick={() => { setFit(false); setScale((s) => Math.max(0.4, s - 0.1)); }}><Minus className="size-3.5" /></button>
            <span className="w-10 text-center text-xs tabular-nums text-ink-3">{Math.round(scale * 100)}%</span>
            <button type="button" className="grid size-7 place-items-center rounded-lg text-ink-2 hover:bg-surface-3" aria-label="Zoom in" onClick={() => { setFit(false); setScale((s) => Math.min(1.4, s + 0.1)); }}><Plus className="size-3.5" /></button>
            <button type="button" className={cn("grid size-7 place-items-center rounded-lg hover:bg-surface-3", fit ? "text-accent" : "text-ink-2")} aria-label="Fit to width" onClick={() => setFit(true)}><Maximize2 className="size-3.5" /></button>
          </div>
        </div>
      )}
      <div ref={box} className="overflow-auto rounded-xl bg-[color-mix(in_srgb,var(--surface-2)_55%,transparent)] p-3 shadow-[inset_0_0_0_1px_var(--line)]"
        style={{ backgroundImage: "radial-gradient(var(--line) 1px, transparent 1px)", backgroundSize: "16px 16px" }}>
        <div style={{ width: width * scale, height: height * scale }}>
          <div className="relative origin-top-left" style={{ width, height, transform: `scale(${scale})` }}>
            <svg className="pointer-events-none absolute inset-0 overflow-visible" width={width} height={height} aria-hidden>
              <defs>
                {Object.entries(COND_COLOR).map(([k, c]) => (
                  <marker key={k} id={`arrow-${k}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                    <path d="M0 0 L8 4 L0 8 z" style={{ fill: c }} />
                  </marker>
                ))}
              </defs>
              {steps.flatMap((s) => (s.depends_on ?? []).filter((d) => byName.has(d.activity)).map((d) => {
                const a = pos(d.activity), b = pos(s.name);
                const cond = d.conditions[0] ?? "Succeeded";
                const src = byName.get(d.activity)!;
                const taken = cond === "Completed" ? src.status !== "NotRun" : src.status === cond;
                const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x - 2, y2 = b.y + NODE_H / 2;
                const mid = (x1 + x2) / 2;
                return (
                  <path key={`${d.activity}->${s.name}`} d={`M${x1} ${y1} C${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`} fill="none"
                    style={{ stroke: COND_COLOR[cond] ?? COND_COLOR.Succeeded }} strokeWidth={taken ? 2 : 1.5}
                    strokeDasharray={taken ? undefined : "4 4"} opacity={taken ? 1 : 0.4} markerEnd={`url(#arrow-${cond in COND_COLOR ? cond : "Succeeded"})`} />
                );
              }))}
            </svg>
            {steps.map((s) => {
              const { x, y } = pos(s.name);
              const t = typeOf(s.type);
              const Icon = t.icon;
              const nested = s.children.length > 0 || (s.called?.length ?? 0) > 0;
              return (
                <button key={s.name} type="button" onClick={() => setSelected(s.name === selected ? null : s.name)}
                  className={cn("absolute flex flex-col overflow-hidden rounded-lg bg-surface text-left shadow-[0_1px_2px_rgb(0_0_0/0.18)] transition-shadow light:bg-white",
                    s.status === "Failed" ? "ring-2 ring-[var(--critical)]" : s.status === "Succeeded" ? "ring-1 ring-[color-mix(in_srgb,var(--good)_55%,var(--line))]" : "ring-1 ring-[var(--line)] opacity-60",
                    selected === s.name && "outline outline-2 outline-offset-2 outline-accent")}
                  style={{ left: x, top: y, width: NODE_W, height: NODE_H }} title={`${t.label}: ${s.name}`}>
                  <span className={cn("flex h-[1.375rem] items-center justify-between px-2 text-[0.75rem] font-medium",
                    s.status === "Failed" ? "bg-[var(--critical)] text-white" : "bg-surface-3 text-ink-3")}>
                    {t.label}{s.iterations > 1 ? ` ×${s.iterations}` : ""}{nested ? " · has steps inside" : ""}
                  </span>
                  <span className="flex flex-1 items-center gap-2 px-2">
                    <Icon className={cn("size-5 shrink-0", s.status === "Failed" ? "text-[var(--critical)]" : "text-accent")} aria-hidden />
                    <span className="min-w-0 flex-1">
                      <span className="line-clamp-2 text-[0.875rem] font-semibold leading-tight text-ink">{s.name}</span>
                      <span className={cn("block truncate text-[0.78125rem]", s.status === "Failed" ? "text-[var(--critical-ink)]" : "text-ink-3")}>{headline(s)}</span>
                    </span>
                    {statusIcon(s.status)}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
      {sel ? <div className="mt-3"><StepDetails step={sel} byName={byName} /></div>
        : !compact && <p className="mt-3 text-xs text-ink-3">Click a step to see what it did.</p>}
    </div>
  );
}
