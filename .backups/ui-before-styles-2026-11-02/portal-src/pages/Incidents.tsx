// The incident register: every incident the bot has ever opened, never removed: for all your projects (/incidents, with
// a project filter) or for one (/p/:key/incidents). Each row is the full
// record: when it opened, who acknowledged it and when, whether and how it was resolved and by whom, and the comments.
// "Needs attention" counts only what nobody has acknowledged yet; the dock badge counts the same.
import { CircleCheck, Download, Hand, MessageSquareText, Search, Siren } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useShell } from "@/app/shell";
import { shortName } from "@/app/projects";
import { ChartCard, Metric, MetricStrip, PageIntro } from "@/components/Cards";
import { SEVERITY_META, SeverityPill } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Input, Segmented, Select, Skeleton } from "@/components/ui/primitives";
import { ago, between, checkLabel, fullDate, time } from "@/lib/format";
import { useApiEach, useNow } from "@/lib/hooks";
import type { IncidentBrief } from "@/lib/types";
import { cn } from "@/lib/utils";

type Group = "all" | "attention" | "acknowledged" | "resolved";
type Row = IncidentBrief & { project: string; project_name: string };

const STATE = {
  open: { label: "Needs attention", color: "var(--critical)" },
  acknowledged: { label: "Acknowledged", color: "var(--accent)" },
  resolved: { label: "Resolved", color: "var(--good)" },
} as const;

export function RegisterStatus({ i, className }: { i: Pick<IncidentBrief, "status" | "severity">; className?: string }) {
  const s = STATE[i.status];
  const color = i.status === "open" ? SEVERITY_META[i.severity]?.color ?? s.color : s.color;
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[0.6875rem] font-semibold", className)}
      style={{ background: `color-mix(in srgb, ${color} 12%, transparent)`, color: `color-mix(in srgb, ${color} 72%, var(--ink))`, boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${color} 30%, transparent)` }}>
      <span className={cn("size-1.5 rounded-full", i.status === "open" && "animate-pulse")} style={{ background: color }} aria-hidden />
      {s.label}
    </span>
  );
}

/** "14 Oct 2026" over "13:10", or a dash. */
function When({ at, by, empty = "–" }: { at?: string | null; by?: string | null; empty?: string }) {
  if (!at) return <span className="text-ink-3">{empty}</span>;
  return (
    <div className="min-w-0 leading-tight">
      {by ? <div className="truncate text-[0.78125rem] font-semibold text-ink" title={by}>{by}</div> : null}
      <div className="num whitespace-nowrap text-[0.71875rem] text-ink-2" title={new Date(at).toString()}>{fullDate(at)} · {time(at)}</div>
    </div>
  );
}

const csvCell = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""').replace(/\r?\n/g, " ")}"`;

function exportCsv(rows: Row[], project: string) {
  const head = ["Incident", "Project", "Title", "Severity", "Status", "Checks", "Scope", "Opened", "Acknowledged by", "Acknowledged at",
    "Resolved by", "Resolved at", "How it was resolved", "Open for", "Comments", "Latest comment", "Likely cause"];
  const lines = rows.map((i) => [i.id, i.project_name, i.title, i.severity, STATE[i.status].label, i.checks.map(checkLabel).join("; "), i.scope_label, i.opened_at,
    i.acknowledged_by ?? "", i.acknowledged_at ?? "", i.resolved_by ?? "", i.resolved_at ?? "", i.resolution ?? "",
    between(i.opened_at, i.resolved_at ?? new Date().toISOString()), i.comments ?? 0,
    i.last_comment ? `${i.last_comment.by}: ${i.last_comment.text}` : "", i.likely_cause].map(csvCell).join(","));
  const blob = new Blob([[head.map(csvCell).join(","), ...lines].join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `incident-register-${project}-${new Date().toISOString().slice(0, 10)}.csv` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function Incidents() {
  const { key = "" } = useParams();
  const { me } = useShell();
  const nav = useNavigate();
  const [group, setGroup] = useState<Group>("all");
  const [q, setQ] = useState("");
  const [only, setOnly] = useState("");  // all projects: narrow to one
  const projects = key ? me.projects.filter((p) => p.key === key) : me.projects;
  const each = useApiEach<IncidentBrief[]>(projects.map((p) => `/api/projects/${p.key}/incidents?status=all&limit=500`), { refreshMs: 30000 });
  const all = { ...each, data: each.data ? each.data.flatMap((rows, k) => (rows ?? []).map((i) => ({ ...i, project: projects[k]!.key, project_name: shortName(projects[k]!) }))) : null };
  const many = !key && projects.length > 1;
  useNow();
  const list: Row[] = (all.data ?? []).filter((i) => !only || i.project === only).sort((a, b) => b.opened_at.localeCompare(a.opened_at));
  const attention = list.filter((i) => i.status === "open");
  const acked = list.filter((i) => i.status === "acknowledged");
  const resolved = list.filter((i) => i.status === "resolved");
  const sev = (s: string) => attention.filter((i) => i.severity === s).length;

  const rows = useMemo(() => {
    const s = q.trim().toLowerCase();
    return list
      .filter((i) => group === "all" || (group === "attention" ? i.status === "open" : i.status === group))
      .filter((i) => !s || [i.id, i.title, i.scope_label, i.acknowledged_by, i.resolved_by, i.resolution, i.likely_cause, i.last_comment?.text, ...i.checks.map(checkLabel)]
        .some((v) => String(v ?? "").toLowerCase().includes(s)));
  }, [list, group, q]);

  const title = !all.data ? "Incidents"
    : attention.length ? `${attention.length} incident${attention.length === 1 ? " needs" : "s need"} attention`
    : acked.length ? "Nothing new needs attention" : "All clear";

  return (
    <div>
      <PageIntro eyebrow={<><Siren className="size-3.5" /> Incidents</>} title={title}>
        <span>
          {all.data && acked.length ? <><b className="font-semibold text-ink">{acked.length} more acknowledged</b> and being handled: they stay open until they're resolved. </> : null}
          Every incident the bot has opened stays in the register below, with who acknowledged it, who resolved it and how, and every comment.
        </span>
      </PageIntro>

      <MetricStrip className="mb-4" cols="grid-cols-2 lg:grid-cols-4">
        <Metric icon={Siren} iconColor="var(--critical)" label="Needs attention" value={all.data ? attention.length : "–"}
          footnote={attention.length ? `${sev("critical")} critical · ${sev("warning")} warning · nobody on it yet` : "nothing unacknowledged"}
          onClick={() => setGroup("attention")} pressed={group === "attention"} />
        <Metric icon={Hand} iconColor="var(--accent)" label="Acknowledged" value={all.data ? acked.length : "–"} footnote="someone's on it, not resolved yet"
          onClick={() => setGroup("acknowledged")} pressed={group === "acknowledged"} />
        <Metric icon={CircleCheck} iconColor="var(--good)" label="Resolved" value={all.data ? resolved.length : "–"} footnote="by the bot or by a person"
          onClick={() => setGroup("resolved")} pressed={group === "resolved"} />
        <Metric icon={Siren} label="All incidents" value={all.data ? list.length : "–"} footnote="the full register, since the bot started"
          onClick={() => setGroup("all")} pressed={group === "all"} />
      </MetricStrip>

      <ChartCard flush icon={Siren} title="Incident register"
        subtitle={all.data ? `${rows.length} of ${list.length} shown · newest first · nothing is ever removed` : undefined}
        loading={all.loading && !all.data} error={all.error} onRetry={all.reload}
        skeleton={<div className="space-y-2 px-6 pb-6">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-14 w-full" />)}</div>}
        actions={<Button size="sm" variant="ghost" onClick={() => exportCsv(rows, key || only || "all-projects")} disabled={!rows.length}><Download /> Export CSV</Button>}
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            <Segmented<Group> size="sm" label="Show" value={group} onChange={setGroup}
              options={[{ value: "all", label: `All ${list.length}` }, { value: "attention", label: `Needs attention ${attention.length}` },
                { value: "acknowledged", label: `Acknowledged ${acked.length}` }, { value: "resolved", label: `Resolved ${resolved.length}` }]} />
            {many && (
              <Select value={only} onChange={(e) => setOnly(e.target.value)} aria-label="Project" className="w-56">
                <option value="">All {projects.length} projects</option>
                {projects.map((p) => <option key={p.key} value={p.key}>{shortName(p)}</option>)}
              </Select>
            )}
            <div className="relative ml-auto">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a title, person, comment…" aria-label="Find in the register" className="w-72 max-w-full pl-8" />
            </div>
          </div>
        }
        empty={!!all.data && !rows.length} emptyIcon={CircleCheck} emptyTone="good"
        emptyText={q ? "Nothing in the register matches that." : group === "attention" ? "Nothing needs attention: every open incident has someone on it." : "No incidents here yet."}>

        {/* wide screens: the full record as a table */}
        <div className="scrollbar-thin hidden overflow-x-auto md:block">
          <table className="data-table reg-table min-w-[74rem]">
            <thead>
              <tr>
                <th className="w-14">#</th><th>Incident</th>{many && <th>Project</th>}<th>Severity</th><th>Status</th><th>Opened</th>
                <th>Acknowledged</th><th>Resolved</th><th>Open for</th><th>Comments</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => {
                const m = SEVERITY_META[i.severity] ?? SEVERITY_META.warning!;
                return (
                  <tr key={`${i.project}-${i.id}`} className="cursor-pointer" onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) nav(`/p/${i.project}/i/${i.id}`); }}>
                    <td className="num align-top text-ink-3">#{i.id}</td>
                    <td className="align-top">
                      <div className="flex min-w-[16rem] max-w-[24rem] items-start gap-2.5">
                        <m.icon className="mt-0.5 size-4 shrink-0" style={{ color: i.status === "resolved" ? "var(--good)" : m.color }} aria-hidden />
                        <div className="min-w-0">
                          <Link to={`/p/${i.project}/i/${i.id}`} className={cn("block font-semibold leading-5 [overflow-wrap:anywhere] hover:underline", i.status === "resolved" ? "text-ink-2" : "text-ink")}>{i.title}</Link>
                          <div className="mt-0.5 text-[0.6875rem] text-ink-3 [overflow-wrap:anywhere]">{i.checks.map(checkLabel).join(" · ")}</div>
                        </div>
                      </div>
                    </td>
                    {many && <td className="align-top whitespace-nowrap text-[0.78125rem] text-ink-2">{i.project_name}</td>}
                    <td className="align-top"><SeverityPill severity={i.severity} /></td>
                    <td className="align-top"><RegisterStatus i={i} /></td>
                    <td className="align-top"><When at={i.opened_at} /><div className="text-[0.6875rem] text-ink-3">{ago(i.opened_at)}</div></td>
                    <td className="align-top"><When at={i.acknowledged_at} by={i.acknowledged_by} empty={i.status === "resolved" ? "–" : "Not yet"} /></td>
                    <td className="align-top">
                      <div className="max-w-[15rem]">
                        <When at={i.resolved_at} by={i.resolved_by} empty="Not yet" />
                        {i.resolution && <p className="mt-1 line-clamp-2 text-[0.6875rem] leading-4 text-ink-3" title={i.resolution}>{i.resolution}</p>}
                      </div>
                    </td>
                    <td className="num align-top whitespace-nowrap text-ink-2">{between(i.opened_at, i.resolved_at ?? new Date().toISOString())}{i.resolved_at ? "" : " so far"}</td>
                    <td className="align-top">
                      {i.comments ? (
                        <div className="max-w-[15rem]">
                          <span className="inline-flex items-center gap-1 text-[0.71875rem] font-semibold text-ink"><MessageSquareText className="size-3.5 text-ink-3" aria-hidden />{i.comments}</span>
                          {i.last_comment && <p className="mt-0.5 line-clamp-2 text-[0.6875rem] leading-4 text-ink-2" title={`${i.last_comment.by}: ${i.last_comment.text}`}><b className="font-semibold">{i.last_comment.by}:</b> {i.last_comment.text}</p>}
                        </div>
                      ) : <span className="text-ink-3">None</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* phones: the same record as cards */}
        <ul className="pb-1 md:hidden">
          {rows.map((i) => (
            <li key={`${i.project}-${i.id}`} className="border-t border-line first:border-t-0">
              <Link to={`/p/${i.project}/i/${i.id}`} className="block px-5 py-3.5 hover:bg-row-hover">
                <div className="flex items-start justify-between gap-2">
                  <span className="min-w-0 text-sm font-semibold text-ink [overflow-wrap:anywhere]"><span className="num mr-1.5 font-normal text-ink-3">#{i.id}</span>{i.title}</span>
                  <SeverityPill severity={i.severity} />
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2"><RegisterStatus i={i} />{many && <span className="text-[0.6875rem] font-semibold text-ink-2">{i.project_name}</span>}<span className="text-[0.6875rem] text-ink-3">{i.checks.map(checkLabel).join(" · ")}</span></div>
                <dl className="mt-2.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.71875rem]">
                  <dt className="text-ink-3">Opened</dt><dd className="num text-ink-2">{fullDate(i.opened_at)} · {time(i.opened_at)}</dd>
                  <dt className="text-ink-3">Acknowledged</dt><dd className="text-ink-2">{i.acknowledged_by ? `${i.acknowledged_by} · ${fullDate(i.acknowledged_at)} ${time(i.acknowledged_at)}` : "Not yet"}</dd>
                  <dt className="text-ink-3">Resolved</dt><dd className="text-ink-2">{i.resolved_at ? `${i.resolved_by ?? "Alert Bot"} · ${fullDate(i.resolved_at)} ${time(i.resolved_at)}` : "Not yet"}</dd>
                  {i.resolution && <><dt className="text-ink-3">How</dt><dd className="text-ink-2">{i.resolution}</dd></>}
                  <dt className="text-ink-3">Comments</dt><dd className="text-ink-2">{i.comments ? `${i.comments} · latest from ${i.last_comment?.by}` : "None"}</dd>
                </dl>
              </Link>
            </li>
          ))}
        </ul>
      </ChartCard>
    </div>
  );
}
