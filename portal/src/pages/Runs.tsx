// Runs: every pipeline run across all your projects (/runs, newest first, with a project filter), or one project's runs
// with each run's step-by-step view (/p/:key/runs).
import { ArrowRight, ListChecks } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { shortName } from "@/app/projects";
import { useShell } from "@/app/shell";
import { ChartCard, PageIntro } from "@/components/Cards";
import { FixCard } from "@/components/FixCard";
import { RunTree } from "@/components/RunTree";
import { RuntimeBar, RuntimeChip } from "@/components/RuntimeVsNormal";
import { StatusChip } from "@/components/Status";
import { Segmented, Select, Skeleton } from "@/components/ui/primitives";
import { ago, dateTime, duration, num } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useApi, useApiEach } from "@/lib/hooks";
import type { RunRow } from "@/lib/types";

export function Runs() {
  const { key = "" } = useParams();
  return key ? <ProjectRuns projectKey={key} /> : <AllRuns />;
}

type RunsReply = { runs: RunRow[]; sites: string[]; kind?: string };

/** Every project's runs in one list. A run opens in its project's run view, step by step. */
function AllRuns() {
  const { me } = useShell();
  const nav = useNavigate();
  const [only, setOnly] = useState("");
  const [status, setStatus] = useState<"all" | "failed" | "running">("all");
  const projects = only ? me.projects.filter((p) => p.key === only) : me.projects;
  const each = useApiEach<RunsReply>(projects.map((p) => `/api/projects/${p.key}/runs?status=${status}&limit=${only ? 300 : 120}`), { refreshMs: 30000 });
  const rows = each.data
    ? each.data.flatMap((r, k) => (r?.runs ?? []).map((run) => ({ ...run, project: projects[k]!.key, project_name: shortName(projects[k]!), adf: r?.kind === "adf" })))
      .sort((a, b) => (b.requested_at ?? "").localeCompare(a.requested_at ?? ""))
    : null;
  const open = (r: NonNullable<typeof rows>[number]) =>
    nav(`/p/${r.project}/runs?${new URLSearchParams({ pipeline: r.site, ...(r.adf && r.run_id ? { run: r.run_id } : {}) })}`);
  return (
    <div>
      <PageIntro eyebrow={<><ListChecks className="size-3.5" /> Runs</>} title="Every pipeline run, across your projects">
        Newest first. Click a run to open it step by step in its project.
      </PageIntro>
      <ChartCard flush icon={ListChecks} title="Run history" subtitle={rows ? `${num(rows.length)} runs · newest first` : undefined}
        loading={each.loading && !each.data} error={each.error} onRetry={each.reload} empty={!!rows && !rows.length} emptyText="No runs match these filters."
        skeleton={<div className="space-y-2">{Array.from({ length: 10 }, (_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>}
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            {me.projects.length > 1 && (
              <Select value={only} onChange={(e) => setOnly(e.target.value)} aria-label="Project" className="w-56">
                <option value="">All {me.projects.length} projects</option>
                {me.projects.map((p) => <option key={p.key} value={p.key}>{shortName(p)}</option>)}
              </Select>
            )}
            <div className="ml-auto">
              <Segmented size="sm" label="Status" value={status} onChange={setStatus} options={[{ value: "all", label: "All" }, { value: "failed", label: "Failed" }, { value: "running", label: "In flight" }]} />
            </div>
          </div>
        }>
        <div className="scrollbar-thin max-h-[42.5rem] overflow-auto">
          <table className="data-table min-w-[52rem]">
            <thead><tr><th>Started</th><th>Project</th><th>Pipeline</th><th>Status</th><th className="text-right">Duration</th><th className="text-right">Rows</th><th className="w-8" /></tr></thead>
            <tbody>
              {rows?.map((r) => (
                <tr key={`${r.project}-${r.id}`} onClick={() => open(r)} className="cursor-pointer">
                  <td className="whitespace-nowrap"><span className="num text-ink">{dateTime(r.requested_at)}</span><span className="num block text-[0.6875rem] text-ink-3">{ago(r.requested_at)}</span></td>
                  <td className="whitespace-nowrap text-[0.78125rem] text-ink-2">{r.project_name}</td>
                  <td className="whitespace-nowrap font-medium text-ink">{r.site}</td>
                  <td className="max-w-[22rem]">
                    {r.run_status ? <StatusChip status={r.run_status} /> : <StatusChip status={r.trigger_status} />}
                    {r.error ? <p className="mt-1 line-clamp-2 text-[0.71875rem] text-critical" title={r.error}>{r.error}</p> : null}
                  </td>
                  <td className="num-cell">{duration(r.duration_s ?? r.vs_normal?.elapsed_s)}</td>
                  <td className="num-cell">{num(r.rows_loaded)}</td>
                  <td><ArrowRight className="size-4 text-ink-3" aria-hidden /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ChartCard>
    </div>
  );
}

function ProjectRuns({ projectKey: key }: { projectKey: string }) {
  const [params] = useSearchParams();
  const [site, setSite] = useState(() => params.get("pipeline") ?? "");
  const [status, setStatus] = useState<"all" | "failed" | "running">("all");
  const q = new URLSearchParams({ status, limit: "300", ...(site ? { site } : {}) });
  const { data, error, reload, loading } = useApi<{ runs: RunRow[]; sites: string[]; kind?: string }>(`/api/projects/${key}/runs?${q}`, { refreshMs: 30000 });
  const adf = data?.kind === "adf";
  const [open, setOpen] = useState<string | null>(() => params.get("run"));
  const detail = useRef<HTMLDivElement>(null);
  useEffect(() => { if (open && adf) detail.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [open, adf]);

  return (
    <div>
      {adf ? (
        <PageIntro eyebrow={<><ListChecks className="size-3.5" /> Runs</>} title="Every pipeline run">
          Each row is one pipeline or Logic App run. Click a run to see it step by step, laid out like the designer in Azure.
        </PageIntro>
      ) : (
        <PageIntro eyebrow={<><ListChecks className="size-3.5" /> Runs</>} title="Every scrape, end to end">
          Each row is one trigger-table request joined with its Data Factory pipeline run and the rows it loaded into staging.
        </PageIntro>
      )}
      <ChartCard flush icon={ListChecks} title="Run history" subtitle={data ? `${num(data.runs.length)} runs · newest first` : undefined}
        loading={loading && !data} error={error} onRetry={reload} empty={!!data && !data.runs.length} emptyText="No runs match these filters."
        skeleton={<div className="space-y-2">{Array.from({ length: 10 }, (_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>}
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            <Select value={site} onChange={(e) => setSite(e.target.value)} aria-label={adf ? "Pipeline" : "Site"} className="w-56">
              <option value="">{adf ? "All pipelines" : "All sites"}</option>
              {[...new Set([...(data?.sites ?? []), ...(site ? [site] : [])])].map((s) => <option key={s}>{s}</option>)}
            </Select>
            <div className="ml-auto">
              <Segmented size="sm" label="Status" value={status} onChange={setStatus} options={[{ value: "all", label: "All" }, { value: "failed", label: "Failed" }, { value: "running", label: "In flight" }]} />
            </div>
          </div>
        }>
        <div className="scrollbar-thin max-h-[42.5rem] overflow-auto">
          <table className="data-table min-w-[56rem]">
            <thead>
              {adf
                ? <tr><th>Started</th><th>Pipeline</th><th>Started by</th><th>Status</th><th className="text-right">Duration</th><th>vs normal</th><th className="text-right">Rows copied</th><th>Run id</th></tr>
                : <tr><th>Requested</th><th>Site</th><th>Trigger</th><th>Pipeline run</th><th className="text-right">Duration</th><th className="text-right">Rows</th><th>Run id</th></tr>}
            </thead>
            <tbody>
              {data?.runs.map((r) => (
                <tr key={r.id} onClick={adf && r.run_id ? () => setOpen(open === r.run_id ? null : r.run_id) : undefined}
                  className={cn(adf && r.run_id && "cursor-pointer", open && open === r.run_id && "bg-[var(--accent-soft)]")}
                  aria-selected={adf ? open === r.run_id : undefined}>
                  <td className="whitespace-nowrap"><span className="num text-ink">{dateTime(r.requested_at)}</span><span className="num block text-[0.6875rem] text-ink-3">{ago(r.requested_at)}</span></td>
                  <td className="whitespace-nowrap font-medium text-ink">{r.site}</td>
                  <td>{adf ? <span className="text-xs text-ink-2">{r.pipeline?.split(" · ").reverse().join(" · ") || "—"}</span> : <StatusChip status={r.trigger_status} />}</td>
                  <td className="max-w-[22rem]">
                    {r.run_status ? <StatusChip status={r.run_status} /> : <span className="text-xs text-ink-3">not started</span>}
                    {r.error ? <p className="mt-1 line-clamp-2 text-[0.71875rem] text-critical" title={r.error}>{r.error}</p> : null}
                  </td>
                  <td className="num-cell">{duration(r.duration_s ?? r.vs_normal?.elapsed_s)}</td>
                  {adf && <td><RuntimeChip r={r.vs_normal} /></td>}
                  <td className="num-cell">{num(r.rows_loaded)}</td>
                  <td className="font-mono text-[0.6875rem] text-ink-3">{r.run_id ? r.run_id.slice(0, 8) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ChartCard>
      {adf && open && (() => {
        const r = data?.runs.find((x) => x.run_id === open);
        return (
          <div ref={detail} className="mt-4 scroll-mt-20 space-y-4">
            {r?.vs_normal && (
              <section className="rounded-[1.25rem] border border-line bg-surface p-5 shadow-card">
                <p className="mb-3 text-[0.8125rem] font-semibold text-ink">{r.site}: this run against its normal run time</p>
                <RuntimeBar r={r.vs_normal} />
              </section>
            )}
            {r?.error && <FixCard project={key} runId={open} />}
            <RunTree project={key} runId={open} />
          </div>
        );
      })()}
    </div>
  );
}
