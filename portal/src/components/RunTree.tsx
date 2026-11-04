// The failure tree of an ADF run: every authored step (ForEach / If / Switch / Until, and child pipelines run by
// ExecutePipeline) with what actually ran. The path down to the failure opens by itself.
import { ChevronRight, ExternalLink, GitBranch, Workflow } from "lucide-react";
import { useState } from "react";
import { ChartCard, Notice } from "@/components/Cards";
import { PipelineGraph } from "@/components/PipelineGraph";
import { RunTables } from "@/components/TableLoads";
import { Segmented } from "@/components/ui/primitives";
import { StatusChip } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { duration, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface StepRun {
  status: string; start: string | null; end: string | null; duration_s: number | null; error: string | null; error_code: string | null;
  copy: { rows_read: number | null; rows_copied: number | null; rows_skipped: number | null } | null; facts?: string[];
}
interface Step {
  name: string; type: string; branch: string | null; status: string; iterations: number; runs: StepRun[];
  depends_on?: { activity: string; conditions: string[] }[]; writes?: string[];
  children: Step[]; called?: { pipeline: string; run_id: string; steps?: Step[]; error?: string }[];
}
interface Tree {
  pipeline: string; run_id: string; status: string; monitor_url: string; steps: Step[];
  failure_path: { name: string; type: string; iteration: number | null; of: number; error: string | null }[];
  tables?: { step: string; table: string; env: string | null; kind: string | null; status: string; rows_read: number | null; rows_copied: number | null; rows_skipped: number | null; error: string | null }[];
}

const STATUS_COLOR: Record<string, string> = { Failed: "var(--critical)", Succeeded: "var(--good)", InProgress: "var(--accent)", NotRun: "var(--line-strong)" };

function copyLine(runs: StepRun[]) {
  const c = runs.map((r) => r.copy).filter(Boolean) as NonNullable<StepRun["copy"]>[];
  if (!c.length) return null;
  const sum = (k: "rows_read" | "rows_copied" | "rows_skipped") => c.reduce((a, x) => a + (x[k] ?? 0), 0);
  return `${num(sum("rows_read"))} read · ${num(sum("rows_copied"))} copied${sum("rows_skipped") ? ` · ${num(sum("rows_skipped"))} skipped` : ""}`;
}

function StepRow({ step, onPath, depth }: { step: Step; onPath: Set<string>; depth: number }) {
  const hot = onPath.has(step.name);
  const nested = step.children.length > 0 || (step.called?.length ?? 0) > 0;
  const [open, setOpen] = useState(hot);
  const failed = step.runs.filter((r) => r.status === "Failed");
  const secs = step.runs.reduce((a, r) => a + (r.duration_s ?? 0), 0);
  const copy = copyLine(step.runs);
  return (
    <li>
      <div className={cn("flex items-start gap-2 rounded-lg px-2 py-1.5", hot && step.status === "Failed" && "bg-[var(--critical-soft)]")}
        style={{ paddingLeft: `${0.5 + depth * 1.125}rem` }}>
        <button type="button" onClick={() => setOpen(!open)} disabled={!nested} aria-expanded={nested ? open : undefined}
          aria-label={nested ? `${open ? "Collapse" : "Expand"} ${step.name}` : undefined}
          className={cn("mt-0.5 grid size-5 shrink-0 place-items-center rounded text-ink-3", nested ? "hover:bg-surface-3 hover:text-ink" : "invisible")}>
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        </button>
        <span className="mt-1.5 size-2 shrink-0 rounded-full" style={{ background: STATUS_COLOR[step.status] ?? "var(--warning)" }} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className={cn("text-[0.8125rem] font-semibold", step.status === "NotRun" ? "text-ink-3" : "text-ink")}>{step.name}</span>
            <span className="rounded bg-surface-2 px-1.5 py-px text-[0.65625rem] text-ink-3 shadow-[inset_0_0_0_1px_var(--line)]">{step.type}</span>
            {step.branch && <span className="inline-flex items-center gap-1 text-[0.6875rem] text-ink-3"><GitBranch className="size-3" />{step.branch}</span>}
            {step.iterations > 1 && <span className="text-[0.6875rem] text-ink-3">×{step.iterations}{failed.length ? `, ${failed.length} failed` : ""}</span>}
            <span className="text-[0.6875rem] text-ink-3">{step.status === "NotRun" ? "not run" : secs ? duration(secs) : ""}</span>
          </div>
          {copy && <p className="mt-0.5 text-[0.71875rem] text-ink-2">{copy}</p>}
          {failed.length > 0 && failed[failed.length - 1]!.error && (
            <p className="mt-1 break-words rounded-md bg-[var(--critical-soft)] px-2 py-1 font-mono text-[0.6875rem] leading-5 text-[var(--critical-ink)]">
              {failed[failed.length - 1]!.error_code ? `${failed[failed.length - 1]!.error_code}: ` : ""}{failed[failed.length - 1]!.error}
            </p>
          )}
        </div>
      </div>
      {open && nested && (
        <ul>
          {step.children.map((c) => <StepRow key={`${c.branch}-${c.name}`} step={c} onPath={onPath} depth={depth + 1} />)}
          {step.called?.map((c) => (
            <li key={c.run_id}>
              <p className="flex items-center gap-1.5 py-1 text-[0.71875rem] font-semibold text-ink-2" style={{ paddingLeft: `${2.25 + (depth + 1) * 1.125}rem` }}>
                <Workflow className="size-3.5 text-accent" /> Child pipeline {c.pipeline}
              </p>
              {c.error ? <p className="text-xs text-[var(--warning-ink)]" style={{ paddingLeft: `${2.25 + (depth + 1) * 1.125}rem` }}>{c.error}</p>
                : <ul>{c.steps?.map((s) => <StepRow key={s.name} step={s} onPath={onPath} depth={depth + 2} />)}</ul>}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function RunTree({ project, runId }: { project: string; runId: string }) {
  const { data, error, reload } = useApi<Tree>(`/api/projects/${project}/runs/${encodeURIComponent(runId)}/tree`);
  const [view, setView] = useState<"graph" | "list">(() => {
    try { return localStorage.getItem("alertbot.runView") === "list" ? "list" : "graph"; } catch { return "graph"; }
  });
  const pick = (v: "graph" | "list") => { setView(v); try { localStorage.setItem("alertbot.runView", v); } catch { /* ignore */ } };
  const onPath = new Set((data?.failure_path ?? []).map((p) => p.name));
  return (
    <ChartCard icon={Workflow} title="This run, step by step" subtitle={data ? `${data.pipeline} · laid out like ADF Studio · red = failed, green = succeeded, faded = didn't run` : "Reading the run from Azure Data Factory…"}
      error={error} onRetry={reload}
      actions={data && (
        <div className="flex items-center gap-2">
          <Segmented label="View" size="sm" value={view} onChange={pick} options={[{ value: "graph", label: "Graph" }, { value: "list", label: "List" }]} />
          <Button size="sm" variant="secondary" asChild><a href={data.monitor_url} target="_blank" rel="noreferrer"><ExternalLink />Open in ADF Studio</a></Button>
        </div>
      )}>
      {!data && !error ? <div className="space-y-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-7" />)}</div> : data && (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
            <StatusChip status={data.status} />
            {data.failure_path.length > 0 && (
              <span className="text-ink-2">
                {data.failure_path.map((p, i) => (
                  <span key={i}>{i > 0 && <ChevronRight className="mx-0.5 inline size-3 text-ink-3" />}<b className="text-ink">{p.name}</b>
                    {p.iteration ? <span className="text-ink-3"> (iteration {p.iteration} of {p.of})</span> : null}</span>
                ))}
              </span>
            )}
          </div>
          {view === "graph" ? <PipelineGraph steps={data.steps} />
            : <ul className="-mx-2">{data.steps.map((s) => <StepRow key={s.name} step={s} onPath={onPath} depth={0} />)}</ul>}
          <RunTables tables={(data.tables ?? []).filter((t) => t.kind !== "files")} />
          {!data.steps.length && <Notice>This pipeline has no steps in its definition.</Notice>}
        </>
      )}
    </ChartCard>
  );
}
