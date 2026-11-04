// A project's mapping of which pipeline loads which table (on which connected database): the allowlist of tables whose
// data is checked. Suggestions come from lineage; anyone on the project can map a table on a database they connected.
import { ArrowRight, Database, History, Link2, Pause, Pencil, Pin, Play, Plus, Sparkles, Trash2, Workflow } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { ChartCard } from "@/components/Cards";
import { ConnStatus, KindIcon, MappingDialog, settingsSummary, type DbConn, type Mapping, type Suggestion } from "@/components/Databases";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { del, patch, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

export interface MappingList { mappings: Mapping[]; suggestions: Suggestion[]; pipelines: string[]; connections: DbConn[]; me: number }

export function DqMappings({ projectKey, owner, onChanged }: { projectKey: string; owner: boolean; onChanged: () => void }) {
  const { data, error, reload } = useApi<MappingList>(`/api/projects/${projectKey}/mappings`);
  const [dialog, setDialog] = useState<{ editing?: Mapping; start?: Suggestion } | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const changed = () => { reload(); onChanged(); };

  const toggle = async (m: Mapping) => {
    setBusy(m.id);
    try { await patch(`/api/projects/${projectKey}/mappings/${m.id}`, { enabled: !m.enabled }); changed(); }
    finally { setBusy(null); }
  };
  const remove = async (m: Mapping) => {
    if (!window.confirm(`Stop checking ${m.schema}.${m.table}? Its history stays.`)) return;
    try { await del(`/api/projects/${projectKey}/mappings/${m.id}`); toast.success(`${m.table} is no longer checked`); changed(); }
    catch (e) { toast.error("Couldn't remove it", { description: (e as Error).message }); }
  };
  const keep = async (m: Mapping) => {
    setBusy(m.id);
    try {
      await patch(`/api/projects/${projectKey}/mappings/${m.id}`, { follow_azure: false });
      toast.success(`${m.table} is checked again`, { description: `Kept for ${m.pipeline} even though Azure's definition doesn't show it loading the table.` });
      changed();
    } finally { setBusy(null); }
  };
  const quickMap = async (sg: Suggestion) => {
    setBusy(sg.connection_id);
    try {
      await post(`/api/projects/${projectKey}/mappings`, { pipeline: sg.pipeline, connection_id: sg.connection_id, schema: sg.schema, table: sg.table, settings: {} });
      toast.success(`${sg.table} mapped to ${sg.pipeline}`);
      changed();
    } catch (e) {
      toast.error("Couldn't map it", { description: (e as Error).message });
    } finally { setBusy(null); }
  };

  const all = data?.mappings ?? [];
  const maps = [...all.filter((m) => !m.retired), ...all.filter((m) => m.retired)];
  const live = all.filter((m) => !m.retired).length;
  const conns = data?.connections ?? [];
  const sugg = data?.suggestions ?? [];
  return (
    <ChartCard flush className="mb-4" icon={Workflow} title="Which pipeline loads which table"
      subtitle={data ? (maps.length ? `${live} link${live === 1 ? "" : "s"} checked after their pipeline loads${maps.length > live ? ` · ${maps.length - live} retired because Azure changed` : ""} · only counts and percentages are read`
        : "Map a table to the pipeline that loads it, and its data is checked after every load") : undefined}
      actions={conns.length ? <Button size="sm" variant="primary" onClick={() => setDialog({})}><Plus />Map a table</Button>
        : <Button size="sm" variant="primary" asChild><Link to="/databases"><Database />Connect a database</Link></Button>}
      error={error} onRetry={reload} loading={!data && !error} skeleton={<div className="space-y-2 px-6">{[0, 1].map((i) => <Skeleton key={i} className="h-11 w-full" />)}</div>}>
      {data && (
        <>
          {!conns.length && !maps.length && (
            <p className="border-t border-line px-6 py-4 text-sm text-ink-2">
              Connect the database your pipelines load first (SQL Server, Azure SQL or Snowflake). It stays connected, and the tables each pipeline writes are suggested here.
            </p>
          )}
          {sugg.length > 0 && (
            <div className="border-t border-line bg-accent-soft/40 px-6 py-3">
              <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-ink"><Sparkles className="size-3.5 text-accent" />From this project's lineage: tables your pipelines load on your databases</p>
              <ul className="space-y-1.5">
                {sugg.map((sg) => (
                  <li key={`${sg.pipeline}-${sg.object}-${sg.connection_id}`} className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="truncate text-ink-2">{sg.pipeline}</span><ArrowRight className="size-3.5 text-ink-3" />
                    <span className="truncate font-semibold text-ink">{sg.schema}.{sg.table}</span>
                    <span className="text-xs text-ink-3">on {sg.connection}</span>
                    <span className="ml-auto flex gap-1">
                      <Button size="sm" variant="ghost" onClick={() => setDialog({ start: sg })}>Settings</Button>
                      <Button size="sm" variant="secondary" disabled={busy === sg.connection_id} onClick={() => void quickMap(sg)}><Link2 />Map</Button>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {maps.length > 0 && (
            <div className="scrollbar-thin overflow-x-auto border-t border-line">
              <table className="data-table min-w-[44rem]">
                <thead><tr><th>Pipeline</th><th>Loads</th><th>Database</th><th>Checks</th><th className="text-right" aria-label="Actions" /></tr></thead>
                <tbody>
                  {maps.map((m) => {
                    const summary = settingsSummary(m.settings);
                    const mine = conns.some((c) => c.id === m.connection_id);
                    return (
                      <tr key={m.id} className={cn((!m.enabled || m.retired) && "opacity-60")}>
                        <td><span className={cn("flex items-center gap-1.5 font-medium text-ink", m.retired && "line-through decoration-[var(--line-strong)]")}><Workflow className="size-3.5 text-ink-3" />{m.pipeline}</span></td>
                        <td><span className="text-ink-3">{m.schema}.</span><span className="font-semibold text-ink">{m.table}</span>
                          {m.automatic && <span className="ml-2 rounded-full bg-accent-soft px-1.5 py-px text-[0.625rem] font-semibold text-accent" title="Mapped by the bot from this pipeline's lineage; edit or remove it like any other">added automatically</span>}</td>
                        <td>
                          <span className="flex min-w-0 items-center gap-1.5"><KindIcon kind={m.connection_kind} className="size-3.5 shrink-0 text-ink-3" /><span className="truncate text-ink-2">{m.connection}</span></span>
                          <ConnStatus compact c={{ status: m.connection_status as "ok", last_ok_at: null, last_error: null }} />
                        </td>
                        <td className="max-w-[20rem] text-xs text-ink-2">{m.retired
                          ? <span className="flex items-start gap-1.5"><History className="mt-px size-3.5 shrink-0 text-ink-3" aria-hidden /><span><b className="font-semibold text-ink">Retired, not checked.</b> {m.retired_reason} It comes back by itself if Azure shows the load again.</span></span>
                          : !m.enabled ? "Paused" : summary.length ? summary.join(" · ") : "Standard checks"}
                          {!m.retired && m.follow_azure === false && <span className="mt-0.5 block text-[0.6875rem] text-ink-3">Kept by hand: checked whatever Azure's definition says</span>}</td>
                        <td className="text-right">
                          <span className="inline-flex gap-0.5">
                            <Button size="icon-sm" variant="ghost" onClick={() => setDialog({ editing: m })} aria-label="Edit checks" title={mine ? "Edit its checks" : "Only the database's owner can change its checks"}><Pencil /></Button>
                            {m.retired
                              ? <Button size="icon-sm" variant="ghost" disabled={busy === m.id} onClick={() => void keep(m)} aria-label="Keep checking anyway" title="Keep checking it anyway (the pipeline loads it in a way Azure's definition doesn't show)"><Pin /></Button>
                              : <Button size="icon-sm" variant="ghost" disabled={busy === m.id} onClick={() => void toggle(m)} aria-label={m.enabled ? "Pause" : "Resume"} title={m.enabled ? "Pause checks" : "Resume checks"}>{m.enabled ? <Pause /> : <Play />}</Button>}
                            {(owner || mine) && <Button size="icon-sm" variant="ghost" onClick={() => void remove(m)} aria-label="Remove" title="Stop checking this table"><Trash2 /></Button>}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {dialog && data && (
        <MappingDialog open onClose={() => setDialog(null)} projectKey={projectKey} pipelines={data.pipelines} connections={conns}
          editing={dialog.editing} start={dialog.start ? { pipeline: dialog.start.pipeline, connection_id: dialog.start.connection_id, schema: dialog.start.schema, table: dialog.start.table } : undefined}
          onSaved={changed} />
      )}
    </ChartCard>
  );
}
