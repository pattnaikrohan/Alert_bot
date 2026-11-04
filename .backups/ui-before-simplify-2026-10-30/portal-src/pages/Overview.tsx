import { Activity, BellRing, CircleCheck, Gauge, LayoutGrid, Rows3, Siren, Sparkles, Table2, Timer } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { ActivityChart, Sparkline } from "@/components/Charts";
import { ChartCard, Em, Facts, Metric, MetricStrip, Notice, PageIntro } from "@/components/Cards";
import { SEVERITY_META, SeverityPill, StatusChip, StatusDot } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { ago, checkLabel, compactNum, duration, num, time } from "@/lib/format";
import { useApi, useNow } from "@/lib/hooks";
import type { IncidentBrief, Overview as OverviewData, SiteCard } from "@/lib/types";
import { cn } from "@/lib/utils";

export function IncidentRow({ i, project }: { i: IncidentBrief; project: string }) {
  const resolved = i.status === "resolved";
  const m = SEVERITY_META[resolved ? "ok" : i.severity]!;
  return (
    <Link to={`/p/${project}/i/${i.id}`} className="group flex items-start gap-3.5 border-t border-line px-6 py-3.5 transition-colors first:border-t-0 hover:bg-row-hover">
      <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-[0.625rem]" style={{ background: m.soft, boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${m.color} 35%, transparent)` }}>
        <m.icon className="size-4" style={{ color: m.color }} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn("truncate text-sm font-semibold", resolved ? "text-ink-2" : "text-ink")}>{i.title}</span>
          {i.status === "acknowledged" ? <StatusChip status="acknowledged" /> : null}
        </div>
        <div className="mt-0.5 text-[0.71875rem] text-ink-3">
          <Facts items={[<span className="num" key="id">#{i.id}</span>, i.checks.map(checkLabel).join(" · "), `opened ${ago(i.opened_at)}`, resolved && i.resolved_at ? `resolved ${ago(i.resolved_at)}` : null]} />
        </div>
        {i.likely_cause ? <p className="mt-1.5 line-clamp-2 text-[0.8125rem] leading-5 text-ink-2">{i.likely_cause}</p> : null}
      </div>
      <SeverityPill severity={resolved ? "ok" : i.severity} label={resolved ? "Resolved" : undefined} className="mt-0.5" />
    </Link>
  );
}

function SiteTile({ s, project }: { s: SiteCard; project: string }) {
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-ink"><StatusDot status={s.status} />{s.site}</span>
        <SeverityPill severity={s.status} />
      </div>
      <div className="mt-3 flex items-baseline gap-1.5">
        <span className="kpi text-[1.5rem] text-ink">{num(s.last_rows)}</span>
        <span className="text-[0.71875rem] text-ink-3">rows{s.expected ? ` · normal ${compactNum(s.expected.low)}–${compactNum(s.expected.high)}` : ""}</span>
      </div>
      <div className="mt-2"><Sparkline values={s.sparkline} band={s.expected} status={s.status} /></div>
      {s.failing.length ? <p className="mt-2 truncate text-[0.71875rem] font-medium text-critical">{s.failing.map((f) => checkLabel(f.check)).join(" · ")}</p> : null}
      <div className="mt-2 flex items-center justify-between text-[0.6875rem] text-ink-3">
        <span className="num">Last run {time(s.last_run.requested_at)} · {duration(s.last_run.duration_s)}</span>
        <StatusChip status={s.last_run.run_status ?? s.last_run.trigger_status} />
      </div>
    </>
  );
  const cls = cn("card-in relative block overflow-hidden rounded-2xl border border-line bg-surface p-4 shadow-card transition-[border-color,transform] duration-150",
    s.status === "critical" && "border-critical/40", s.status === "warning" && "border-warning/40");
  return s.incident_id ? (
    <Link to={`/p/${project}/i/${s.incident_id}`} className={cn(cls, "hover:-translate-y-0.5 hover:border-line-strong")}>
      <span className="absolute inset-y-0 left-0 w-[3px]" style={{ background: SEVERITY_META[s.status]?.color }} aria-hidden />
      {body}
    </Link>
  ) : <div className={cls}>{body}</div>;
}

export function Overview() {
  const { key = "" } = useParams();
  const { data, error } = useApi<OverviewData>(`/api/projects/${key}/overview`, { refreshMs: 30000 });
  useNow();

  if (error && !data) return <Notice tone="warn">Couldn't load this project: {error.message}</Notice>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-36 w-full rounded-[1.5rem]" /><Skeleton className="h-28 w-full" /><Skeleton className="h-80 w-full" /></div>;

  const k = data.kpis;
  // the same model as the dock and the register: "needs attention" = nobody has acknowledged it yet
  const fresh = data.active_incidents.filter((i) => i.status === "open");
  const acked = data.active_incidents.length - fresh.length;
  const critFresh = fresh.filter((i) => i.severity === "critical").length;
  const headline = critFresh ? `${critFresh} critical incident${critFresh > 1 ? "s" : ""} need${critFresh > 1 ? "" : "s"} attention`
    : fresh.length ? `${fresh.length} incident${fresh.length > 1 ? "s" : ""} need${fresh.length > 1 ? "" : "s"} attention`
    : acked ? `Nothing new: ${acked} incident${acked > 1 ? "s are" : " is"} being handled` : "Every check is passing";

  return (
    <div>
      <PageIntro eyebrow={<><Gauge className="size-3.5" /> {data.project.display_name}</>} title={headline}
        actions={
          <>
            <span className="inline-flex items-center gap-2 rounded-full bg-surface-2 px-3 py-1 text-xs text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">
              <Sparkles className="size-3.5 text-accent" aria-hidden />
              {data.ai.enabled ? <>AI analysis · <Em>{data.ai.model}</Em></> : <>Rule-based analysis · AI off</>}
            </span>
            <span className={cn("inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs shadow-[inset_0_0_0_1px_var(--line)]", data.notifier.mode === "live" ? "bg-good-soft text-good" : "bg-warning-soft text-warning")}>
              <BellRing className="size-3.5" aria-hidden /> {data.notifier.mode === "live" ? "Alerts live" : "Alerts in dry-run"}
            </span>
          </>
        }>
        {data.project.description}
      </PageIntro>

      <MetricStrip className="mb-4" cols="grid-cols-2 lg:grid-cols-5">
        <Metric size="lg" icon={Gauge} label="Health" value={<>{data.health == null ? "–" : Math.round(data.health)}<span className="text-[1.125rem] text-ink-3">%</span></>}
          footnote={`${num(k.checks - k.checks_failing)} of ${num(k.checks)} checks passing`} />
        <Metric size="lg" icon={Siren} iconColor={fresh.length ? "var(--critical)" : undefined} label="Need attention" value={num(fresh.length)}
          footnote={`${critFresh ? `${critFresh} critical · ` : ""}${acked} being handled`} />
        <Metric size="lg" icon={Activity} label="Runs today" value={num(k.runs_today)} footnote={k.failed_today ? `${k.failed_today} failed` : "none failed"} />
        <Metric size="lg" icon={Rows3} label="Rows loaded today" value={compactNum(k.rows_today)} footnote={`across ${data.sites.length} sites`} />
        <Metric size="lg" icon={Timer} label="Last check" value={<span className="text-[1.5rem]">{ago(data.bot.last_cycle_at)}</span>}
          footnote={data.bot.last_cycle_status === "error" ? <span className="text-critical">last cycle failed</span> : "every 10 minutes"} />
      </MetricStrip>

      {data.tables.some((t) => t.status !== "ok") ? (
        <Notice tone="warn" className="mb-4" action={<Button size="sm" asChild><Link to={`/p/${key}/tables`}><Table2 /> Review schema</Link></Button>}>
          {data.tables.filter((t) => t.status !== "ok").map((t) => <div key={t.output}><Em>{t.output}</Em>: {t.message}</div>)}
        </Notice>
      ) : null}

      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 space-y-4 xl:col-span-8">
          <ChartCard index={1} icon={Siren} title="Active incidents" subtitle={data.active_incidents.length ? `${fresh.length} need attention · ${acked} being handled · newest first` : "Nothing needs attention"} flush
            actions={<Button size="sm" variant="ghost" asChild><Link to={`/p/${key}/incidents`}>All incidents</Link></Button>}
            empty={!data.active_incidents.length} emptyIcon={CircleCheck} emptyTone="good" emptyText="No open incidents. Every check passed on the latest round.">
            <div className="pb-1">{data.active_incidents.map((i) => <IncidentRow key={i.id} i={i} project={key} />)}</div>
          </ChartCard>

          <ChartCard index={2} icon={LayoutGrid} title="Sites" subtitle="Rows from each site's latest run against its learned normal range" bodyClassName="pt-1">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 2xl:grid-cols-3">
              {data.sites.map((s) => <SiteTile key={s.site} s={s} project={key} />)}
            </div>
          </ChartCard>

          <ChartCard index={3} icon={Activity} title="Runs, last 14 days" subtitle={`${num(data.activity.reduce((a, d) => a + d.runs, 0))} runs · hover a day for details`}>
            <ActivityChart days={data.activity} />
          </ChartCard>
        </div>

        <div className="col-span-12 space-y-4 xl:col-span-4">
          <ChartCard index={2} icon={BellRing} title="Alert delivery" subtitle="Where the bot sends incident alerts"
            actions={<Button size="sm" variant="ghost" asChild><Link to={`/p/${key}/alerts`}>Alerts</Link></Button>}>
            <dl className="space-y-2.5 text-[0.8125rem]">
              {[["Channel", data.notifier.channel === "power_automate" ? "Power Automate" : data.notifier.channel === "teams_workflow" ? "Teams workflow" : "Off"],
                ["Teams channel", data.notifier.teams_channel || "–"], ["Email owners on", data.notifier.email_owners_on.join(", ") || "never"]].map(([a, b]) => (
                <div key={a} className="flex items-center justify-between gap-3"><dt className="text-ink-3">{a}</dt><dd className="font-medium text-ink">{b}</dd></div>
              ))}
            </dl>
            {data.notifier.mode !== "live" ? (
              <Notice tone="warn" className="mt-4">
                <Em>Dry-run.</Em> No flow URL yet: alerts are built and stored here but not sent. Set <code className="num rounded bg-surface-3 px-1 text-[0.75rem]">{data.notifier.flow_url_ref?.replace("env://", "") ?? "flow_url"}</code> to go live.
              </Notice>
            ) : null}
          </ChartCard>

          <ChartCard index={3} icon={Activity} title="Recent activity" subtitle="Incidents opening, worsening and resolving" empty={!data.recent_events.length} emptyText="Nothing has happened yet.">
            <ol className="timeline">
              {data.recent_events.map((e, i) => {
                const m = SEVERITY_META[e.kind === "resolved" ? "ok" : e.kind === "acknowledged" ? "info" : e.severity] ?? SEVERITY_META.info!;
                return (
                  <li key={i}>
                    <span className="absolute left-0 top-0.5 flex size-[1.125rem] items-center justify-center rounded-full" style={{ background: m.soft }}>
                      <m.icon className="size-2.5" style={{ color: m.color }} aria-hidden />
                    </span>
                    <Link to={`/p/${key}/i/${e.incident_id}`} className="text-[0.8125rem] font-medium text-ink hover:text-accent">{e.title}</Link>
                    <div className="text-[0.6875rem] text-ink-3">{e.kind} · {ago(e.at)}</div>
                  </li>
                );
              })}
            </ol>
          </ChartCard>

          {data.bot.last_error ? (
            <Notice tone="warn">The last cycle failed: {data.bot.last_error}</Notice>
          ) : null}
        </div>
      </div>
    </div>
  );
}
