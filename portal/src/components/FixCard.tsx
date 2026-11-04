// "How to fix it": the documented fix for this failure's error, with this failure's details filled in, as a checklist.
// Then: did it work? Yes → confidence grows (and what you did is kept for next time); no → say why, and the next
// approach is chosen from your reason (the documented alternatives first, then the AI rethinks).
import {
  BookOpen, Check, CheckCircle2, ChevronDown, Copy, ExternalLink, History, Lightbulb, Loader2, PartyPopper, RotateCcw, ShieldCheck, Sparkles,
  ThumbsDown, ThumbsUp, Users, Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ChartCard } from "@/components/Cards";
import { Button } from "@/components/ui/button";
import { post } from "@/lib/api";
import { ago, time } from "@/lib/format";
import { cn } from "@/lib/utils";

interface Step { do: string; code?: string }
interface Attempt {
  id: number; variant: string; source: "library" | "ai" | "team" | "escalate"; title: string; steps: Step[]; verify: string;
  status: "open" | "worked" | "failed"; reason: string | null; note: string | null; created_at: string; created_by: string | null;
  answered_at: string | null; answered_by: string | null;
}
export interface FixView {
  applicable: boolean; signature: string; code: string | null; known: boolean; title: string; meaning: string; source: string;
  docs: { label: string; url: string }[]; pipeline: string; step: string | null; run_id: string; run_at: string; error: string;
  current: Attempt | null; history: Attempt[]; current_stats: { worked: number; failed: number } | null;
  totals: { worked: number; failed: number; approaches: number | null }; team_notes: { by: string; at: string; note: string; approach: string }[];
  next_run_ok: { at: string; run_id: string } | null; incident: { id: number; status: string } | null; celebrate: boolean;
}

const SOURCE: Record<Attempt["source"], { label: string; icon: typeof BookOpen; tone: string }> = {
  library: { label: "Documented fix", icon: BookOpen, tone: "var(--accent)" },
  team: { label: "What worked for your team", icon: Users, tone: "var(--good)" },
  ai: { label: "AI-written · not yet proven", icon: Sparkles, tone: "var(--cat-4)" },
  escalate: { label: "Next step: escalate", icon: Lightbulb, tone: "var(--warning)" },
};
const WHY = ["A step didn't apply to us", "Still the same error", "A different error now", "Couldn't find that setting", "The data really is wrong"];

export function FixCard({ project, incidentId, runId, onResolved }: { project: string; incidentId?: number; runId?: string; onResolved?: () => void }) {
  const [v, setV] = useState<FixView | null>(null);
  const [gone, setGone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"ask" | "yes" | "no">("ask");
  const [text, setText] = useState("");
  const [done, setDone] = useState<Set<number>>(new Set());
  const [showErr, setShowErr] = useState(false);

  useEffect(() => {
    let live = true;
    post<FixView>(`/api/projects/${project}/fix`, incidentId ? { incident_id: incidentId } : { run_id: runId })
      .then((r) => { if (live) setV(r); })
      .catch(() => { if (live) setGone(true); });
    return () => { live = false; };
  }, [project, incidentId, runId]);
  useEffect(() => { setDone(new Set()); setMode("ask"); setText(""); }, [v?.current?.id]);
  useEffect(() => {
    if (v && window.location.hash === "#fix") document.getElementById("fix")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [v]);

  if (gone) return null;
  if (!v) return <ChartCard icon={Wrench} title="How to fix it" loading skeleton={<div className="h-40 animate-pulse rounded-xl bg-surface-2" />} />;

  const a = v.current;
  const send = async (worked: boolean) => {
    if (!a) return;
    setBusy(true);
    try {
      const r = await post<FixView>(`/api/projects/${project}/fix/${a.id}/feedback`, worked ? { worked, note: text.trim() || null } : { worked, reason: text.trim() || null });
      setV(r);
      if (worked) toast.success("Great, that's noted", { description: "This fix now ranks higher for this error, for everyone." });
      else if (r.current && r.current.id !== a.id) toast.message("Here's a different approach", { description: r.current.title });
    } catch (e) {
      toast.error("Couldn't save that", { description: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  const resolve = async () => {
    if (!v.incident || !a) return;
    try {
      await post(`/api/projects/${project}/incidents/${v.incident.id}/resolve`, { note: `Fixed: ${a.title}${a.note ? ` (${a.note})` : ""}` });
      toast.success(`Resolved #${v.incident.id}`);
      onResolved?.();
    } catch (e) { toast.error("Couldn't resolve it", { description: (e as Error).message }); }
  };
  const src = a ? SOURCE[a.source] : SOURCE.library;
  const attempt = v.history.length + 1;
  const exhausted = a?.status === "failed";

  return (
    <section id="fix" className="fix-card card-in scroll-mt-24 overflow-hidden rounded-[1.25rem] border border-line bg-surface shadow-card">
      <header className="fix-head px-6 pb-4 pt-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <p className="flex items-center gap-2 text-[0.8125rem] font-semibold text-ink"><span className="fix-badge"><Wrench className="size-4" /></span>How to fix it</p>
          <div className="flex flex-wrap items-center gap-1.5">
            {v.code && <span className="fix-pill font-mono">{v.code}</span>}
            <span className="fix-pill" style={{ color: `color-mix(in srgb, ${src.tone} 70%, var(--ink))`, background: `color-mix(in srgb, ${src.tone} 13%, transparent)` }}>
              <src.icon className="size-3" />{src.label}
            </span>
            {v.current_stats && v.current_stats.worked + v.current_stats.failed > 0 && (
              <span className="fix-pill" title="How often this approach fixed this error, across your projects">
                <ShieldCheck className="size-3 text-[var(--good)]" />worked {v.current_stats.worked} of {v.current_stats.worked + v.current_stats.failed}
              </span>
            )}
          </div>
        </div>
        <h3 className="display mt-3 text-[1.1875rem] font-bold leading-snug text-ink">{v.title}</h3>
        <p className="dq-serif mt-1.5 text-[0.96875rem] leading-relaxed text-ink-2">{v.meaning}</p>
        <button type="button" onClick={() => setShowErr((x) => !x)} className="mt-2 inline-flex items-center gap-1 text-[0.71875rem] font-medium text-ink-3 hover:text-ink">
          <ChevronDown className={cn("size-3.5 transition-transform", showErr && "rotate-180")} />{showErr ? "Hide" : "Show"} the error from {v.pipeline}{v.step ? ` › ${v.step}` : ""}
        </button>
        {showErr && <pre className="scrollbar-thin mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-xl bg-surface-2 p-3 font-mono text-[0.6875rem] leading-5 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">{v.error}</pre>}
      </header>

      {v.next_run_ok && a?.status === "open" && (
        <div className="fix-nudge mx-6 mb-3 flex flex-wrap items-center gap-2 rounded-xl px-3.5 py-2.5 text-[0.8125rem] text-ink">
          <CheckCircle2 className="size-4 shrink-0 text-[var(--good)]" />
          <span className="min-w-0 flex-1">{v.pipeline} ran successfully at {time(v.next_run_ok.at)}, after these steps were opened. Did they fix it?</span>
          <Button size="sm" variant="primary" onClick={() => setMode("yes")}><ThumbsUp />Yes</Button>
        </div>
      )}

      {a && (
        <div className="border-t border-line px-6 py-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-[0.875rem] font-semibold text-ink">
              {attempt > 1 && <span className="mr-2 rounded-full bg-accent-soft px-2 py-0.5 text-[0.6875rem] font-semibold text-accent">Approach {attempt}</span>}{a.title}
            </p>
            <span className="text-[0.6875rem] text-ink-3">{done.size}/{a.steps.length} done</span>
          </div>
          <ol className="fix-steps mt-3 space-y-2">
            {a.steps.map((s, i) => {
              const on = done.has(i);
              return (
                <li key={i} className={cn("fix-step", on && "is-done")}>
                  <button type="button" className="fix-check" aria-pressed={on} aria-label={on ? "Mark not done" : "Mark done"}
                    onClick={() => setDone((d) => { const n = new Set(d); if (n.has(i)) n.delete(i); else n.add(i); return n; })}>
                    {on ? <Check className="size-3.5" /> : <span className="text-[0.6875rem] font-bold">{i + 1}</span>}
                  </button>
                  <div className="min-w-0 flex-1">
                    <p className="text-[0.84375rem] leading-6 text-ink [overflow-wrap:anywhere]">{s.do}</p>
                    {s.code && (
                      <div className="fix-code mt-1.5">
                        <code className="min-w-0 flex-1 [overflow-wrap:anywhere]">{s.code}</code>
                        <button type="button" onClick={() => { void navigator.clipboard?.writeText(s.code!); toast.success("Copied"); }} aria-label="Copy" className="fix-copy"><Copy className="size-3.5" /></button>
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
          {a.verify && <p className="mt-3 flex items-start gap-2 text-[0.78125rem] text-ink-2"><CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-[var(--good)]" /><span><b className="font-semibold text-ink">You'll know it worked when:</b> {a.verify}</span></p>}

          {/* did it work? */}
          {a.status === "open" ? (
            <div className="fix-ask mt-4 rounded-2xl p-4">
              {mode === "ask" && (
                <div className="flex flex-wrap items-center gap-2">
                  <p className="mr-auto text-[0.875rem] font-semibold text-ink">Did this fix it?</p>
                  <Button size="sm" variant="primary" onClick={() => setMode("yes")}><ThumbsUp />Yes, it worked</Button>
                  <Button size="sm" variant="secondary" onClick={() => setMode("no")}><ThumbsDown />No, it didn't</Button>
                </div>
              )}
              {mode === "yes" && (
                <div>
                  <p className="text-[0.875rem] font-semibold text-ink">Brilliant. Anything you did differently? <span className="font-normal text-ink-3">(optional; it helps the next person)</span></p>
                  <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} maxLength={1500} className="fix-input mt-2" placeholder="e.g. The quote character was empty; set it to a double quote" />
                  <div className="mt-2 flex justify-end gap-2">
                    <Button size="sm" variant="ghost" onClick={() => setMode("ask")}>Back</Button>
                    <Button size="sm" variant="primary" disabled={busy} onClick={() => void send(true)}>{busy ? <Loader2 className="animate-spin" /> : <Check />}It worked</Button>
                  </div>
                </div>
              )}
              {mode === "no" && (
                <div>
                  <p className="text-[0.875rem] font-semibold text-ink">Sorry about that. What happened?</p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {WHY.map((w) => <button key={w} type="button" className={cn("as-quick", text === w && "!text-ink !shadow-[inset_0_0_0_1px_var(--accent)]")} onClick={() => setText(w)}>{w}</button>)}
                  </div>
                  <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} maxLength={1500} className="fix-input mt-2"
                    placeholder="In your words: which step, what you saw. The next approach is chosen from this." />
                  <div className="mt-2 flex justify-end gap-2">
                    <Button size="sm" variant="ghost" onClick={() => setMode("ask")}>Back</Button>
                    <Button size="sm" variant="primary" disabled={busy} onClick={() => void send(false)}>{busy ? <Loader2 className="animate-spin" /> : <RotateCcw />}Try another way</Button>
                  </div>
                </div>
              )}
            </div>
          ) : a.status === "worked" ? (
            <div className="fix-won mt-4 flex flex-wrap items-center gap-3 rounded-2xl p-4">
              <PartyPopper className="size-5 shrink-0 text-[var(--good)]" />
              <p className="min-w-0 flex-1 text-[0.875rem] text-ink"><b className="font-semibold">Fixed, and learned.</b> {a.answered_by} confirmed it {ago(a.answered_at)}. Next time this error happens, this approach comes first.</p>
              {v.incident && v.incident.status !== "resolved" && <Button size="sm" variant="primary" onClick={() => void resolve()}><CheckCircle2 />Resolve #{v.incident.id}</Button>}
            </div>
          ) : exhausted ? (
            <p className="mt-4 rounded-2xl bg-[var(--warning-soft)] p-4 text-[0.84375rem] text-ink">
              I've run out of approaches for this one. The evidence above (the run ID and the error) is what support will need; anything you find, add as a comment so the team has it next time.
            </p>
          ) : null}
        </div>
      )}

      {(v.history.length > 0 || v.team_notes.length > 0 || v.docs.length > 0) && (
        <div className="grid gap-4 border-t border-line bg-surface-2/40 px-6 py-4 md:grid-cols-2">
          {v.history.length > 0 && (
            <div>
              <p className="flex items-center gap-1.5 text-[0.71875rem] font-semibold uppercase tracking-[0.06em] text-ink-3"><History className="size-3.5" />Tried already</p>
              <ul className="mt-2 space-y-1.5">
                {v.history.map((h, i) => (
                  <li key={h.id} className="text-[0.78125rem] leading-5">
                    <span className="font-medium text-ink">{i + 1}. {h.title}</span>{" "}
                    <span className={h.status === "worked" ? "text-[var(--good)]" : "text-[var(--critical-ink)]"}>{h.status === "worked" ? "worked" : "didn't work"}</span>
                    {h.reason && <span className="block text-ink-3">“{h.reason}” · {h.answered_by}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {v.team_notes.length > 0 && (
            <div>
              <p className="flex items-center gap-1.5 text-[0.71875rem] font-semibold uppercase tracking-[0.06em] text-ink-3"><Users className="size-3.5" />How your team fixed it before</p>
              <ul className="mt-2 space-y-1.5">
                {v.team_notes.map((n, i) => <li key={i} className="text-[0.78125rem] leading-5 text-ink-2"><span className="text-ink">“{n.note}”</span> · {n.by}, {ago(n.at)}</li>)}
              </ul>
            </div>
          )}
          {v.docs.length > 0 && (
            <div>
              <p className="flex items-center gap-1.5 text-[0.71875rem] font-semibold uppercase tracking-[0.06em] text-ink-3"><BookOpen className="size-3.5" />Source · {v.source}</p>
              <ul className="mt-2 space-y-1">
                {v.docs.map((d) => <li key={d.url}><a href={d.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[0.78125rem] font-medium text-accent hover:underline">{d.label}<ExternalLink className="size-3" /></a></li>)}
              </ul>
              {v.totals.worked + v.totals.failed > 0 && <p className="mt-2 text-[0.6875rem] text-ink-3">This error: fixed {v.totals.worked} time{v.totals.worked === 1 ? "" : "s"}, {v.totals.failed} approach{v.totals.failed === 1 ? "" : "es"} didn't work, across your projects.</p>}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
