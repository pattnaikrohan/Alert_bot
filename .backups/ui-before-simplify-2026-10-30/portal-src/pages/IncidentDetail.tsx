import { BadgeCheck, BellRing, Check, ChevronLeft, ChevronRight, CircleCheck, ClipboardList, FileSearch, History, LineChart, ListChecks, MessageSquareText, Network, Search, Send, ServerCog, Siren, Sparkles, Wrench } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { AdaptiveCardPreview } from "@/components/AdaptiveCardPreview";
import { ChartCard, Em, Facts, Notice, PageIntro } from "@/components/Cards";
import { MetricChart } from "@/components/Charts";
import { FixCard } from "@/components/FixCard";
import { Remediation } from "@/components/Remediation";
import { RuntimeBar } from "@/components/RuntimeVsNormal";
import { Modal } from "@/components/Databases";
import { RunTree } from "@/components/RunTree";
import { SEVERITY_META, SeverityPill, StatusChip } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { post } from "@/lib/api";
import { ago, between, checkLabel, dateTime, duration, fullDate, initials, num, pct, time } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import type { FailingCheck, IncidentDetail as Detail, SeriesPoint } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useProject } from "@/app/Layout";
import { RegisterStatus } from "./Incidents";

const fmtFor = (metric: string) => (metric === "null_rate" || metric === "duplicate_rate" ? (v: number) => pct(v) : metric === "duration_s" ? (v: number) => duration(v) : num);

function MetricPanel({ project, site, metric, field, label, output }: { project: string; site: string; metric: string; field: string; label: string; output?: string }) {
  const q = new URLSearchParams({ site, metric, field, limit: "40", ...(output ? { output } : {}) });
  const { data } = useApi<{ points: SeriesPoint[] }>(`/api/projects/${project}/series?${q}`);
  return (
    <div>
      <h3 className="mb-2 text-[0.8125rem] font-semibold text-ink">{label}</h3>
      {data ? <MetricChart points={data.points} label={label} format={fmtFor(metric)} /> : <Skeleton className="h-56 w-full" />}
    </div>
  );
}

function RangeBar({ c }: { c: FailingCheck }) {
  if (c.actual == null || c.expected_high == null) return null;
  const lo = c.expected_low ?? 0;
  const max = Math.max(c.actual, c.expected_high) * 1.15 || 1;
  const p = (v: number) => `${Math.min(100, Math.max(0, (v / max) * 100))}%`;
  const fmt = c.check === "null_spike" || c.check === "duplicates" ? (v: number) => pct(v) : c.check === "run_too_slow" ? duration : num;
  return (
    <div className="mt-2.5">
      <div className="relative h-2 rounded-full bg-surface-3" aria-hidden>
        <div className="absolute inset-y-0 rounded-full bg-accent-soft shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--accent)_55%,transparent)]" style={{ left: p(lo), width: `calc(${p(c.expected_high)} - ${p(lo)})` }} />
        <div className="absolute -top-[3px] size-3.5 -translate-x-1/2 rounded-full border-2 border-surface bg-critical shadow-[0_0_0_3px_var(--critical-soft)]" style={{ left: p(c.actual) }} />
      </div>
      <div className="num mt-1.5 flex justify-between text-[0.6875rem] text-ink-3">
        <span>Actual <b className="font-semibold text-ink">{fmt(c.actual)}</b></span>
        <span>Expected {c.expected_low != null ? `${fmt(c.expected_low)} – ` : "≤ "}{fmt(c.expected_high)}</span>
      </div>
    </div>
  );
}

/** Plain text with any https:// links made clickable (e.g. the ADF Studio link in a suggested action). */
function Linkified({ text, bold = false }: { text: string; bold?: boolean }) {
  // the first line of "why" is the headline; links become buttons-in-text
  const [first, ...rest] = bold ? [text.split("\n")[0] ?? "", text.split("\n").slice(1).join("\n")] : [text];
  const render = (t: string) => t.split(/(https:\/\/\S+)/g).map((p, i) => (p.startsWith("https://")
    ? <a key={i} href={p} target="_blank" rel="noreferrer" className="font-medium text-accent underline-offset-2 hover:underline">{p.includes("adf.azure.com") ? "Open the run in ADF Studio" : p}</a>
    : <span key={i}>{p}</span>));
  return bold ? <><span className="font-semibold">{render(first!)}</span>{rest[0] ? <>{"\n"}{render(rest[0])}</> : null}</> : <>{render(text)}</>;
}

const shortModel = (m: string) => m.split("/").pop()!.replace(/:free$/, "");

export function IncidentDetail() {
  const { key = "", id = "" } = useParams();
  const { data, error, reload, setData } = useApi<Detail>(`/api/projects/${key}/incidents/${id}`, { refreshMs: 30000 });
  const [busy, setBusy] = useState(false);
  const [openTool, setOpenTool] = useState<number | null>(null);
  const [resolving, setResolving] = useState(false);
  const [note, setNote] = useState("");
  const [comment, setComment] = useState("");
  const project = useProject(key);
  useNow();

  const acknowledge = async () => {
    setBusy(true);
    try {
      setData(await post<Detail>(`/api/projects/${key}/incidents/${id}/acknowledge`));
      toast.success("Acknowledged", { description: "The team can see you're on it." });
    } catch (e) {
      toast.error("Couldn't acknowledge", { description: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const resolve = async () => {
    setBusy(true);
    try {
      setData(await post<Detail>(`/api/projects/${key}/incidents/${id}/resolve`, { note: note.trim() || null }));
      setResolving(false);
      setNote("");
      toast.success("Resolved", { description: "Kept in the register with your name. If its checks still fail, the bot opens a new incident." });
    } catch (e) {
      toast.error("Couldn't resolve", { description: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  const addComment = async () => {
    if (!comment.trim()) return;
    setBusy(true);
    try {
      setData(await post<Detail>(`/api/projects/${key}/incidents/${id}/comments`, { text: comment.trim() }));
      setComment("");
    } catch (e) {
      toast.error("Couldn't add the comment", { description: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  if (error && !data) return <Notice tone="warn">Couldn't load this incident: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-36 w-full rounded-[1.5rem]" /><Skeleton className="h-64 w-full" /></div>;

  const resolved = data.status === "resolved";
  const aiModel = data.evidence.find((e) => e.tool === "ai")?.data?.model as string | undefined;
  const tri = data.evidence.find((e) => e.tool === "triage");
  const triage = tri?.data ? { urgency: String(tri.data.urgency), escalate: Number(tri.data.escalate ?? 0), detail: tri.detail } : null;

  return (
    <div>
      <Link to={`/p/${key}/incidents`} className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-ink-3 hover:text-ink"><ChevronLeft className="size-3.5" /> All incidents</Link>
      <PageIntro
        eyebrow={<><Siren className="size-3.5" /> Incident #{data.id}</>}
        title={data.title}
        actions={resolved ? null : <div className="flex flex-wrap gap-2">
          {data.status === "open" && <Button variant="primary" onClick={acknowledge} disabled={busy}><Check /> Acknowledge</Button>}
          <Button variant={data.status === "open" ? "secondary" : "primary"} onClick={() => setResolving(true)} disabled={busy}><CircleCheck /> Resolve…</Button>
        </div>}>
        <span className="mr-3 inline-flex gap-1.5"><SeverityPill severity={data.severity} /><RegisterStatus i={data} /></span>
        <Facts items={[`opened ${ago(data.opened_at)}`, dateTime(data.opened_at), data.acknowledged_by ? `acknowledged by ${data.acknowledged_by}` : null,
          data.resolved_at ? `resolved ${ago(data.resolved_at)} by ${data.resolved_by ?? "Alert Bot"}` : null]} />
      </PageIntro>

      <div className="grid grid-cols-12 gap-4">
        {data.kind === "adf" && data.run?.run_id && <div className="col-span-12"><RunTree project={key!} runId={data.run.run_id} /></div>}
        <div className="col-span-12 space-y-4 xl:col-span-8">
          <ChartCard variant="hero" icon={data.analysis_source === "ai" ? Sparkles : Wrench} title="Analysis"
            subtitle={aiModel ? `Investigated by rules from read-only tool calls; the opening briefing by AI (${shortModel(aiModel)}), checked against the evidence`
              : "Investigated by rules from read-only tool calls (the AI briefing is added when a model is available)"}
            actions={<>
              {triage && <span title={triage.detail} className={cn("rounded-full px-2.5 py-1 text-[0.6875rem] font-semibold shadow-[inset_0_0_0_1px_var(--line)]",
                triage.escalate >= 0.5 ? "bg-[var(--critical-soft)] text-[var(--critical-ink)]" : "bg-surface-2 text-ink-2")}>
                <Sparkles className="mr-1 inline size-3" aria-hidden />AI triage: {triage.urgency}</span>}
              <span className="rounded-full bg-surface-2 px-2.5 py-1 text-[0.6875rem] text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">Confidence <Em>{data.confidence || "–"}</Em></span>
            </>}
            bodyClassName="px-0 pb-0">
            {(() => {
              const parts: [string, string][] = [["What happened", data.summary], ["Why it happened", data.likely_cause], ["Impact", data.impact], ["What to do", data.suggested_action]];
              const long = parts.some(([, t]) => (t ?? "").includes("\n"));
              return (
                <div className={cn("grid grid-cols-1 border-t border-line", !long && "md:grid-cols-2")}>
                  {parts.map(([h, t], i) => (
                    <div key={h} className={cn("px-6 py-4", long ? i > 0 && "border-t border-line" : cn(i % 2 === 0 && "md:border-r md:border-line", i > 1 && "border-t border-line", i === 1 && "max-md:border-t max-md:border-line"))}>
                      <p className="eyebrow font-semibold text-ink-3">{h}</p>
                      <div className={cn("mt-1.5 whitespace-pre-line break-words text-[0.875rem] leading-6", h === "Why it happened" ? "text-ink" : "text-ink-2")}>
                        <Linkified text={t ?? ""} bold={h === "Why it happened"} />
                      </div>
                    </div>
                  ))}
                </div>
              );
            })()}
          </ChartCard>

          {data.kind === "adf" && <FixCard project={key} incidentId={data.id} onResolved={reload} />}

          {data.site && data.charts.length ? (
            <ChartCard icon={LineChart} title="Against the learned normal" subtitle={`${data.site} · last 40 runs · shaded band = what the bot has learned is normal (it keeps learning from every run)`}>
              <div className="space-y-7">{data.charts.map((c) => <MetricPanel key={c.metric + c.field} project={key} site={data.site!} {...c} />)}</div>
            </ChartCard>
          ) : null}

          <ChartCard icon={ListChecks} title="Failing checks" subtitle={`${data.failing_checks.length} grouped into this incident`}>
            <div className="divide-y divide-line">
              {data.failing_checks.map((c) => {
                const m = SEVERITY_META[c.severity] ?? SEVERITY_META.warning!;
                return (
                  <div key={c.check} className="flex gap-3 py-3.5 first:pt-0 last:pb-0">
                    <m.icon className="mt-0.5 size-4 shrink-0" style={{ color: m.color }} aria-hidden />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2"><span className="text-sm font-semibold text-ink">{checkLabel(c.check)}</span><SeverityPill severity={c.severity} /></div>
                      <p className="mt-1 text-[0.8125rem] leading-5 text-ink-2">{c.message}</p>
                      <RangeBar c={c} />
                    </div>
                  </div>
                );
              })}
            </div>
          </ChartCard>

          <ChartCard icon={Search} title="Investigation log" subtitle="Every read-only tool call, as audited" flush empty={!data.tool_calls.length} emptyText="No tool calls recorded.">
            <ul className="pb-1">
              {data.tool_calls.map((t, i) => (
                <li key={i} className="border-t border-line first:border-t-0">
                  <button onClick={() => setOpenTool(openTool === i ? null : i)} className="flex w-full items-center gap-3 px-6 py-2.5 text-left transition-colors hover:bg-row-hover" aria-expanded={openTool === i}>
                    <ChevronRight className={cn("size-3.5 shrink-0 text-ink-3 transition-transform", openTool === i && "rotate-90")} aria-hidden />
                    <code className="font-mono text-[0.78125rem] text-ink">{t.tool}</code>
                    <span className="num min-w-0 flex-1 truncate text-[0.6875rem] text-ink-3">{Object.entries(t.args ?? {}).map(([k, v]) => `${k}=${String(v).slice(0, 22)}`).join(", ")}</span>
                    <span className="num shrink-0 text-[0.6875rem] text-ink-3">{t.agent} · {t.duration_ms} ms</span>
                  </button>
                  {openTool === i ? <pre className="scrollbar-thin mx-6 mb-3 max-h-80 overflow-auto rounded-xl bg-surface-2 p-3 font-mono text-[0.71875rem] leading-5 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">{t.error ?? JSON.stringify(t.result, null, 2)}</pre> : null}
                </li>
              ))}
            </ul>
          </ChartCard>
        </div>

        <div className="col-span-12 space-y-4 xl:col-span-4">
          <ChartCard icon={ClipboardList} title="Record" subtitle="Kept in the register for good">
            <dl className="rec-list">
              <div><dt>Status</dt><dd><RegisterStatus i={data} /></dd></div>
              <div><dt>Opened</dt><dd><b>{fullDate(data.opened_at)} · {time(data.opened_at)}</b><span>{ago(data.opened_at)} · by the bot</span></dd></div>
              <div><dt>Acknowledged</dt><dd>{data.acknowledged_at
                ? <><b>{data.acknowledged_by}</b><span>{fullDate(data.acknowledged_at)} · {time(data.acknowledged_at)} · {between(data.opened_at, data.acknowledged_at)} after it opened</span></>
                : <em>{resolved ? "Not acknowledged" : "Not yet: nobody is on it"}</em>}</dd></div>
              <div><dt>Resolved</dt><dd>{data.resolved_at
                ? <><b>{data.resolved_by ?? "Alert Bot"}</b><span>{fullDate(data.resolved_at)} · {time(data.resolved_at)} · open for {between(data.opened_at, data.resolved_at)}</span></>
                : <em>Not yet · open for {between(data.opened_at, new Date().toISOString())}</em>}</dd></div>
              {data.resolution && <div><dt>How</dt><dd>{data.resolution}</dd></div>}
              {data.last_seen_at && !resolved && <div><dt>Last seen</dt><dd><em>still failing at the bot's look {ago(data.last_seen_at)}</em></dd></div>}
              <div><dt>Checks</dt><dd>{data.checks.map(checkLabel).join(", ")}</dd></div>
            </dl>
          </ChartCard>

          <ChartCard icon={MessageSquareText} title="Comments" subtitle={data.comments.length ? `${data.comments.length} · everyone on the project sees them` : "Notes for the team, kept with the incident"}>
            {data.comments.length > 0 && (
              <ol className="mb-3 space-y-3">
                {data.comments.map((c) => (
                  <li key={c.id} className="flex gap-2.5">
                    <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[0.625rem] font-bold text-accent" aria-hidden>{initials(c.by)}</span>
                    <div className="min-w-0 flex-1 rounded-xl rounded-tl-sm bg-surface-2 px-3 py-2 shadow-[inset_0_0_0_1px_var(--line)]">
                      <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                        <span className="text-[0.75rem] font-semibold text-ink">{c.by}</span>
                        <span className="num text-[0.65625rem] text-ink-3" title={new Date(c.at).toString()}>{fullDate(c.at)} · {time(c.at)}</span>
                      </div>
                      <p className="mt-0.5 whitespace-pre-line text-[0.8125rem] leading-5 text-ink-2 [overflow-wrap:anywhere]">{c.text}</p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
            <form onSubmit={(e) => { e.preventDefault(); void addComment(); }} className="flex items-end gap-2">
              <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} maxLength={2000} aria-label="Add a comment"
                onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void addComment(); } }}
                placeholder="What you found, what you did, who's on it…"
                className="min-h-[2.5rem] min-w-0 flex-1 resize-y rounded-lg border border-line-strong bg-surface-2 px-2.5 py-2 text-[0.8125rem] leading-5 text-ink placeholder:text-ink-3 focus-visible:border-accent/60 focus-visible:outline-2 focus-visible:outline-focus light:bg-surface" />
              <Button type="submit" variant="primary" size="icon" disabled={busy || !comment.trim()} aria-label="Post comment"><Send /></Button>
            </form>
          </ChartCard>

          {data.kind === "adf" && (
            <Link to={`/lineage?focus=${encodeURIComponent(data.scope.startsWith("site:") ? `pipeline:${key}:${data.scope.slice(5)}` : `table:${data.scope.split(":").slice(1).join(":")}`)}`}
              className="flex items-center gap-3 rounded-2xl border border-line bg-surface p-4 shadow-card transition-colors hover:border-line-strong">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><Network className="size-4" /></span>
              <span className="min-w-0 flex-1"><span className="block text-sm font-semibold text-ink">See what this affects</span>
                <span className="block text-xs text-ink-2">The tables and pipelines downstream, on every platform</span></span>
              <ChevronRight className="size-4 shrink-0 text-ink-3" />
            </Link>
          )}
          {data.remediation && (
            <Remediation plan={data.remediation} projectKey={key} incidentId={data.id} scope={data.scope} owner={project?.role === "owner"}
              acknowledged={!!data.acknowledged_by} onAcknowledge={acknowledge} onDone={reload} />
          )}
          <ChartCard icon={FileSearch} title="Evidence" subtitle="What the investigator found">
            <ul className="space-y-3">
              {data.evidence.map((e, i) => (
                <li key={i} className="flex gap-2.5">
                  <BadgeCheck className="mt-0.5 size-4 shrink-0 text-good" aria-hidden />
                  <div className="min-w-0">
                    <div className="text-[0.8125rem] font-semibold text-ink">{e.label}</div>
                    <div className="text-[0.78125rem] leading-5 text-ink-2">{e.detail}</div>
                    {e.tool ? <div className="mt-0.5 font-mono text-[0.65625rem] text-ink-3">{e.tool}</div> : null}
                  </div>
                </li>
              ))}
            </ul>
          </ChartCard>

          {data.run ? (
            <ChartCard icon={ServerCog} title="The run" subtitle={data.run.pipeline ?? undefined}>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-[0.8125rem]">
                <dt className="text-ink-3">Site</dt><dd className="text-ink">{data.run.site}</dd>
                <dt className="text-ink-3">Status</dt><dd><StatusChip status={data.run.run_status ?? data.run.trigger_status} /></dd>
                <dt className="text-ink-3">Requested</dt><dd className="num text-ink">{dateTime(data.run.requested_at)}</dd>
                <dt className="text-ink-3">Duration</dt><dd className="num text-ink">{duration(data.run.duration_s)}</dd>
                {data.run.vs_normal !== undefined && <><dt className="text-ink-3">vs normal</dt><dd className="min-w-0"><RuntimeBar r={data.run.vs_normal} /></dd></>}
                <dt className="text-ink-3">Rows</dt><dd className="num text-ink">{num(data.run.rows_loaded)}</dd>
                <dt className="text-ink-3">Run id</dt><dd className="truncate font-mono text-[0.6875rem] text-ink-2">{data.run.run_id}</dd>
              </dl>
            </ChartCard>
          ) : null}

          <ChartCard icon={History} title="Timeline">
            <ol className="timeline">
              {data.events.map((e, i) => {
                const m = SEVERITY_META[e.kind === "resolved" ? "ok" : e.kind === "opened" || e.kind === "worsened" ? data.severity : "info"] ?? SEVERITY_META.info!;
                return (
                  <li key={i}>
                    <span className="absolute left-0 top-0.5 flex size-[1.125rem] items-center justify-center rounded-full" style={{ background: m.soft }}><m.icon className="size-2.5" style={{ color: m.color }} aria-hidden /></span>
                    <div className="text-[0.8125rem] leading-5 text-ink">{e.message}</div>
                    <div className="num text-[0.6875rem] text-ink-3">{dateTime(e.at)}</div>
                  </li>
                );
              })}
            </ol>
          </ChartCard>

          <ChartCard icon={BellRing} title="Alerts sent" subtitle="What went to Teams (or would have)" empty={!data.notifications.length} emptyText="No alerts for this incident.">
            <div className="space-y-5">
              {data.notifications.map((n) => (
                <div key={n.id}>
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="text-xs text-ink-2"><Em>{n.event.replace("incident.", "")}</Em> · {dateTime(n.created_at)}</span>
                    <StatusChip status={n.status} />
                  </div>
                  {n.payload.card ? <AdaptiveCardPreview card={n.payload.card} /> : null}
                </div>
              ))}
            </div>
          </ChartCard>
        </div>
      </div>

      <Modal open={resolving} onClose={() => setResolving(false)} icon={CircleCheck} title={`Resolve incident #${data.id}`}
        subtitle="It stays in the register with your name, the time and your note."
        footer={<>
          <Button variant="ghost" onClick={() => setResolving(false)}>Cancel</Button>
          <Button variant="primary" onClick={() => void resolve()} disabled={busy}><CircleCheck /> Resolve</Button>
        </>}>
        <label className="block text-xs font-semibold text-ink-2" htmlFor="resolve-note">How was it resolved? <span className="font-normal text-ink-3">(optional)</span></label>
        <textarea id="resolve-note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={1000}
          placeholder="e.g. Restarted trigger4; the backfill was expected"
          className="mt-1.5 w-full resize-y rounded-lg border border-line-strong bg-surface-2 px-2.5 py-2 text-sm leading-5 text-ink placeholder:text-ink-3 focus-visible:border-accent/60 focus-visible:outline-2 focus-visible:outline-focus light:bg-surface" />
        <p className="mt-2 text-xs leading-5 text-ink-3">The bot keeps checking. If the same checks still fail on its next look, it opens a new incident rather than staying quiet.</p>
      </Modal>
    </div>
  );
}
