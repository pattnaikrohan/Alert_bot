// Home: where everyone lands. It answers "is everything OK?" in a sentence and four numbers, then shows how to use the
// app through the bot: three steps and examples. The bot itself opens over the page only when asked for ("Ask the bot",
// or an example). Below: what needs attention, your projects, and what happened lately.
import {
  ArrowRight, CheckCircle2, CircleCheck, Hand, Loader2, MessageSquareText, OctagonAlert, RefreshCw, Sparkles, TriangleAlert, type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";
import { lookAgain, useShell } from "@/app/Layout";
import { shortName } from "@/app/projects";
import { openBot } from "@/components/assistant/BotPanel";
import { ExampleCards, HowSteps, useExampleGroups } from "@/components/assistant/examples";
import { BotAvatar } from "@/components/BotAvatar";
import { SeverityPill } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { ago, time } from "@/lib/format";
import type { ProjectCard } from "@/lib/types";
import { cn } from "@/lib/utils";

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

/** A number to take in at a glance; links where the number leads. */
function Stat({ label, value, sub, tone, to }: { label: string; value: number | string; sub?: string; tone?: "critical" | "warning" | "good" | "accent"; to?: string }) {
  const body = (
    <>
      <span className={cn("home-stat-value", value === 0 ? "is-zero" : tone && `is-${tone}`)}>{value}</span>
      <span className="home-stat-label">{label}</span>
      {sub && <span className="home-stat-sub">{sub}</span>}
    </>
  );
  return to ? <Link to={to} className="home-stat is-link">{body}</Link> : <div className="home-stat">{body}</div>;
}

const STATUS: Record<string, { label: string; color: string; icon: LucideIcon }> = {
  critical: { label: "Needs action", color: "var(--critical)", icon: OctagonAlert },
  warning: { label: "Needs a look", color: "var(--warning)", icon: TriangleAlert },
  ok: { label: "Healthy", color: "var(--good)", icon: CheckCircle2 },
  unknown: { label: "Waiting for first check", color: "var(--ink-3)", icon: Loader2 },
};

const HANDLED = { label: "Being handled", color: "var(--accent)", icon: Hand };

/** A project in one line: status, name, what needs attention, when it was last checked. */
function ProjectRow({ p }: { p: ProjectCard }) {
  const needs = p.needs_attention ?? p.open_incidents;
  const s = p.open_incidents && !needs ? HANDLED : STATUS[p.status] ?? STATUS.unknown!;
  return (
    <li>
      <Link to={`/p/${p.key}`} className="group flex items-center gap-3 px-4 py-3 transition-colors hover:bg-surface-2">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg" style={{ background: `color-mix(in srgb, ${s.color} 14%, transparent)` }}>
          <s.icon className="size-4" style={{ color: s.color }} aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[0.875rem] font-semibold text-ink group-hover:underline" title={p.display_name}>{shortName(p)}</span>
          <span className="block truncate text-[0.6875rem] text-ink-3">
            {needs ? `${needs} need${needs === 1 ? "s" : ""} attention` : s.label}
            {p.health != null ? ` · ${Math.round(p.health)}% checks passing` : ""} · {p.last_cycle_at ? `checked ${ago(p.last_cycle_at)}` : "not checked yet"}
          </span>
        </span>
        <ArrowRight className="size-4 shrink-0 text-ink-3 transition-transform group-hover:translate-x-0.5" aria-hidden />
      </Link>
    </li>
  );
}

export function Home() {
  const { me, feed, reload, project, pipelines, pipelineItems } = useShell();
  const groups = useExampleGroups();
  const say = (text: string) => openBot(text);  // the bot opens over the page and is asked straight away
  const first = me.user.name.split(" ")[0];
  const active = feed?.active ?? [];
  const fresh = active.filter((a) => a.status === "open");  // nobody on it yet
  const acked = active.filter((a) => a.status === "acknowledged");
  const crit = fresh.filter((a) => a.severity === "critical").length;
  const doneToday = feed?.resolved_today ?? [];
  // pipelines whose problem someone is on without an open incident (resolved by a person; the next run will tell)
  const awaiting = pipelineItems.filter((x) => x.state === "handled" && x.handled?.kind === "resolved");
  const looking = me.discovery?.status === "running";

  if (!me.projects.length) {
    return (
      <div className="mx-auto max-w-2xl pt-10 text-center">
        <BotAvatar mood={looking ? "ok" : "idle"} className="mx-auto size-20" />
        <h1 className="display mt-5 text-[1.75rem] font-bold tracking-[-0.02em] text-ink">{greeting()}, {first}</h1>
        {looking ? (
          <p className="mt-3 flex items-center justify-center gap-2 text-ink-2"><Loader2 className="size-4 animate-spin text-accent" /> Finding the Data Factories, Synapse workspaces and Logic Apps your Microsoft account can see…</p>
        ) : (
          <>
            <p className="mx-auto mt-3 max-w-lg text-ink-2">
              {me.discovery?.status === "error" ? <>The bot couldn't look in Azure: {me.discovery.message}</>
                : me.discovery ? <>Azure shows <b>{me.user.email}</b> no Data Factory, Synapse workspace or Logic App yet. If you should have access, ask for the Reader role on them in Azure, then look again.</>
                : <>The bot looks in Azure with your own sign-in for every Data Factory, Synapse workspace and Logic App you can see.</>}
            </p>
            <Button className="mt-6" variant="primary" onClick={() => void lookAgain(reload)}><RefreshCw />Look in Azure{me.discovery ? " again" : ""}</Button>
          </>
        )}
      </div>
    );
  }

  // the worst thing first: what nobody is on yet, then what's being handled, then all clear (with what today held)
  const failing = pipelines?.failing ?? 0, unstable = pipelines?.unstable ?? 0;
  const handling = acked.length + awaiting.length;
  const s = (n: number) => (n === 1 ? "" : "s");
  const [headline, tone] = !feed ? [null, "ok"] as const
    : crit ? [`${crit} critical problem${s(crit)} need${crit === 1 ? "s" : ""} attention`, "critical"] as const
    : fresh.length ? [`${fresh.length} thing${s(fresh.length)} need${fresh.length === 1 ? "s" : ""} attention`, "warning"] as const
    : failing ? [`${failing} pipeline${s(failing)} ${failing === 1 ? "is" : "are"} failing`, "critical"] as const
    : handling ? [`Nothing new: ${handling} ${handling === 1 ? "problem is" : "problems are"} being handled`, "handled"] as const
    : doneToday.length ? [`All clear: today's ${doneToday.length} incident${s(doneToday.length)} ${doneToday.length === 1 ? "is" : "are"} resolved`, "ok"] as const
    : ["Everything is running as expected", "ok"] as const;


  const needTo = fresh[0] ? `/p/${fresh[0].project}/i/${fresh[0].id}` : project ? `/p/${project.key}/incidents` : undefined;
  const handledTo = acked[0] ? `/p/${acked[0].project}/incidents` : awaiting[0] ? `/p/${awaiting[0].project}/incidents` : undefined;

  return (
    <div className="mx-auto max-w-[80rem]">
      {/* hero: how things stand, in a sentence and four numbers */}
      <section className={cn("home-hero", `is-${tone}`)} aria-labelledby="h-status">
        <div className="flex min-w-0 flex-1 items-start gap-4">
          <BotAvatar mood={tone === "handled" ? "ok" : tone} className="home-hero-bot hidden size-12 shrink-0 sm:block" />
          <div className="min-w-0">
            <p className="flex flex-wrap items-center gap-x-2 text-[0.8125rem] text-ink-2">
              <span>{greeting()}, {first}</span>
              {feed?.bot.last_cycle_at && <span className="inline-flex items-center gap-1.5 text-ink-3"><span className="home-live" aria-hidden />checked {ago(feed.bot.last_cycle_at)}</span>}
            </p>
            {headline ? <h1 id="h-status" className="display mt-0.5 text-[1.5rem] font-bold leading-[1.15] tracking-[-0.02em] text-ink">{headline}</h1>
              : <Skeleton className="mt-1.5 h-8 w-80" />}
            <p className="mt-1.5 text-[0.8125rem] text-ink-2">
              Watching {pipelines?.total ? <><b className="font-semibold text-ink">{pipelines.total} pipeline{s(pipelines.total)}</b> across </> : null}
              <b className="font-semibold text-ink">{me.projects.length} project{s(me.projects.length)}</b>
              {unstable ? <> · <Link to="/pipelines" className="hover:underline">{unstable} had a failed run this week, fine since</Link></> : null}
              {feed?.bot.stale ? <span className="text-[var(--warning-ink)]"> · the bot has been quiet for a while</span> : null}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              {fresh[0] ? <Button variant="primary" asChild><Link to={`/p/${fresh[0].project}/i/${fresh[0].id}`}>Show me the {crit ? "worst" : "first"} one <ArrowRight /></Link></Button>
                : failing ? <Button variant="primary" asChild><Link to="/pipelines">See which <ArrowRight /></Link></Button>
                : handling || doneToday.length ? <Button variant="primary" asChild><Link to={`/p/${(acked[0] ?? doneToday[0] ?? awaiting[0])!.project}/incidents`}>Open the register <ArrowRight /></Link></Button> : null}
              <Button variant="secondary" onClick={() => say(fresh.length || failing ? "What's wrong right now?" : "Is today's data ready?")}>
                <MessageSquareText />{fresh.length || failing ? "Ask the bot what's wrong" : "Ask the bot"}
              </Button>
            </div>
          </div>
        </div>
        <div className="home-stats">
          <Stat label="Need you" value={feed ? fresh.length : "–"} sub={crit ? `${crit} critical` : fresh.length ? "nobody on them yet" : "all clear"}
            tone={crit ? "critical" : fresh.length ? "warning" : "good"} to={fresh.length ? needTo : undefined} />
          <Stat label="Being handled" value={feed ? handling : "–"} sub={handling ? "someone is on it" : "nothing open"} tone={handling ? "accent" : undefined} to={handledTo} />
          <Stat label="Resolved today" value={feed ? doneToday.length : "–"} sub={doneToday.length ? `last by ${doneToday[0]!.resolved_by ?? "Alert Bot"}` : "none yet"}
            tone={doneToday.length ? "good" : undefined} to={doneToday[0] ? `/p/${doneToday[0].project}/incidents` : undefined} />
          <Stat label="Pipelines" value={pipelines?.total ?? "–"} sub={failing ? `${failing} failing` : pipelines ? "none failing" : undefined}
            tone={failing ? "critical" : undefined} to="/pipelines" />
        </div>
      </section>
      {feed?.bot.error && (
        <p className="-mt-2 mb-5 flex items-start gap-2 rounded-xl bg-[var(--warning-soft)] px-4 py-3 text-sm text-ink"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />The last check couldn't finish: {feed.bot.error}</p>
      )}

      {/* the bot: how to use the app through it. The chat itself opens over the page when asked for. */}
      <section aria-labelledby="h-bot" className="home-bot mb-8">
        <header className="home-bot-head">
          <div className="flex flex-wrap items-center gap-3">
            <span className="home-bot-badge"><Sparkles className="size-[1.125rem]" aria-hidden /></span>
            <div className="min-w-0 flex-1">
              <h2 id="h-bot" className="display text-[1.0625rem] font-semibold leading-tight text-ink">Ask the bot. It runs the whole app.</h2>
              <p className="mt-0.5 text-xs text-ink-2">Questions, fixes, Teams alerts, pages and settings, all in plain words. It opens over this page.</p>
            </div>
            <Button variant="primary" onClick={() => openBot()} className="home-ask"><MessageSquareText />Ask the bot</Button>
          </div>
          <HowSteps />
        </header>
        <div className="px-4 pb-4 pt-3">
          <p className="text-xs font-semibold text-ink-2">Or start with one of these: the bot opens and does it.</p>
          <ExampleCards groups={groups} onSay={say} />
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="min-w-0 space-y-6">
          {/* attention */}
          <section aria-labelledby="h-attention">
            <div className="mb-3 flex items-baseline justify-between">
              <h2 id="h-attention" className="display text-base font-semibold text-ink">Needs your attention</h2>
              {project && <Link to={`/p/${project.key}/incidents`} className="text-xs font-semibold text-accent hover:underline">Incident register</Link>}
            </div>
            {!feed ? <Skeleton className="h-28 w-full" /> : (
              <div className="space-y-3">
                {fresh.length > 0 ? <IncidentList items={fresh} /> : (
                  <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface p-5 shadow-card">
                    <CheckCircle2 className="size-6 shrink-0 text-[var(--good)]" />
                    <div>
                      <p className="text-sm font-semibold text-ink">Nothing needs you right now</p>
                      <p className="text-xs text-ink-2">{handling ? `Every open problem has someone on it (below). ` : ""}The bot will tell you here and in Teams the moment something new breaks.</p>
                    </div>
                  </div>
                )}
                {(acked.length > 0 || awaiting.length > 0) && (
                  <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
                    <p className="flex items-center gap-1.5 border-b border-line px-4 py-2.5 text-xs font-semibold text-ink-2"><Hand className="size-3.5 text-accent" />Being handled</p>
                    <ul className="divide-y divide-line">
                      {acked.map((i) => (
                        <HandledRow key={`a-${i.project}-${i.id}`} to={`/p/${i.project}/i/${i.id}`} title={i.title}
                          line={`Acknowledged by ${i.acknowledged_by ?? "someone"} · still open until it's resolved · ${i.project_name}`} />
                      ))}
                      {awaiting.map((x) => (
                        <HandledRow key={`r-${x.project}-${x.name}`} to={`/p/${x.project}/i/${x.handled!.incident_id}`} title={`${x.name}: ${x.handled!.title}`}
                          line={`Resolved by ${x.handled!.by}${x.handled!.note ? ` (“${x.handled!.note}”)` : ""} · its last run failed, so the next run will confirm`} />
                      ))}
                    </ul>
                  </div>
                )}
                {doneToday.length > 0 && (
                  <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
                    <p className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5 text-xs font-semibold text-ink-2">
                      <span className="flex items-center gap-1.5"><CircleCheck className="size-3.5 text-[var(--good)]" />Resolved today · {doneToday.length}</span>
                      <Link to={`/p/${doneToday[0]!.project}/incidents`} className="font-semibold text-accent hover:underline">Full register</Link>
                    </p>
                    <ul className="divide-y divide-line">
                      {doneToday.slice(0, 5).map((i) => (
                        <li key={`d-${i.project}-${i.id}`}>
                          <Link to={`/p/${i.project}/i/${i.id}`} className="group flex items-start gap-3 px-4 py-2.5 transition-colors hover:bg-surface-2">
                            <CircleCheck className="mt-0.5 size-4 shrink-0 text-[var(--good)]" aria-hidden />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-[0.8125rem] font-medium text-ink group-hover:underline">{i.title}</span>
                              <span className="mt-0.5 block truncate text-[0.6875rem] text-ink-3">
                                Resolved by <b className="font-semibold text-ink-2">{i.resolved_by ?? "Alert Bot"}</b> at {time(i.resolved_at)}{i.resolution ? ` · ${i.resolution}` : ""}
                              </span>
                            </span>
                          </Link>
                        </li>
                      ))}
                    </ul>
                    {doneToday.length > 5 && <p className="border-t border-line px-4 py-2 text-[0.6875rem] text-ink-3">+{doneToday.length - 5} more in the register</p>}
                  </div>
                )}
              </div>
            )}
          </section>

        </div>

        {/* your projects: one compact row each */}
        <aside aria-labelledby="h-projects" className="space-y-3">
          <div className="flex items-baseline justify-between">
            <h2 id="h-projects" className="display text-base font-semibold text-ink">Your projects</h2>
            <button onClick={() => void lookAgain(reload)} className="inline-flex items-center gap-1 text-xs font-semibold text-accent hover:underline"><RefreshCw className="size-3" />Find new ones</button>
          </div>
          <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
            {me.projects.map((p) => <ProjectRow key={p.key} p={p} />)}
          </ul>
        </aside>
      </div>
    </div>
  );
}

function IncidentList({ items }: { items: NonNullable<ReturnType<typeof useShell>["feed"]>["active"] }) {
  return (
    <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-surface shadow-card">
      {items.slice(0, 6).map((i) => (
        <li key={`${i.project}-${i.id}`}>
          <Link to={`/p/${i.project}/i/${i.id}`} className="group flex items-start gap-3 px-4 py-3.5 transition-colors hover:bg-surface-2">
            <SeverityPill severity={i.severity} className="mt-0.5" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-semibold text-ink group-hover:underline">{i.title}</span>
              <span className="mt-0.5 block line-clamp-1 text-xs text-ink-2">{(i.likely_cause || "").split("\n")[0]}</span>
              <span className="mt-0.5 block text-[0.6875rem] text-ink-3">{i.project_name} · opened {ago(i.opened_at)} · nobody on it yet</span>
            </span>
            <ArrowRight className="mt-1 size-4 shrink-0 text-ink-3" aria-hidden />
          </Link>
        </li>
      ))}
    </ul>
  );
}

function HandledRow({ to, title, line }: { to: string; title: string; line: string }) {
  return (
    <li>
      <Link to={to} className="group flex items-start gap-3 px-4 py-2.5 transition-colors hover:bg-surface-2">
        <Hand className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[0.8125rem] font-medium text-ink group-hover:underline">{title}</span>
          <span className="mt-0.5 block line-clamp-2 text-[0.6875rem] text-ink-3">{line}</span>
        </span>
        <ArrowRight className="mt-0.5 size-4 shrink-0 text-ink-3" aria-hidden />
      </Link>
    </li>
  );
}
