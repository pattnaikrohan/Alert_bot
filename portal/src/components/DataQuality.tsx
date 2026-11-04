// Data quality, per project: every mapped table is checked by itself right after its pipeline loads new data (and
// right after it's mapped). The overview shows what's being checked now, the analyst's summary and one card per table;
// a card opens the table's full report (?table=…). Only counts and percentages are ever read from the database.
import { ArrowRight, Loader2, Play, ShieldCheck, Sparkles, Workflow } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Notice } from "@/components/Cards";
import { DqMappings } from "@/components/DqMappings";
import { DqTableReport } from "@/components/dq/TableReport";
import { CHECK_ICON, ScoreRing, hours, nShort, pct, tone, type Overview, type TableSummary } from "@/components/dq/kit";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/menu";
import { Skeleton } from "@/components/ui/primitives";
import { post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface Dq { enabled: boolean; overview: Overview; mappings?: unknown[] }

export function DataQuality({ projectKey, owner }: { projectKey: string; owner: boolean }) {
  const [params, setParams] = useSearchParams();
  const table = params.get("table");
  const { data, error, reload } = useApi<Dq>(`/api/projects/${projectKey}/quality`, { refreshMs: 8000 });
  const [busy, setBusy] = useState(false);
  const open = (t: string | null) => setParams(t ? { tab: "quality", table: t } : { tab: "quality" });

  const checkNow = async () => {
    setBusy(true);
    try {
      const r = await post<{ status: string; error: string | null; failed_checks: number; opened: number }>(`/api/projects/${projectKey}/quality/check`);
      if (r.status !== "ok") toast.error("The check didn't finish", { description: r.error ?? r.status });
      else toast.success("Checked", { description: r.failed_checks ? `${r.failed_checks} checks failed · ${r.opened} new incidents` : "Every check passed." });
      reload();
    } catch (e) {
      toast.error("Couldn't run the checks", { description: (e as Error).message });
    } finally { setBusy(false); }
  };

  if (table) return <DqTableReport projectKey={projectKey} owner={owner} table={table} onBack={() => open(null)} />;

  const ov = data?.overview;
  const tables = ov?.tables ?? [];
  const live = ov?.live && ov.live.state !== "idle" ? ov.live : null;
  const scored = tables.filter((t) => t.score != null);
  const score = scored.length ? Math.round(scored.reduce((a, t) => a + (t.score ?? 0), 0) / scored.length) : null;
  const broken = tables.filter((t) => t.severity === "critical").length;
  const attention = tables.filter((t) => t.status === "fail" && t.severity !== "critical").length;
  const healthy = tables.filter((t) => t.status === "ok").length;
  const lastChecked = tables.map((t) => t.checked_at).filter(Boolean).sort().at(-1) ?? null;

  return (
    <div className="space-y-4">
      {error && !data && <Notice tone="warn">Couldn't load the data checks: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice>}
      {!data && !error && <Skeleton className="h-40 w-full rounded-[1.25rem]" />}

      {data && tables.length > 0 && (
        <section className="dq-hero card-in relative overflow-hidden rounded-[1.5rem] border border-line p-6 shadow-card">
          <div className="flex flex-wrap items-center gap-6">
            <ScoreRing score={score} size={96} stroke={8} />
            <div className="min-w-0 flex-1">
              <p className="eyebrow flex items-center gap-1.5 text-accent"><ShieldCheck className="size-3.5" />Data quality</p>
              <h2 className="dq-display mt-1 text-[1.75rem] leading-tight text-ink">
                {broken ? `${broken} table${broken > 1 ? "s are" : " is"} failing` : attention ? `${attention} table${attention > 1 ? "s need" : " needs"} a look`
                  : healthy ? "Every table looks healthy" : "Learning what normal looks like"}
              </h2>
              <p className="mt-1 text-sm text-ink-2">
                {tables.length} table{tables.length > 1 ? "s" : ""} checked automatically after every load{lastChecked ? ` · last check ${ago(lastChecked)}` : ""}.
                No one needs to press anything.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Tile n={healthy} label="healthy" t="good" />
              <Tile n={attention} label="need a look" t="warn" />
              <Tile n={broken} label="failing" t="crit" />
            </div>
          </div>
          <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
            {live ? (
              <span className="dq-live inline-flex min-w-0 items-center gap-2 text-sm font-medium text-ink">
                <Loader2 className="size-4 shrink-0 animate-spin text-accent" />
                <span className="truncate">{live.state === "checking"
                  ? `Checking ${(live.tables ?? []).join(", ")} now${live.reasons?.[0] ? `: ${live.reasons[0]}` : ""}`
                  : `Writing the explanation for ${(live.tables ?? []).join(", ")}…`}</span>
              </span>
            ) : (
              <span className="inline-flex items-center gap-2 text-sm text-ink-2"><span className="sb-live relative size-2 rounded-full bg-good" />Watching for new loads</span>
            )}
            <Button size="sm" variant="ghost" className="ml-auto" disabled={busy || Boolean(live)} onClick={() => void checkNow()}>
              {busy ? <Loader2 className="animate-spin" /> : <Play />}{busy ? "Checking…" : "Check again now"}
            </Button>
          </div>
        </section>
      )}

      {ov?.analysis?.summary && tables.length > 0 && (
        <section className="dq-note card-in rounded-[1.25rem] border border-line p-5 shadow-card">
          <p className="eyebrow mb-2 flex items-center gap-1.5 text-ink-3"><Sparkles className="size-3.5 text-accent" />
            {ov.analysis.source === "ai" ? "The analyst's summary" : "Summary"}
            <span className="font-normal normal-case tracking-normal text-ink-3">· {ov.analysis.source === "ai" ? `written by AI from counts and percentages only${ov.analysis.model ? ` · ${ov.analysis.model}` : ""}` : "from the check results"}{ov.report_at ? ` · ${ago(ov.report_at)}` : ""}</span>
          </p>
          <p className="dq-serif text-[1.1875rem] leading-[1.6] text-ink">{ov.analysis.summary}</p>
        </section>
      )}

      {tables.length > 0 && (
        <div className="grid gap-4 xl:grid-cols-2">
          {tables.map((t, i) => <TableCard key={t.table} t={t} i={i} onOpen={() => open(t.table)} />)}
        </div>
      )}

      <DqMappings projectKey={projectKey} owner={owner} onChanged={reload} />
    </div>
  );
}

function Tile({ n, label, t }: { n: number; label: string; t: "good" | "warn" | "crit" }) {
  return (
    <div className={cn("dq-tile min-w-[5.5rem] rounded-2xl px-3.5 py-2.5 text-center", `dq-${t}`, !n && "opacity-50")}>
      <div className="dq-num text-2xl font-bold">{n}</div>
      <div className="text-[0.6875rem] font-medium text-ink-2">{label}</div>
    </div>
  );
}

function TableCard({ t, i, onOpen }: { t: TableSummary; i: number; onOpen: () => void }) {
  const st = t.status === "fail" ? (t.severity === "critical" ? "crit" : "warn") : t.status === "ok" ? "good" : "idle";
  return (
    <article style={{ animationDelay: `${i * 40}ms` }} onClick={onOpen}
      className={cn("dq-card card-in group relative flex cursor-pointer flex-col overflow-hidden rounded-[1.25rem] border border-line bg-surface shadow-card", `dq-edge-${st}`)}>
      <header className="flex items-start gap-4 px-5 pb-3 pt-4">
        <ScoreRing score={t.score} size={54} stroke={5} />
        <div className="min-w-0 flex-1">
          <h3 className="dq-display truncate text-lg text-ink">{t.name}</h3>
          <p className="truncate text-xs text-ink-3">{[t.database, t.schema].filter(Boolean).join(" · ")}{t.connection ? ` · ${t.connection.name}` : ""}</p>
          {t.pipeline && <p className="mt-1 inline-flex items-center gap-1 rounded-full bg-surface-2 px-2 py-0.5 text-[0.6875rem] font-medium text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">
            <Workflow className="size-3" />loaded by {t.pipeline}</p>}
        </div>
        {t.incident && <span className="dq-chip dq-crit shrink-0"><i />incident open</span>}
      </header>
      {t.ai?.headline && <p className="dq-serif px-5 text-[1rem] leading-snug text-ink">{t.ai.headline}</p>}
      <dl className="mt-3 grid grid-cols-3 gap-px border-y border-line bg-line">
        <Kpi label="Rows today" value={nShort(t.rows_today)}
          sub={t.rows_delta != null ? `${t.rows_delta >= 0 ? "+" : ""}${(t.rows_delta * 100).toFixed(0)}% vs normal` : t.rows_normal != null ? `normal ${nShort(t.rows_normal)}` : "learning"}
          bad={t.rows_delta != null && Math.abs(t.rows_delta) >= 0.5} />
        <Kpi label="Newest row" value={hours(t.freshness_hours)} sub="old" bad={(t.freshness_hours ?? 0) > 26} />
        <Kpi label="Most empty column" value={t.worst_null ? pct(t.worst_null.rate) : "–"} sub={t.worst_null?.column ?? ""} bad={(t.worst_null?.rate ?? 0) >= 0.15} />
      </dl>
      <div className="flex flex-wrap gap-1.5 px-5 py-3" aria-label="Checks">
        {t.cards.map((c) => {
          const Icon = CHECK_ICON[c.key] ?? ShieldCheck;
          return (
            <Tip key={c.key} content={<span><b>{c.label}</b>: {c.status === "pass" ? "passed" : c.status === "fail" ? (c.severity === "critical" ? "failing" : "needs a look") : c.status === "not_set" ? "not set up" : "waiting for data"}</span>}>
              <span className={cn("dq-dot", `dq-${tone(c)}`)} aria-label={`${c.label}: ${c.status}`}><Icon className="size-3.5" /></span>
            </Tip>
          );
        })}
      </div>
      <footer className="mt-auto flex items-center gap-2 border-t border-line px-5 py-2.5 text-xs text-ink-3">
        <span className="truncate">{t.checked_at ? `Checked ${ago(t.checked_at)}` : "Waiting for the first check"} · {t.passing} passed{t.failing ? ` · ${t.failing} failed` : ""}</span>
        <Link to={`?tab=quality&table=${encodeURIComponent(t.table)}`} onClick={(e) => e.stopPropagation()}
          className="ml-auto inline-flex shrink-0 items-center gap-1 font-semibold text-accent">Full report<ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" /></Link>
      </footer>
    </article>
  );
}

function Kpi({ label, value, sub, bad }: { label: string; value: string; sub: string; bad?: boolean }) {
  return (
    <div className="min-w-0 bg-surface px-4 py-2.5">
      <dt className="truncate text-[0.6875rem] text-ink-3">{label}</dt>
      <dd className={cn("dq-num truncate text-lg font-bold", bad ? "text-[var(--critical-ink)]" : "text-ink")}>{value}</dd>
      <dd className="truncate text-[0.6875rem] text-ink-3">{sub}</dd>
    </div>
  );
}
