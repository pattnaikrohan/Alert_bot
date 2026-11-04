import { Check, Columns3, Database, FileWarning, ShieldCheck, Table2 } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { ChartCard, Em, Notice, PageIntro } from "@/components/Cards";
import { SeverityPill } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { post } from "@/lib/api";
import { dateTime, num, pct } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { TableView } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useProject } from "@/app/Layout";
import { DataQuality } from "@/components/DataQuality";
import { SuspiciousValues } from "@/components/Suspicious";
import { TableLoads } from "@/components/TableLoads";

export function Tables() {
  const { key = "" } = useParams();
  const project = useProject(key);
  if (project?.kind === "adf") return <AzureTables projectKey={key} owner={project.role === "owner"} service={project.service ?? "Data Factory"} />;
  return <SchemaTables projectKey={key} owner={project?.role === "owner"} />;
}

function AzureTables({ projectKey, owner, service }: { projectKey: string; owner: boolean; service: string }) {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "quality" ? "quality" : params.get("tab") === "suspicious" ? "suspicious" : "loads";
  if (service === "Logic Apps") {
    return (
      <div>
        <PageIntro eyebrow={<><Table2 className="size-3.5" /> Tables</>} title="Logic Apps don't load tables">
          Table loads come from the copy steps of Data Factory and Synapse pipelines. For this project's workflows, see Runs.
        </PageIntro>
        <Button variant="primary" asChild><Link to={`/p/${projectKey}/runs`}>Open runs</Link></Button>
      </div>
    );
  }
  return (
    <div>
      <PageIntro eyebrow={<><Table2 className="size-3.5" /> Tables</>} title="What went into your tables">
        Exactly how many rows each table received, per load and per day, against what's normal for it, and whether the data inside passes its checks.
      </PageIntro>
      <div className="mb-5 flex items-center gap-1 border-b border-line" role="tablist" aria-label="Tables view">
        {([["loads", "Rows inserted", Database], ["quality", "Data quality", ShieldCheck], ["suspicious", "Suspicious values", FileWarning]] as const).map(([id, label, Icon]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setParams(id === "loads" ? {} : { tab: id }, { replace: true })}
            className={cn("-mb-px flex items-center gap-2 border-b-2 px-3.5 pb-2.5 pt-1 text-sm transition-colors",
              tab === id ? "border-accent font-semibold text-ink" : "border-transparent text-ink-2 hover:text-ink")}>
            <Icon className="size-4" aria-hidden />{label}
          </button>
        ))}
      </div>
      {tab === "loads" ? <TableLoads projectKey={projectKey} /> : tab === "suspicious" ? <SuspiciousValues projectKey={projectKey} /> : <DataQuality projectKey={projectKey} owner={owner} />}
    </div>
  );
}

function SchemaTables({ projectKey: key, owner }: { projectKey: string; owner: boolean }) {
  const { data, error, reload, setData } = useApi<TableView[]>(`/api/projects/${key}/tables`);
  const [busy, setBusy] = useState(false);

  const accept = async (id: string) => {
    setBusy(true);
    try {
      setData(await post<TableView[]>(`/api/projects/${key}/tables/${id}/accept-schema`));
      toast.success("New schema accepted", { description: "The incident resolves on the bot's next round." });
    } catch (e) {
      toast.error("Couldn't accept the schema", { description: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageIntro eyebrow={<><Table2 className="size-3.5" /> Tables</>} title="What the bot reads">
        The scraped output tables, the schema the bot expects (changes raise an incident), and each site's latest measurements.
      </PageIntro>
      {error && !data ? <Notice tone="warn">Couldn't load tables: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice> : null}
      {!data && !error ? <Skeleton className="h-80 w-full" /> : null}
      <div className="space-y-4">
        {data?.map((t) => {
          const d = t.schema.diff;
          const changed = new Set([...d.added, ...d.retyped.map((r) => r.column)]);
          return (
            <div key={t.id} className="space-y-4">
              <ChartCard icon={Database} title={<span className="font-mono text-[0.9375rem]">{t.table}</span>}
                subtitle={`key ${t.key.join(" + ")} · grouped by ${t.group_by} · loaded at ${t.loaded_at}`}
                actions={t.schema.drifted ? <SeverityPill severity="warning" label="Schema changed" /> : <SeverityPill severity="ok" label="Schema stable" />}>
                {t.schema.drifted ? (
                  <Notice tone="warn" className="mb-4" action={owner ? <Button size="sm" variant="primary" disabled={busy} onClick={() => accept(t.id)}><Check /> Accept new schema</Button> : null}>
                    <Em>The table changed since its schema was accepted {dateTime(t.schema.accepted_at)}.</Em>{" "}
                    {d.added.length ? <>Added <code className="font-mono">{d.added.join(", ")}</code>. </> : null}
                    {d.removed.length ? <>Removed <code className="font-mono">{d.removed.join(", ")}</code>. </> : null}
                    {d.retyped.map((r) => <span key={r.column}>Retyped <code className="font-mono">{r.column}</code> {r.from} → {r.to}. </span>)}
                    {!owner ? <span className="text-ink-3">Only project owners can accept it.</span> : null}
                  </Notice>
                ) : null}
                <div className="flex flex-wrap gap-1.5">
                  {t.schema.live.map((c) => (
                    <span key={c.name} title={c.type} className={cn("inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[0.75rem] shadow-[inset_0_0_0_1px_var(--line)]",
                      changed.has(c.name) ? "bg-warning-soft text-ink" : "bg-surface-2 text-ink")}>
                      <Columns3 className={cn("size-3", t.required_fields.includes(c.name) ? "text-accent" : "text-ink-3")} aria-hidden />
                      <span className="font-medium">{c.name}</span>
                      <span className="font-mono text-[0.65625rem] text-ink-3">{c.type.toLowerCase()}</span>
                    </span>
                  ))}
                  {d.removed.map((c) => <span key={c} className="inline-flex items-center rounded-lg bg-critical-soft px-2 py-1 text-[0.75rem] text-critical line-through">{c}</span>)}
                </div>
                <p className="mt-3 flex items-center gap-1.5 text-[0.6875rem] text-ink-3"><Columns3 className="size-3 text-accent" aria-hidden /> watched for empty values</p>
              </ChartCard>

              <ChartCard flush icon={Table2} title="Latest measurements by site" subtitle="From each site's most recent measured run">
                <div className="scrollbar-thin overflow-auto">
                  <table className="data-table min-w-[44rem]">
                    <thead>
                      <tr><th>Site</th><th className="text-right">Rows</th>{t.required_fields.map((f) => <th key={f} className="text-right">Empty {f}</th>)}<th className="text-right">Duplicate keys</th><th>Measured</th></tr>
                    </thead>
                    <tbody>
                      {t.sites.map((s) => (
                        <tr key={s.site}>
                          <td className="font-medium text-ink">{s.site}</td>
                          <td className="num-cell">{num(s.rows)}</td>
                          {t.required_fields.map((f) => <td key={f} className={cn("num-cell", (s.null_rates[f] ?? 0) > 0.05 && "font-semibold text-critical")}>{pct(s.null_rates[f])}</td>)}
                          <td className="num-cell">{pct(s.duplicate_rate)}</td>
                          <td className="num whitespace-nowrap text-xs text-ink-3">{dateTime(s.at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </ChartCard>
            </div>
          );
        })}
      </div>
    </div>
  );
}
