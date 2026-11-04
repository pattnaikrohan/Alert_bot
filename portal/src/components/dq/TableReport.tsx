// One table's full data-quality report: the analyst's note (AI, or the bot's rules), the fourteen checks as cards,
// every column's empty share against its normal, values and statistics, rows against their normal, schema, and the
// loading pipeline's runs. Built from counts and percentages only.
import { AlertOctagon, ArrowLeft, CheckCircle2, ChevronRight, Lightbulb, ListChecks, Loader2, Play, Settings2, ShieldCheck, Sparkles, Target, Workflow } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Notice } from "@/components/Cards";
import { MappingDialog, type DbConn } from "@/components/Databases";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/primitives";
import { api, post } from "@/lib/api";
import { ago, dateTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { Bars, CHECK_ICON, ScoreRing, StatusChip, dur, hours, n0, nShort, pct, tone, type AiTable, type CheckCard, type Column, type TableDetail } from "./kit";

export function DqTableReport({ projectKey, owner, table, onBack }: { projectKey: string; owner: boolean; table: string; onBack: () => void }) {
  const { data: d, error, reload } = useApi<TableDetail>(`/api/projects/${projectKey}/quality/table?name=${encodeURIComponent(table)}`, { refreshMs: 15000 });
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ pipelines: string[]; connections: DbConn[] } | null>(null);

  const checkNow = async () => {
    setBusy(true);
    try {
      const r = await post<{ status: string; failed_checks: number }>(`/api/projects/${projectKey}/quality/check`);
      toast.success("Checked", { description: r.failed_checks ? `${r.failed_checks} checks failed across the project` : "Every check passed." });
      reload();
    } catch (e) { toast.error("Couldn't run the checks", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  const acceptSchema = async () => {
    try { await post(`/api/projects/${projectKey}/quality/${encodeURIComponent(table)}/accept-schema`); toast.success("New schema accepted"); reload(); }
    catch (e) { toast.error("Couldn't accept it", { description: (e as Error).message }); }
  };
  const openSettings = async () => {
    const r = await api<{ pipelines: string[]; connections: DbConn[] }>(`/api/projects/${projectKey}/mappings`);
    setEditing(r);
  };

  if (error && !d) return <><Back onBack={onBack} /><Notice tone="warn">Couldn't load this table's report: {error.message}</Notice></>;
  if (!d) return <><Back onBack={onBack} /><div className="space-y-4"><Skeleton className="h-44 w-full rounded-[1.5rem]" /><Skeleton className="h-64 w-full rounded-[1.25rem]" /></div></>;

  const fails = d.cards.filter((c) => c.status === "fail");
  return (
    <div className="space-y-5">
      <Back onBack={onBack} />

      {/* the table at a glance */}
      <section className={cn("dq-hero card-in relative overflow-hidden rounded-[1.5rem] border border-line p-6 shadow-card", `dq-edge-${d.status === "fail" ? (d.severity === "critical" ? "crit" : "warn") : d.status === "ok" ? "good" : "idle"}`)}>
        <div className="flex flex-wrap items-start gap-6">
          <ScoreRing score={d.score} size={104} stroke={9} />
          <div className="min-w-0 basis-full sm:basis-0 sm:flex-1">
            <p className="eyebrow text-ink-3">{[d.database, d.schema].filter(Boolean).join(" · ")}{d.connection ? ` · ${d.connection.name}` : ""}</p>
            <h1 className="dq-display mt-1 text-[1.625rem] leading-[1.08] text-ink [overflow-wrap:anywhere] sm:text-[2.25rem]">{d.name}</h1>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <StatusChip c={{ status: d.status === "ok" ? "pass" : d.status === "fail" ? "fail" : "waiting", severity: d.severity }} />
              {d.pipeline && <span className="inline-flex items-center gap-1 rounded-full bg-surface-2 px-2.5 py-1 font-medium text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]"><Workflow className="size-3.5" />loaded by {d.pipeline}</span>}
              <span className="text-ink-3">{d.checked_at ? `checked ${ago(d.checked_at)}` : "not checked yet"}{d.last_load_at ? ` · last load ${ago(d.last_load_at)}` : ""}</span>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {d.mapping && <Button size="sm" variant="secondary" onClick={() => void openSettings()}><Settings2 />Checks for this table</Button>}
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => void checkNow()}>{busy ? <Loader2 className="animate-spin" /> : <Play />}Check again now</Button>
          </div>
        </div>
        <dl className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Big label="Rows today" value={nShort(d.rows_today)} sub={d.rows_delta != null ? `${d.rows_delta >= 0 ? "+" : ""}${(d.rows_delta * 100).toFixed(0)}% against a normal of ${nShort(d.rows_normal)}` : d.rows_normal != null ? `normal ${nShort(d.rows_normal)}` : "learning its normal"}
            bad={d.rows_delta != null && Math.abs(d.rows_delta) >= 0.5} />
          <Big label="Rows in the table" value={nShort(d.rows_total)} sub="all time" />
          <Big label="Newest row" value={hours(d.freshness_hours)} sub="old" bad={(d.freshness_hours ?? 0) > 26} />
          <Big label="Checks" value={`${d.passing}/${d.passing + fails.length}`} sub={fails.length ? `${fails.length} failing` : "all passing"} bad={fails.length > 0} />
        </dl>
        {d.incident && (
          <Link to={`/p/${projectKey}/i/${d.incident.id}`} className="mt-4 flex items-center gap-2 rounded-xl bg-[var(--critical-soft)] px-3.5 py-2.5 text-sm text-ink hover:brightness-105">
            <AlertOctagon className="size-4 text-[var(--critical-ink)]" /><b className="font-semibold">Incident open:</b><span className="truncate">{d.incident.title}</span><ChevronRight className="ml-auto size-4" />
          </Link>
        )}
      </section>

      <AnalystNote ai={d.ai} meta={d.analysis} at={d.report_at} />

      {/* the fourteen checks */}
      <section>
        <SectionTitle icon={ListChecks} title="Every check" sub="Checked automatically after each load of this table" />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {d.cards.map((c, i) => <Card key={c.key} c={c} i={i} />)}
        </div>
      </section>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <EmptyValues cols={d.columns} profiled={d.profiled_rows} />
        <div className="space-y-5">
          <Panel title="Rows per check" sub={`dashed line: normal (${nShort(d.rows_normal)})`}>
            {d.history.length ? <Bars values={d.history.map((h) => h.rows)} normal={d.rows_normal} labels={d.history.map((h) => dateTime(h.at))} unit=" rows" />
              : <p className="text-sm text-ink-3">No checks yet.</p>}
          </Panel>
          {d.loads.length > 0 && (
            <Panel title="Rows each load copied" sub="from the pipeline's copy steps">
              <Bars values={d.loads.map((l) => l.rows)} labels={d.loads.map((l) => dateTime(l.at))} unit=" rows" height={90} />
            </Panel>
          )}
          <Panel title="Duplicates and gaps">
            <div className="grid grid-cols-2 gap-3">
              <Mini label="Duplicate rows" value={n0(d.duplicates.rows)} sub={d.duplicates.rate != null ? `${pct(d.duplicates.rate, 2)} of today's rows` : d.duplicates.key.length ? "" : "set a business key"} />
              <Mini label="Repeated keys" value={n0(d.duplicates.groups)} sub={d.duplicates.key.length ? `key ${d.duplicates.key.join(", ")}` : "identical rows"} />
              <Mini label="Empty key values" value={n0(d.duplicates.empty_key_rows)} sub="not counted as duplicates" />
              <Mini label="Days with rows" value={d.partitions.recent != null ? `${d.partitions.recent}/7` : "–"} sub={d.partitions.before != null ? (d.partitions.before >= 19 ? "a daily table" : "not loaded daily") : "needs a date column"} />
            </div>
          </Panel>
        </div>
      </div>

      <Values cols={d.columns} rules={d.rules} references={d.references} />

      <div className="grid gap-5 xl:grid-cols-2">
        <Pipeline d={d} projectKey={projectKey} />
        <Schema d={d} owner={owner} onAccept={() => void acceptSchema()} />
      </div>

      {editing && d.mapping && (
        <MappingDialog open onClose={() => setEditing(null)} projectKey={projectKey} pipelines={editing.pipelines} connections={editing.connections}
          editing={d.mapping} onSaved={reload} />
      )}
    </div>
  );
}

const Back = ({ onBack }: { onBack: () => void }) => (
  <button onClick={onBack} className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-2 hover:text-ink"><ArrowLeft className="size-4" />All tables</button>
);

function SectionTitle({ icon: Icon, title, sub }: { icon: typeof ShieldCheck; title: string; sub?: string }) {
  return (
    <div className="mb-3 flex items-baseline gap-2">
      <Icon className="size-4 translate-y-0.5 text-accent" aria-hidden />
      <h2 className="dq-display text-lg text-ink">{title}</h2>
      {sub && <p className="text-xs text-ink-3">{sub}</p>}
    </div>
  );
}

function Panel({ title, sub, children, className }: { title: string; sub?: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("card-in rounded-[1.25rem] border border-line bg-surface p-5 shadow-card", className)}>
      <div className="mb-3"><h3 className="dq-display text-base text-ink">{title}</h3>{sub && <p className="text-xs text-ink-3">{sub}</p>}</div>
      {children}
    </section>
  );
}

const Big = ({ label, value, sub, bad }: { label: string; value: string; sub: string; bad?: boolean }) => (
  <div className="rounded-2xl bg-surface-2/70 px-4 py-3 shadow-[inset_0_0_0_1px_var(--line)]">
    <dt className="text-xs text-ink-3">{label}</dt>
    <dd className={cn("dq-num mt-0.5 text-[1.75rem] font-bold leading-tight", bad ? "text-[var(--critical-ink)]" : "text-ink")}>{value}</dd>
    <dd className="truncate text-xs text-ink-3">{sub}</dd>
  </div>
);
const Mini = ({ label, value, sub }: { label: string; value: string; sub: string }) => (
  <div className="rounded-xl bg-surface-2/70 px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--line)]">
    <p className="text-[0.6875rem] text-ink-3">{label}</p><p className="dq-num text-xl font-bold text-ink">{value}</p><p className="truncate text-[0.6875rem] text-ink-3">{sub}</p>
  </div>
);

function Card({ c, i }: { c: CheckCard; i: number }) {
  const Icon = CHECK_ICON[c.key] ?? ShieldCheck;
  const t = tone(c);
  return (
    <article style={{ animationDelay: `${i * 25}ms` }} className={cn("dq-check card-in flex flex-col rounded-2xl border border-line bg-surface p-4 shadow-card", `dq-edge-${t}`)}>
      <div className="flex items-center gap-2.5">
        <span className={cn("dq-dot", `dq-${t}`)}><Icon className="size-4" /></span>
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">{c.label}</h3>
        <StatusChip c={c} />
      </div>
      <p className="dq-num mt-3 text-[1.0625rem] font-bold leading-snug text-ink">{c.value || (c.status === "waiting" ? "No result yet" : "–")}</p>
      {c.normal && <p className="text-xs text-ink-3">{c.normal}</p>}
      {c.status === "fail" && c.detail && <p className="mt-2 text-[0.8125rem] leading-5 text-ink-2">{c.detail}</p>}
      {c.hint && <p className="mt-2 text-xs leading-4 text-ink-3">{c.hint}</p>}
      <p className="mt-auto pt-3 text-[0.6875rem] text-ink-3">{c.what}</p>
    </article>
  );
}

function AnalystNote({ ai, meta, at }: { ai: AiTable | null; meta: TableDetail["analysis"]; at: string | null }) {
  if (!ai) return null;
  const v = ai.verdict === "broken" ? "crit" : ai.verdict === "needs attention" ? "warn" : "good";
  return (
    <section className="dq-note card-in overflow-hidden rounded-[1.5rem] border border-line shadow-card">
      <div className="px-6 pb-5 pt-5 md:px-8">
        <p className="eyebrow mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-ink-3">
          <Sparkles className="size-3.5 text-accent" />{meta?.source === "ai" ? "The analyst's note" : "What the checks say"}
          <span className={cn("dq-chip", `dq-${v}`)}><i />{ai.verdict}</span>
          <span className="font-normal normal-case tracking-normal">{meta?.source === "ai" ? `Written by AI from counts and percentages only${meta?.model ? ` · ${meta.model}` : ""}` : meta?.note ?? "Written by the bot from the check results"}{at ? ` · ${ago(at)}` : ""}</span>
        </p>
        <h2 className="dq-serif text-[1.625rem] font-medium leading-[1.25] text-ink md:text-[1.875rem]">{ai.headline}</h2>
        {ai.explanation && ai.explanation !== ai.headline && <p className="dq-serif mt-3 max-w-[72ch] text-[1.0625rem] leading-[1.7] text-ink-2">{ai.explanation}</p>}
      </div>
      <div className="grid gap-px border-t border-line bg-line md:grid-cols-2">
        {ai.findings.length > 0 && (
          <div className="bg-surface px-6 py-5 md:px-8">
            <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-ink"><AlertOctagon className="size-4 text-[var(--critical-ink)]" />What's wrong</h3>
            <ul className="space-y-3">
              {ai.findings.map((f, i) => {
                const Icon = CHECK_ICON[f.check] ?? ShieldCheck;
                return (
                  <li key={i} className="flex gap-3">
                    <span className="dq-dot dq-warn mt-0.5"><Icon className="size-3.5" /></span>
                    <div className="min-w-0"><p className="text-sm font-semibold text-ink">{f.title}</p><p className="text-[0.8125rem] leading-5 text-ink-2">{f.detail}</p></div>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        {ai.likely_causes.length > 0 && (
          <div className="bg-surface px-6 py-5 md:px-8">
            <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-ink"><Lightbulb className="size-4 text-[var(--warning-ink)]" />Most likely causes</h3>
            <ul className="space-y-3">
              {ai.likely_causes.map((c, i) => {
                const lvl = c.confidence === "high" ? 3 : c.confidence === "medium" ? 2 : 1;
                return (
                  <li key={i} className="flex gap-3">
                    <span className="mt-1 flex shrink-0 gap-0.5" aria-label={`${c.confidence} confidence`} title={`${c.confidence} confidence`}>
                      {[1, 2, 3].map((k) => <i key={k} className={cn("block h-3.5 w-1.5 rounded-full", k <= lvl ? "bg-accent" : "bg-surface-3")} />)}
                    </span>
                    <p className="text-[0.875rem] leading-5 text-ink">{c.cause}</p>
                  </li>
                );
              })}
            </ul>
            {ai.impact && <p className="mt-4 rounded-xl bg-surface-2 px-3.5 py-2.5 text-[0.8125rem] leading-5 text-ink-2"><b className="text-ink">Impact.</b> {ai.impact}</p>}
          </div>
        )}
        {ai.next_steps.length > 0 && (
          <div className="bg-surface px-6 py-5 md:px-8">
            <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-ink"><Target className="size-4 text-accent" />What to do</h3>
            <ol className="space-y-2.5">
              {ai.next_steps.map((s, i) => (
                <li key={i} className="flex gap-3 text-[0.875rem] leading-5 text-ink">
                  <span className="dq-num flex size-6 shrink-0 items-center justify-center rounded-full bg-accent-soft text-xs font-bold text-accent">{i + 1}</span>{s}
                </li>
              ))}
            </ol>
          </div>
        )}
        {ai.fine.length > 0 && (
          <div className="bg-surface px-6 py-5 md:px-8">
            <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-ink"><CheckCircle2 className="size-4 text-[var(--good)]" />Looks healthy</h3>
            <div className="flex flex-wrap gap-1.5">{ai.fine.map((f) => <span key={f} className="dq-chip dq-good"><i />{f}</span>)}</div>
          </div>
        )}
      </div>
    </section>
  );
}

function EmptyValues({ cols, profiled }: { cols: Column[]; profiled: number | null }) {
  const [all, setAll] = useState(false);
  const sorted = useMemo(() => [...cols].sort((a, b) => (b.null_rate ?? -1) - (a.null_rate ?? -1)), [cols]);
  const shown = all ? sorted : sorted.slice(0, 14);
  const max = Math.max(0.05, ...cols.map((c) => Math.max(c.null_rate ?? 0, c.null_normal ?? 0)));
  return (
    <Panel title="Empty values in every column" sub={`share of today's ${profiled != null ? n0(profiled) + " " : ""}rows that are empty (NULL or blank) · line: its normal`}>
      {!cols.length ? <p className="text-sm text-ink-3">Measured on the first check.</p> : (
        <>
          <ul className="space-y-1.5">
            {shown.map((c) => {
              const r = c.null_rate ?? 0;
              const worse = c.null_normal != null ? r - c.null_normal > 0.02 : r >= 0.15;
              return (
                <li key={c.name} className="grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)_4.25rem] items-center gap-3 text-xs">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate font-mono text-[0.75rem] text-ink" title={`${c.name} · ${c.type}`}>{c.name}</span>
                    {c.key && <b className="dq-tag">key</b>}{c.required && <b className="dq-tag">required</b>}{c.sensitive && <b className="dq-tag dq-tag-warn">sensitive</b>}
                  </span>
                  <span className="relative h-2.5 rounded-full bg-surface-3">
                    <span className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${Math.max(r > 0 ? 1.5 : 0, (r / max) * 100)}%`, background: worse ? "var(--critical)" : r > 0 ? "var(--warning)" : "transparent" }} />
                    {c.null_normal != null && <span className="absolute -inset-y-1 w-0.5 rounded bg-ink-2" style={{ left: `${(c.null_normal / max) * 100}%` }} title={`normal ${pct(c.null_normal)}`} />}
                  </span>
                  <span className={cn("dq-num text-right font-semibold", worse ? "text-[var(--critical-ink)]" : "text-ink")}>{c.null_rate == null ? "–" : pct(c.null_rate)}</span>
                </li>
              );
            })}
          </ul>
          {cols.length > 14 && <button onClick={() => setAll((x) => !x)} className="mt-3 text-xs font-semibold text-accent">{all ? "Show fewer" : `Show all ${cols.length} columns`}</button>}
        </>
      )}
    </Panel>
  );
}

function Values({ cols, rules, references }: { cols: Column[]; rules: TableDetail["rules"]; references: TableDetail["references"] }) {
  const rows = cols.filter((c) => c.stats || c.mean != null || c.top_share != null || c.negative_rate || c.out_of_range_rate || c.invalid_rate != null);
  if (!rows.length && !rules.length && !references.length) return null;
  return (
    <Panel title="Values, rules and references" sub="statistics only for columns approved for them; never a value itself">
      {rows.length > 0 && (
        <div className="scrollbar-thin -mx-5 overflow-x-auto px-5">
          <table className="data-table min-w-[44rem]">
            <thead><tr><th>Column</th><th className="text-right">Average</th><th className="text-right">Normal avg.</th><th className="text-right">Min</th><th className="text-right">Max</th>
              <th className="text-right">Commonest value's share</th><th className="text-right">Negative</th><th className="text-right">Out of range</th><th className="text-right">Badly typed</th></tr></thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.name}>
                  <td className="font-mono text-[0.75rem] text-ink">{c.name}</td>
                  <td className="num-cell">{fmt(c.stats?.avg ?? c.mean)}</td>
                  <td className="num-cell text-ink-3">{fmt(c.mean_normal)}</td>
                  <td className="num-cell">{fmt(c.stats?.min)}</td><td className="num-cell">{fmt(c.stats?.max)}</td>
                  <td className="num-cell">{c.top_share != null ? `${pct(c.top_share, 0)}${c.top_share_normal != null ? ` (normally ${pct(c.top_share_normal, 0)})` : ""}` : "–"}</td>
                  <td className={cn("num-cell", c.negative_rate ? "font-semibold text-[var(--critical-ink)]" : "")}>{c.negative_rate != null ? pct(c.negative_rate, 2) : "–"}</td>
                  <td className={cn("num-cell", c.out_of_range_rate ? "font-semibold text-[var(--critical-ink)]" : "")}>{c.out_of_range_rate != null ? pct(c.out_of_range_rate, 2) : "–"}</td>
                  <td className={cn("num-cell", c.invalid_rate ? "font-semibold text-[var(--critical-ink)]" : "")}>{c.invalid_rate != null ? pct(c.invalid_rate, 2) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(rules.length > 0 || references.length > 0) && (
        <ul className="mt-4 grid gap-2 md:grid-cols-2">
          {[...rules.map((r) => ({ k: `r-${r.name}`, title: `Rule: ${r.name}`, status: r.status, severity: r.severity, msg: r.message, rate: r.rate })),
            ...references.map((r) => ({ k: `f-${r.reference}`, title: `Reference: ${r.reference}`, status: r.status, severity: "warning", msg: r.message, rate: r.rate }))].map((x) => (
            <li key={x.k} className="flex items-start gap-2.5 rounded-xl bg-surface-2/70 px-3.5 py-2.5 shadow-[inset_0_0_0_1px_var(--line)]">
              <span className={cn("dq-dot mt-0.5", `dq-${x.status === "fail" ? (x.severity === "critical" ? "crit" : "warn") : "good"}`)}>{x.status === "fail" ? <AlertOctagon className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}</span>
              <div className="min-w-0"><p className="truncate text-sm font-semibold text-ink">{x.title}</p>
                <p className="text-xs text-ink-2">{x.status === "fail" ? x.msg.split(": ").slice(1).join(": ") || x.msg : `Every row passes${x.rate != null ? " (0 rows breaking it)" : ""}.`}</p></div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

const fmt = (v: number | null | undefined) => (v == null ? "–" : Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 3 }));

function Pipeline({ d, projectKey }: { d: TableDetail; projectKey: string }) {
  const s = d.pipeline_stats;
  const runs = d.pipeline_runs;
  return (
    <Panel title={d.pipeline ? `The pipeline: ${d.pipeline}` : "The pipeline"} sub="duration of each run · dashed line: its normal">
      {runs.length ? (
        <Bars values={runs.map((r) => r.duration_min)} normal={s.normal_duration_min ?? null} unit=" min" height={110}
          labels={runs.map((r) => `${dateTime(r.at)} · ${r.status}`)}
          colors={runs.map((r) => (/fail|cancel/i.test(r.status) ? "var(--critical)" : /succe/i.test(r.status) ? "color-mix(in srgb, var(--good) 70%, var(--surface-3))" : "var(--ink-3)"))} />
      ) : <p className="text-sm text-ink-3">No runs seen yet.</p>}
      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
        <Mini label="Last run" value={dur(s.last_duration_min)} sub={`normally ${dur(s.normal_duration_min)}`} />
        <Mini label="Failed (7 days)" value={String(s.failed_7d ?? 0)} sub={`of ${s.runs_7d ?? 0} runs`} />
        <Mini label="Retried after failing" value={String(s.retries_7d ?? 0)} sub="in 7 days" />
      </div>
      {d.pipeline && <Link to={`/p/${projectKey}/runs?pipeline=${encodeURIComponent(d.pipeline)}`} className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-accent">Open its runs<ChevronRight className="size-3.5" /></Link>}
    </Panel>
  );
}

function Schema({ d, owner, onAccept }: { d: TableDetail; owner: boolean; onAccept: () => void }) {
  const ch = d.schema_change;
  const added = new Set(ch?.added ?? []);
  const retyped = new Map((ch?.retyped ?? []).map((r) => [r.column, r]));
  return (
    <Panel title={`Schema · ${d.columns.length} columns`} sub="compared with the accepted schema after every check">
      {ch && (
        <div className="mb-3 rounded-xl bg-[var(--warning-soft)] px-3.5 py-2.5 text-sm text-ink">
          <b>The columns changed.</b>{ch.removed?.length ? ` Removed: ${ch.removed.join(", ")}.` : ""}{ch.added?.length ? ` Added: ${ch.added.join(", ")}.` : ""}
          {ch.retyped?.length ? ` Retyped: ${ch.retyped.map((r) => `${r.column} (${r.from} → ${r.to})`).join(", ")}.` : ""}
          {owner && <Button size="sm" variant="secondary" className="mt-2" onClick={onAccept}>Accept the new schema</Button>}
        </div>
      )}
      <div className="flex max-h-64 flex-wrap gap-1.5 overflow-auto">
        {d.columns.map((c) => (
          <span key={c.name} className={cn("inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[0.71875rem] shadow-[inset_0_0_0_1px_var(--line)]",
            added.has(c.name) ? "bg-[var(--good-soft)]" : retyped.has(c.name) ? "bg-[var(--warning-soft)]" : "bg-surface-2")}>
            <span className="font-medium text-ink">{c.name}</span><span className="font-mono text-[0.65625rem] text-ink-3">{c.type.toLowerCase()}</span>
          </span>
        ))}
        {(ch?.removed ?? []).map((n) => <span key={n} className="rounded-lg bg-[var(--critical-soft)] px-2 py-1 text-[0.71875rem] text-ink line-through">{n}</span>)}
      </div>
    </Panel>
  );
}
