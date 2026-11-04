// Your databases: connect once, stay connected (the bot reconnects when you sign in), see what each one feeds and map
// the tables your pipelines load. Checks are aggregate-only: counts and percentages, never rows.
import { ArrowRight, ChevronRight, Database, History, Link2, Pencil, Plus, RefreshCw, ShieldAlert, Sparkles, Trash2, Workflow } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { ChartCard, PageIntro } from "@/components/Cards";
import { ConnStatus, ConnectDialog, KIND_LABEL, KindIcon, MappingDialog, PrivacyPromise, type DbConn, type Suggestion } from "@/components/Databases";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { api, del, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface DbList { connections: DbConn[]; suggestions: Suggestion[] }

export function Databases() {
  const { data, error, reload, setData } = useApi<DbList>("/api/databases");
  const [connectOpen, setConnectOpen] = useState(false);
  const [editing, setEditing] = useState<DbConn | null>(null);
  const [mapping, setMapping] = useState<Suggestion | null>(null);
  const [pipes, setPipes] = useState<string[]>([]);
  const [busy, setBusy] = useState<number | "all" | null>(null);

  const test = async (c: DbConn) => {
    setBusy(c.id);
    try {
      const r = await post<{ ok: boolean; error: string | null; connection: DbConn }>(`/api/databases/${c.id}/test`);
      setData((d) => d && { ...d, connections: d.connections.map((x) => (x.id === c.id ? r.connection : x)) });
      if (r.ok) toast.success(`${c.name} is connected`); else toast.error(`Can't connect to ${c.name}`, { description: r.error ?? undefined });
    } finally { setBusy(null); }
  };
  const remove = async (c: DbConn) => {
    const n = c.mappings.filter((m) => !m.retired).length;
    if (!window.confirm(`Disconnect ${c.name}?${n ? `\n\nIts ${n} mapped table${n > 1 ? "s" : ""} will stop being checked.` : ""}\nThe stored password is deleted.`)) return;
    await del(`/api/databases/${c.id}`);
    toast.success(`${c.name} disconnected`);
    reload();
  };
  const quickMap = async (sg: Suggestion) => {  // one click: map with defaults (settings can be refined later)
    setBusy(sg.connection_id);
    try {
      await post(`/api/projects/${sg.project}/mappings`, { pipeline: sg.pipeline, connection_id: sg.connection_id, schema: sg.schema, table: sg.table, settings: {} });
      toast.success(`${sg.table} mapped to ${sg.pipeline}`, { description: `${sg.project_name}: checked after each load from now on.`,
        action: { label: "Fine-tune", onClick: () => void openMap(sg) } });
      reload();
    } catch (e) {
      toast.error("Couldn't map it", { description: (e as Error).message, action: { label: "Open", onClick: () => void openMap(sg) } });
    } finally { setBusy(null); }
  };
  const mapAll = async () => {
    if (!data) return;
    setBusy("all");
    let ok = 0;
    for (const sg of data.suggestions.filter((x) => x.match === "exact")) {
      try { await post(`/api/projects/${sg.project}/mappings`, { pipeline: sg.pipeline, connection_id: sg.connection_id, schema: sg.schema, table: sg.table, settings: {} }); ok++; }
      catch { /* shown in the list on reload */ }
    }
    setBusy(null);
    toast.success(`Mapped ${ok} table${ok === 1 ? "" : "s"}`);
    reload();
  };
  const openMap = async (sg: Suggestion) => {
    const r = await api<{ pipelines: string[] }>(`/api/projects/${sg.project}/mappings`);
    setPipes(r.pipelines);
    setMapping(sg);
  };

  const conns = data?.connections ?? [];
  const sugg = data?.suggestions ?? [];
  const exact = sugg.filter((s) => s.match === "exact").length;
  return (
    <div>
      <PageIntro eyebrow={<><Database className="size-3.5" /> Databases</>} title="Your databases"
        actions={conns.length ? <Button variant="primary" onClick={() => { setEditing(null); setConnectOpen(true); }}><Plus />Connect a database</Button> : undefined}>
        Connect the databases your pipelines load, once. They stay connected: the bot reconnects whenever you sign in, and checks each
        mapped table's data after every load. It only ever reads counts and percentages.
      </PageIntro>

      {error && !data && <ChartCard title="Databases" error={error} onRetry={reload} />}
      {!data && !error && <div className="grid gap-4 md:grid-cols-2">{[0, 1].map((i) => <Skeleton key={i} className="h-48 w-full rounded-[1.25rem]" />)}</div>}

      {data && !conns.length && (
        <section className="card-in surface-hero relative overflow-hidden rounded-[1.5rem] border border-line p-8 shadow-card">
          <div className="max-w-2xl">
            <span className="flex size-12 items-center justify-center rounded-2xl bg-accent-soft text-accent"><Database className="size-6" /></span>
            <h2 className="display mt-4 text-2xl font-bold text-ink">Connect your first database</h2>
            <p className="mt-2 text-sm leading-6 text-ink-2">
              SQL Server, Azure SQL or Snowflake. Sign in once with a login that can read the tables; the bot then checks row counts, empty values,
              duplicates, freshness, schema, ranges, rules and references inside the database, after each pipeline load.
            </p>
            <Button variant="primary" size="lg" className="mt-5" onClick={() => setConnectOpen(true)}><Plus />Connect a database</Button>
          </div>
          <PrivacyPromise className="mt-7" />
        </section>
      )}

      {conns.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-2">
          {conns.map((c, i) => (
            <section key={c.id} style={{ animationDelay: `${i * 40}ms` }}
              className={cn("card-in flex min-w-0 flex-col rounded-[1.25rem] border bg-surface shadow-card", c.status === "error" ? "border-[color-mix(in_srgb,var(--critical)_40%,var(--line))]" : "border-line")}>
              <header className="flex items-start gap-3 px-5 pb-3 pt-4">
                <span className={cn("flex size-11 shrink-0 items-center justify-center rounded-2xl", c.kind === "snowflake" ? "bg-[color-mix(in_srgb,#29b5e8_16%,transparent)] text-[#29b5e8]" : "bg-accent-soft text-accent")}>
                  <KindIcon kind={c.kind} className="size-5" />
                </span>
                <div className="min-w-0 flex-1">
                  <h2 className="display truncate text-base font-semibold text-ink">{c.name}</h2>
                  <p className="truncate text-xs text-ink-3">{KIND_LABEL[c.kind]} · <span className="font-mono">{c.host}{c.port && c.port !== 1433 ? `:${c.port}` : ""}</span> · {c.database}</p>
                  <div className="mt-1.5"><ConnStatus c={c} /></div>
                </div>
                <div className="flex shrink-0 items-center gap-0.5">
                  <Button size="icon-sm" variant="ghost" onClick={() => void test(c)} disabled={busy === c.id} aria-label="Test the connection" title="Test the connection">
                    <RefreshCw className={cn(busy === c.id && "animate-spin")} />
                  </Button>
                  <Button size="icon-sm" variant="ghost" onClick={() => { setEditing(c); setConnectOpen(true); }} aria-label="Edit" title="Edit sign-in or options"><Pencil /></Button>
                  <Button size="icon-sm" variant="ghost" onClick={() => void remove(c)} aria-label="Disconnect" title="Disconnect"><Trash2 /></Button>
                </div>
              </header>
              {c.status === "error" && c.last_error && (
                <p className="mx-5 mb-3 rounded-xl bg-[var(--critical-soft)] px-3 py-2 text-xs text-ink">{c.last_error}</p>
              )}
              {c.can_write && (
                <p className="mx-5 mb-3 flex items-start gap-2 rounded-xl bg-[var(--warning-soft)] px-3 py-2 text-[0.71875rem] leading-4 text-ink">
                  <ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-[var(--warning-ink)]" />
                  <span>This login can change data. The bot never will: every query is a fixed, rolled-back SELECT. A read-only login is still the safest choice.</span>
                </p>
              )}
              <div className="mt-auto border-t border-line px-5 py-3">
                <CardMappings c={c} />
                <p className="mt-2 text-[0.6875rem] text-ink-3">Signed in as {c.username || "your Microsoft account"}{c.last_used_at ? ` · used ${ago(c.last_used_at)}` : ""}</p>
              </div>
            </section>
          ))}
        </div>
      )}

      {sugg.length > 0 && (
        <ChartCard className="mt-5" flush icon={Sparkles} title="Your pipelines load these tables"
          subtitle="Found from each pipeline's own definition (lineage), on databases you connected. Map them to start checking their data."
          actions={exact > 1 ? <Button size="sm" variant="primary" disabled={busy === "all"} onClick={() => void mapAll()}><Link2 />Map all {exact}</Button> : undefined}>
          <ul className="divide-y divide-line border-t border-line">
            {sugg.map((sg) => (
              <li key={`${sg.project}-${sg.pipeline}-${sg.object}-${sg.connection_id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-6 py-3">
                <span className="flex min-w-0 basis-full items-center gap-2 text-sm sm:basis-0 sm:flex-1">
                  <Workflow className="size-4 shrink-0 text-ink-3" />
                  <span className="shrink-0 text-ink-2">{sg.pipeline}</span>
                  <ArrowRight className="size-3.5 shrink-0 text-ink-3" />
                  <span className="min-w-0 truncate font-semibold text-ink">{sg.schema}.{sg.table}</span>
                  <span className="hidden shrink-0 text-xs text-ink-3 sm:inline">on {sg.connection}</span>
                </span>
                <span className="text-xs text-ink-3">{sg.project_name}</span>
                {sg.match === "database" && <span className="rounded-full bg-surface-3 px-2 py-0.5 text-[0.65625rem] text-ink-2" title="The pipeline's connection doesn't name its server; matched by database name">probable</span>}
                <Button size="sm" variant="ghost" onClick={() => void openMap(sg)}>Settings</Button>
                <Button size="sm" variant="secondary" disabled={busy === sg.connection_id} onClick={() => void quickMap(sg)}><Link2 />Map</Button>
              </li>
            ))}
          </ul>
        </ChartCard>
      )}

      <ConnectDialog open={connectOpen} editing={editing} onClose={() => { setConnectOpen(false); setEditing(null); }}
        onSaved={() => reload()} />
      {mapping && (
        <MappingDialog open onClose={() => setMapping(null)} projectKey={mapping.project} pipelines={pipes} connections={conns}
          start={{ pipeline: mapping.pipeline, connection_id: mapping.connection_id, schema: mapping.schema, table: mapping.table }} onSaved={reload} />
      )}
    </div>
  );
}


/** What a database feeds: the live pipeline → table links, then (folded) the ones Azure no longer has, with why. */
function CardMappings({ c }: { c: DbConn }) {
  const live = c.mappings.filter((m) => !m.retired);
  const old = c.mappings.filter((m) => m.retired);
  const [open, setOpen] = useState(false);
  const pipes = new Set(live.map((m) => `${m.project}|${m.pipeline}`)).size;
  return (
    <>
      <p className="eyebrow mb-1.5 text-ink-3">
        {live.length ? `Feeds ${live.length} checked link${live.length > 1 ? "s" : ""} · ${pipes} pipeline${pipes > 1 ? "s" : ""}` : "No table mapped yet"}
      </p>
      {live.length ? (
        <ul className="space-y-1">
          {live.slice(0, 8).map((m) => (
            <li key={m.mapping_id}>
              <Link to={`/p/${m.project}/tables?tab=quality`} className={cn("group flex min-w-0 items-center gap-1.5 text-xs text-ink-2 hover:text-ink", m.enabled === false && "opacity-60")}>
                <Workflow className="size-3.5 shrink-0 text-ink-3" /><span className="max-w-[45%] shrink-0 truncate">{m.pipeline}</span>
                <ArrowRight className="size-3 shrink-0 text-ink-3" /><span className="min-w-0 truncate font-medium text-ink">{m.schema}.{m.table}</span>
                {m.enabled === false && <span className="shrink-0 rounded bg-surface-3 px-1 text-[0.5625rem] font-semibold text-ink-3">paused</span>}
                <span className="ml-auto hidden min-w-0 truncate pl-2 text-[0.6875rem] text-ink-3 group-hover:text-ink-2 sm:inline">{m.project_name}</span>
              </Link>
            </li>
          ))}
          {live.length > 8 && <li className="text-[0.6875rem] text-ink-3">+{live.length - 8} more in each project's Tables › Data quality</li>}
        </ul>
      ) : <p className="text-xs text-ink-3">Map the tables your pipelines load here (suggestions below), or from a project's Tables › Data quality.</p>}
      {old.length > 0 && (
        <div className="mt-2.5 rounded-xl bg-surface-2 shadow-[inset_0_0_0_1px_var(--line)]">
          <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
            className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[0.6875rem] text-ink-3 hover:text-ink">
            <History className="size-3.5 shrink-0" aria-hidden />
            <span className="min-w-0 flex-1">{old.length} older link{old.length > 1 ? "s" : ""} retired: Azure changed (not checked, kept for history)</span>
            <ChevronRight className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")} aria-hidden />
          </button>
          {open && (
            <ul className="space-y-1.5 border-t border-line px-2.5 py-2">
              {old.map((m) => (
                <li key={m.mapping_id} className="text-[0.6875rem] leading-4">
                  <span className="text-ink-2 line-through decoration-[var(--line-strong)]">{m.pipeline} → {m.schema}.{m.table}</span>
                  <span className="block text-ink-3">{m.retired_reason}{m.retired_at ? ` · ${ago(m.retired_at)}` : ""}</span>
                </li>
              ))}
              <li className="pt-0.5 text-[0.65625rem] text-ink-3">A retired link comes back by itself if the pipeline loads the table again.</li>
            </ul>
          )}
        </div>
      )}
    </>
  );
}
