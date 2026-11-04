// The Daily Morning Report as the team receives it in Teams each morning, live: health score, pipelines in the last
// 24 h, tables and anomalies, what needs attention, trends vs the week before, and a short AI summary.
import { ArrowDownRight, ArrowRight, ArrowUpRight, FileText, Send, Sparkles, Sunrise } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { ChartCard, Notice, PageIntro } from "@/components/Cards";
import { SeverityPill } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { post } from "@/lib/api";
import { ago, dateTime, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { useProject } from "@/app/Layout";

interface Report {
  project: { key: string; name: string; kind: string }; date: string; generated_at: string; schedule: string | null;
  health: { score: number | null; parts: { pipelines: number | null; delivery: number | null; quality: number | null } };
  status: { level: "ok" | "warning" | "critical"; headline: string; reasons: string[] };
  pipelines: { completed: number; failed: number; running: number; delayed: number; delayed_names: string[] };
  tables: { loaded: number; on_time: number; checked: number; anomalies: number; failing_quality: number };
  issues: { critical: number; warning: number; list: { id: number; title: string; severity: string; opened_at: string }[] };
  trends: { label: string; now: number; before: number; unit: string; direction: "up" | "down" | "same"; good: boolean }[];
  ai_summary: string | null; ai_model?: string; text: string;
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "good" | "warning" | "critical" }) {
  return (
    <div className="rounded-xl bg-surface-2 px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--line)]">
      <p className={cn("display text-2xl font-bold tabular-nums", tone === "critical" && value ? "text-[var(--critical-ink)]" : tone === "warning" && value ? "text-[var(--warning-ink)]" : "text-ink")}>{num(value)}</p>
      <p className="text-xs text-ink-3">{label}</p>
    </div>
  );
}

export function MorningReport() {
  const { key = "" } = useParams();
  const project = useProject(key);
  const base = useApi<Report>(`/api/projects/${key}/report`);
  const withAi = useApi<Report>(`/api/projects/${key}/report?ai=1`);
  const r = withAi.data ?? base.data;
  const [sending, setSending] = useState(false);

  const send = async () => {
    setSending(true);
    try {
      const r = await post<{ delivery?: { sent?: number; dry_run?: number; retrying?: number; failed?: number } | null }>(`/api/projects/${key}/report/send`);
      const d = r.delivery ?? {};
      if (d.sent) toast.success("Report sent", { description: "Posted to the project's Teams channel." });
      else if (d.dry_run) toast.warning("Report not delivered", { description: "Teams isn't set up for this project yet, so the report was saved here but not sent." });
      else toast.warning("Teams didn't accept it yet", { description: "The bot keeps retrying." });
    } catch (e) {
      toast.error("Couldn't send the report", { description: (e as Error).message });
    } finally {
      setSending(false);
    }
  };

  if (base.error && !r) return <Notice tone="warn">Couldn't build the report: {base.error.message}</Notice>;
  if (!r) return <div className="space-y-4"><Skeleton className="h-28 w-full" /><Skeleton className="h-64 w-full" /></div>;
  const score = r.health.score;
  const scoreColor = score == null ? "var(--ink-3)" : score >= 90 ? "var(--good)" : score >= 70 ? "var(--warning)" : "var(--critical)";

  return (
    <div className="mx-auto max-w-4xl">
      <PageIntro eyebrow={<><Sunrise className="size-3.5" /> Daily morning report</>} title={`${r.project.name} · ${new Date(`${r.date}T12:00:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}`}>
        {r.schedule ? <>Sent to Teams every morning at {r.schedule} (the bot's time zone). This is the live version.</> : <>The morning report is switched off for this project; this is the live version.</>}
      </PageIntro>

      <ChartCard icon={FileText} title="Is today's data ready?" subtitle={`Built ${ago(r.generated_at)} · ${dateTime(r.generated_at)}`}
        actions={project?.role === "owner" ? <Button size="sm" variant="secondary" disabled={sending} onClick={send}><Send />{sending ? "Sending…" : "Send to Teams now"}</Button> : null}>
        <div className="flex flex-wrap items-center gap-5">
          <div className="flex items-baseline gap-1">
            <span className="display text-5xl font-bold tabular-nums" style={{ color: scoreColor }}>{score ?? "–"}</span>
            <span className="text-lg text-ink-3">/100</span>
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold uppercase tracking-wide text-ink-3">Data health score</p>
            <p className="mt-0.5 text-[0.9375rem] font-semibold leading-snug text-ink">{r.status.headline}</p>
          </div>
        </div>

        {r.ai_summary ? (
          <div className="mt-4 rounded-xl bg-accent-soft/50 px-4 py-3 shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--accent)_22%,transparent)]">
            <p className="flex items-center gap-1.5 text-xs font-semibold text-accent"><Sparkles className="size-3.5" /> Summary{r.ai_model ? <span className="font-normal text-ink-3">· AI ({r.ai_model.split("/").pop()?.replace(":free", "")}), checked against the numbers below</span> : null}</p>
            <p className="mt-1 text-sm leading-6 text-ink">{r.ai_summary}</p>
          </div>
        ) : withAi.data === undefined && !withAi.error ? (
          <p className="mt-4 flex items-center gap-1.5 text-xs text-ink-3"><Sparkles className="size-3.5 animate-pulse" /> Writing the summary…</p>
        ) : null}

        <h3 className="mt-6 text-sm font-semibold text-ink">Pipelines · last 24 hours</h3>
        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="completed" value={r.pipelines.completed} />
          <Stat label="delayed" value={r.pipelines.delayed} tone="warning" />
          <Stat label="failed" value={r.pipelines.failed} tone="critical" />
          <Stat label="running now" value={r.pipelines.running} />
        </div>
        {r.pipelines.delayed_names.length > 0 && <p className="mt-1.5 text-xs text-ink-3">Delayed: {r.pipelines.delayed_names.join(", ")}</p>}

        <h3 className="mt-5 text-sm font-semibold text-ink">Tables</h3>
        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="loaded" value={r.tables.loaded} />
          <Stat label="on time" value={r.tables.on_time} />
          <Stat label="quality-checked" value={r.tables.checked} />
          <Stat label="anomalies found" value={r.tables.anomalies} tone="warning" />
        </div>

        <h3 className="mt-5 text-sm font-semibold text-ink">Needs attention · {r.issues.critical} critical · {r.issues.warning} warnings</h3>
        {r.issues.list.length ? (
          <ul className="mt-2 divide-y divide-line rounded-xl shadow-[inset_0_0_0_1px_var(--line)]">
            {r.issues.list.map((i) => (
              <li key={i.id} className="flex items-center gap-2 px-3 py-2">
                <SeverityPill severity={i.severity} />
                <Link to={`/p/${key}/i/${i.id}`} className="min-w-0 flex-1 truncate text-sm font-medium text-ink hover:underline">{i.title}</Link>
                <span className="shrink-0 text-xs text-ink-3">since {ago(i.opened_at)}</span>
              </li>
            ))}
          </ul>
        ) : <p className="mt-1.5 text-sm text-ink-2">Nothing open. Every check is passing.</p>}

        {r.trends.length > 0 && (
          <>
            <h3 className="mt-5 text-sm font-semibold text-ink">Trends vs the previous week</h3>
            <table className="data-table mt-2">
              <tbody>
                {r.trends.map((t) => {
                  const Icon = t.direction === "up" ? ArrowUpRight : t.direction === "down" ? ArrowDownRight : ArrowRight;
                  const fmt = (v: number) => (t.unit === "%" ? `${Math.round(v)}%` : num(v));
                  return (
                    <tr key={t.label}>
                      <td className="text-ink">{t.label}</td>
                      <td className="num-cell font-semibold">{fmt(t.now)}</td>
                      <td className="w-40">
                        <span className={cn("inline-flex items-center gap-1 text-xs font-semibold", t.direction === "same" ? "text-ink-3" : t.good ? "text-[var(--good-ink,var(--good))]" : "text-[var(--critical-ink)]")}>
                          <Icon className="size-3.5" aria-hidden />was {fmt(t.before)}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </ChartCard>
    </div>
  );
}
