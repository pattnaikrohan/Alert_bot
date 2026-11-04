// Every Data Factory / Synapse pipeline and Logic App the person can see, across all their projects, whether it has run
// or not: where it lives, what starts it, its latest run and the last week. Click a row for its runs.
import {
  Boxes, CheckCircle2, CircleDashed, CirclePause, Clock3, Globe, Hand, Loader2, NotebookPen, OctagonAlert, RefreshCw, Search, SquareFunction,
  TriangleAlert, Workflow, Zap,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { refreshEverything } from "@/app/actions";
import { RuntimeChip } from "@/components/RuntimeVsNormal";
import { ChartCard, Notice, PageIntro } from "@/components/Cards";
import { StatusChip } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Input, Segmented, Skeleton } from "@/components/ui/primitives";
import { ago, dateTime, duration } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

type State = "failing" | "handled" | "unstable" | "running" | "healthy" | "idle" | "never_run" | "disabled";
interface Trigger { name: string; kind: string; started: boolean; every_hours: number | null }
interface Item {
  name: string; project: string; project_name: string; service: string; type: "pipeline" | "logic_app";
  folder: string | null; description: string; steps: number | null; triggers: Trigger[]; enabled: boolean; watched: boolean;
  state: State; last_run: { at: string; status: string; duration_s: number | null; run_id: string | null } | null;
  runs: number; failed: number; succeeded: number; incident: { id: number; title: string; severity: string; status?: string } | null;
  runtime?: import("@/components/RuntimeVsNormal").VsNormal | null;
  handled?: { kind: "acknowledged" | "resolved"; by: string | null; at: string | null; incident_id: number; title: string; note: string | null } | null;
  connections?: { kind: string; name: string; steps: string[]; function?: string | null; project?: string; project_name?: string }[];
  called_by?: { name: string; project: string; project_name: string }[];
}

const CONN: Record<string, { label: string; icon: typeof Zap; color: string }> = {
  logic_app: { label: "Logic App", icon: Zap, color: "var(--nav-pipelines)" },
  function_app: { label: "Function App", icon: SquareFunction, color: "var(--nav-report)" },
  pipeline: { label: "Pipeline", icon: Workflow, color: "var(--nav-overview)" },
  databricks: { label: "Databricks", icon: Boxes, color: "var(--nav-incidents)" },
  notebook: { label: "Notebook", icon: NotebookPen, color: "var(--nav-tables)" },
  web: { label: "Web call", icon: Globe, color: "var(--nav-ask)" },
};

/** What a pipeline calls (Logic Apps, Function Apps, other pipelines…) and, for a Logic App, the pipelines that call it. */
function Connections({ i }: { i: Item }) {
  const conns = (i.connections ?? []).filter((c) => c.kind !== "web" || (i.connections ?? []).length < 4);
  if (!conns.length && !i.called_by?.length) return null;
  return (
    <span className="mt-1 flex flex-wrap items-center gap-1">
      {conns.slice(0, 4).map((c) => {
        const m = CONN[c.kind] ?? CONN.web!;
        const body = (
          <span className="inline-flex max-w-[14rem] items-center gap-1 rounded-md px-1.5 py-0.5 text-[0.6875rem] font-medium text-ink"
            style={{ background: `color-mix(in srgb, ${m.color} 13%, transparent)` }}
            title={`${m.label} ${c.name}${c.function ? ` · function ${c.function}` : ""} · called by step ${c.steps.join(", ")}`}>
            <m.icon className="size-3 shrink-0" style={{ color: m.color }} aria-hidden /><span className="truncate">{c.name}</span>
          </span>
        );
        return c.project
          ? <Link key={`${c.kind}${c.name}`} to={`/p/${c.project}/runs?pipeline=${encodeURIComponent(c.name)}`} onClick={(e) => e.stopPropagation()} className="hover:opacity-80">{body}</Link>
          : <span key={`${c.kind}${c.name}`}>{body}</span>;
      })}
      {conns.length > 4 && <span className="text-[0.6875rem] text-ink-3">+{conns.length - 4}</span>}
      {i.called_by?.map((c) => (
        <Link key={`${c.project}/${c.name}`} to={`/p/${c.project}/runs?pipeline=${encodeURIComponent(c.name)}`} onClick={(e) => e.stopPropagation()}
          className="inline-flex items-center gap-1 rounded-md bg-surface-3 px-1.5 py-0.5 text-[0.6875rem] text-ink-2 hover:text-ink" title={`Called by ${c.name} (${c.project_name})`}>
          <Workflow className="size-3" aria-hidden />called by {c.name}
        </Link>
      ))}
    </span>
  );
}
interface View {
  days: number; items: Item[];
  sources: { project: string; project_name: string; service: string; count: number; checked_at: string | null; error: string | null; gone?: string[] }[];
  counts: Record<State | "total", number>; services: Record<string, number>;
}

const STATE: Record<State, { label: string; icon: typeof CheckCircle2; color: string }> = {
  failing: { label: "Failing", icon: OctagonAlert, color: "var(--critical)" },
  handled: { label: "Being handled", icon: Hand, color: "var(--accent)" },
  unstable: { label: "Failed this week, fine since", icon: TriangleAlert, color: "var(--warning)" },
  running: { label: "Running now", icon: Loader2, color: "var(--accent)" },
  healthy: { label: "Healthy", icon: CheckCircle2, color: "var(--good)" },
  idle: { label: "No runs this week", icon: Clock3, color: "var(--ink-3)" },
  never_run: { label: "No runs on record", icon: CircleDashed, color: "var(--ink-3)" },
  disabled: { label: "Disabled", icon: CirclePause, color: "var(--ink-3)" },
};

function every(h: number) {
  if (h < 1) return `every ${Math.round(h * 60)} min`;
  if (h < 24) return `every ${+h.toFixed(1)} h`;
  return h === 24 ? "daily" : h === 168 ? "weekly" : `every ${+(h / 24).toFixed(1)} days`;
}

function TriggerText({ triggers }: { triggers: Trigger[] }) {
  if (!triggers.length) return <span className="text-ink-3">Manual only</span>;
  const t = triggers.find((x) => x.started) ?? triggers[0]!;
  return (
    <span title={triggers.map((x) => `${x.name}: ${x.kind}${x.every_hours ? ` (${every(x.every_hours)})` : ""}${x.started ? "" : " · stopped"}`).join("\n")}>
      <span className={cn(!t.started && "text-ink-3 line-through")}>{t.kind}</span>
      {t.every_hours ? <span className="text-ink-3"> · {every(t.every_hours)}</span> : null}
      {triggers.length > 1 ? <span className="text-ink-3"> +{triggers.length - 1}</span> : null}
    </span>
  );
}

function Week({ i, days }: { i: Item; days: number }) {
  const total = i.succeeded + i.failed;
  if (!i.runs) return <span className="text-xs text-ink-3">–</span>;
  return (
    <div className="min-w-[7rem]" title={`${i.succeeded} succeeded · ${i.failed} failed in the last ${days} days`}>
      <div className="flex h-1.5 gap-0.5 overflow-hidden rounded-full bg-surface-3">
        {i.succeeded > 0 && <span className="h-full" style={{ width: `${(i.succeeded / Math.max(1, total)) * 100}%`, background: "var(--chart-ok)" }} />}
        {i.failed > 0 && <span className="h-full" style={{ width: `${(i.failed / Math.max(1, total)) * 100}%`, background: "var(--chart-bad)" }} />}
      </div>
      <p className="mt-1 text-[0.6875rem] text-ink-2"><b className="text-ink">{i.runs}</b> runs{i.failed ? <span className="text-[var(--critical-ink)]"> · {i.failed} failed</span> : ""}</p>
    </div>
  );
}

export function AllPipelines() {
  const [days] = useState(7);
  const { data, error, reload } = useApi<View>(`/api/pipelines?days=${days}`, { refreshMs: 60000 });
  const [q, setQ] = useState("");
  const [service, setService] = useState("all");
  const [state, setState] = useState<State | "all">("all");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  const items = useMemo(() => (data?.items ?? []).filter((i) =>
    (service === "all" || i.service === service) && (state === "all" || i.state === state)
    && (!q || `${i.name} ${i.folder ?? ""} ${i.project_name} ${i.description}`.toLowerCase().includes(q.toLowerCase()))), [data, q, service, state]);
  const services = Object.entries(data?.services ?? {}).filter(([, n]) => n > 0);

  const refresh = async () => {
    setBusy(true);
    try {
      await refreshEverything();
      reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageIntro eyebrow={<><Workflow className="size-3.5" /> All pipelines</>} title="Every pipeline and Logic App you can see">
        Data Factory and Synapse pipelines and Logic Apps across all your projects, including the ones that haven't run, and the Logic Apps
        and Function Apps each pipeline calls. Click one for its runs.
      </PageIntro>

      {data && (
        <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5 2xl:grid-cols-9">
          {(["all", "failing", "handled", "unstable", "running", "healthy", "idle", "never_run", "disabled"] as const).map((k) => {
            const s = k === "all" ? null : STATE[k];
            const n = k === "all" ? data.counts.total : data.counts[k] ?? 0;
            const Icon = s?.icon ?? Workflow;
            return (
              <button key={k} onClick={() => setState(k)} aria-pressed={state === k}
                className={cn("rounded-2xl bg-surface p-3.5 text-left shadow-[inset_0_0_0_1px_var(--line)] transition-colors hover:bg-surface-2 light:bg-white",
                  state === k && "shadow-[inset_0_0_0_2px_var(--accent)]")}>
                <span className="flex items-center gap-1.5 text-xs text-ink-3"><Icon className="size-3.5" style={{ color: s?.color }} aria-hidden />{s?.label ?? "All"}</span>
                <span className="display mt-1 block text-2xl font-bold tabular-nums text-ink">{n}</span>
              </button>
            );
          })}
        </div>
      )}

      {data?.sources.filter((s) => s.gone?.length).map((s) => (
        <Notice key={`gone-${s.project}`} className="mb-3">
          <b>{s.gone!.join(", ")}</b> {s.gone!.length > 1 ? "are" : "is"} no longer in {s.project_name} on Azure (renamed or deleted), so {s.gone!.length > 1 ? "they're" : "it's"} not listed.
          Past runs stay under <Link to={`/p/${s.project}/runs`} className="font-semibold text-accent hover:underline">Runs</Link>.
        </Notice>
      ))}
      {data?.sources.filter((s) => s.error).map((s) => (
        <Notice key={s.project} tone="warn" className="mb-3"><b>{s.project_name}</b>: couldn't read its list from Azure ({s.error}). Showing what the bot last saw.</Notice>
      ))}

      <ChartCard flush icon={Workflow} title="Pipelines and Logic Apps"
        subtitle={data ? `${items.length} of ${data.counts.total} · ${data.sources.length} project${data.sources.length === 1 ? "" : "s"} · last ${data.days} days` : undefined}
        actions={<Button size="sm" variant="secondary" disabled={busy} onClick={refresh}><RefreshCw className={cn(busy && "animate-spin")} />{busy ? "Refreshing…" : "Refresh everything"}</Button>}
        error={error} onRetry={reload} loading={!data && !error}
        skeleton={<div className="space-y-2 px-6">{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>}
        empty={!!data && !data.counts.total}
        emptyText={<>No pipelines yet. The bot lists every Data Factory, Synapse pipeline and Logic App your Azure account can see; new ones appear within a few minutes of being found.</>}
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name, folder or project" aria-label="Search" className="w-72 pl-8" />
            </div>
            {services.length > 1 && (
              <Segmented label="Service" value={service} onChange={setService}
                options={[{ value: "all", label: "All" }, ...services.map(([s, n]) => ({ value: s, label: `${s} (${n})` }))]} />
            )}
          </div>
        }>
        {items.length === 0 ? <p className="px-6 pb-6 text-sm text-ink-3">Nothing matches these filters.</p> : (
          <div className="scrollbar-thin overflow-auto">
            <table className="data-table min-w-[56rem]">
              <thead>
                <tr><th>Name</th><th>Project</th><th>Started by</th><th>Latest run</th><th>Last {data?.days} days</th><th>Status</th></tr>
              </thead>
              <tbody>
                {items.map((i) => {
                  const s = STATE[i.state];
                  const to = `/p/${i.project}/runs?pipeline=${encodeURIComponent(i.name)}`;
                  return (
                    <tr key={`${i.project}/${i.name}`} className="cursor-pointer" onClick={() => nav(to)}>
                      <td className="max-w-[15rem]">
                        <Link to={to} onClick={(e) => e.stopPropagation()} className="flex items-center gap-1.5 font-semibold text-ink hover:underline">
                          {i.type === "logic_app" ? <Zap className="size-3.5 shrink-0 text-ink-3" aria-label="Logic App" /> : <Workflow className="size-3.5 shrink-0 text-ink-3" aria-label="Pipeline" />}
                          <span className="truncate">{i.name}</span>
                        </Link>
                        <span className="block truncate text-[0.6875rem] text-ink-3">
                          {[i.folder, i.steps != null ? `${i.steps} step${i.steps === 1 ? "" : "s"}` : null, !i.watched && i.enabled ? "not watched" : null].filter(Boolean).join(" · ") || i.description || " "}
                        </span>
                        <Connections i={i} />
                      </td>
                      <td className="whitespace-nowrap">
                        <span className="block max-w-[12rem] truncate text-[0.8125rem] text-ink" title={i.project_name}>{i.project_name}</span>
                        <span className="block text-[0.6875rem] text-ink-3">{i.service}</span>
                      </td>
                      <td className="text-xs"><TriggerText triggers={i.triggers} /></td>
                      <td className="whitespace-nowrap">
                        {i.last_run ? (
                          <span title={dateTime(i.last_run.at)}>
                            <StatusChip status={i.last_run.status} />
                            <span className="ml-1.5 text-xs text-ink-3">{ago(i.last_run.at)}{i.last_run.duration_s ? ` · ${duration(i.last_run.duration_s)}` : ""}</span>
                            {i.runtime && <RuntimeChip r={i.runtime} className="ml-1.5" />}
                          </span>
                        ) : <span className="text-xs text-ink-3" title="No run since the bot started watching (it reads the last 7 days when it starts)">None on record</span>}
                      </td>
                      <td><Week i={i} days={data!.days} /></td>
                      <td className="whitespace-nowrap">
                        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-ink">
                          <s.icon className={cn("size-3.5", i.state === "running" && "animate-spin")} style={{ color: s.color }} aria-hidden />{s.label}
                        </span>
                        {i.handled ? (
                          <Link to={`/p/${i.project}/i/${i.handled.incident_id}`} onClick={(e) => e.stopPropagation()}
                            className="mt-0.5 block max-w-[16rem] truncate text-[0.6875rem] text-ink-2 hover:underline"
                            title={`${i.handled.title}${i.handled.note ? `: ${i.handled.note}` : ""}`}>
                            {i.handled.kind === "acknowledged" ? `Acknowledged by ${i.handled.by ?? "someone"}` : `Resolved by ${i.handled.by}; next run will confirm`}
                          </Link>
                        ) : i.incident && <Link to={`/p/${i.project}/i/${i.incident.id}`} onClick={(e) => e.stopPropagation()}
                          className="mt-0.5 block max-w-[14rem] truncate text-[0.6875rem] text-[var(--critical-ink)] hover:underline" title={i.incident.title}>{i.incident.title}</Link>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </ChartCard>
      {data && (
        <p className="mt-3 text-[0.6875rem] text-ink-3">
          Lists from Azure: {data.sources.map((s) => `${s.project_name} ${s.checked_at ? ago(s.checked_at) : "not read yet"}`).join(" · ")}
          {" · "}Changes made in ADF Studio or Synapse Studio show here once they're published (Publish all).
        </p>
      )}
    </div>
  );
}
