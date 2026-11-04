// "Your charts" on a project's overview: charts this person asked the bot for, in words. Only they see them; the ✕
// removes one. The chat (bottom right) turns a request into a chart: the AI picks a metric from the bot's catalogue,
// the bot computes it from its own records. Nothing here reads the monitored tables.
import { ArrowUp, Check, Loader2, MessageSquarePlus, Plus, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { del, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { ChartView, type ChartData, type ChartSpec } from "./ChartView";

interface Pinned { id: number; spec: ChartSpec; data: ChartData | null; error: string | null }
interface Msg { role: "user" | "assistant"; content: string; spec?: ChartSpec | null; data?: ChartData | null; pinned?: boolean; error?: boolean }

const EXAMPLES = [
  "How many runs failed last month, as a card",
  "Failed runs per day this month as a bar graph",
  "Total runs this month by pipeline as a pie chart",
  "Run duration for each pipeline",
  "Success rate as a gauge",
  "Rows loaded per day over the last 2 weeks as an area chart",
];

export function MyCharts({ projectKey }: { projectKey: string }) {
  const { data, reload, setData } = useApi<{ charts: Pinned[] }>(`/api/projects/${projectKey}/charts`, { refreshMs: 60000 });
  const [open, setOpen] = useState(false);
  const charts = data?.charts ?? [];

  const remove = async (c: Pinned) => {
    setData((d) => d && { charts: d.charts.filter((x) => x.id !== c.id) });
    try {
      await del(`/api/projects/${projectKey}/charts/${c.id}`);
      toast.success(`Removed “${c.spec.title}”`, { action: { label: "Undo", onClick: () => void post(`/api/projects/${projectKey}/charts`, { spec: c.spec }).then(reload) } });
    } catch (e) {
      toast.error("Couldn't remove it", { description: (e as Error).message });
      reload();
    }
  };

  return (
    <>
      <section className="mt-6" aria-label="Your charts">
        <div className="mb-3 flex flex-wrap items-end gap-x-3 gap-y-1">
          <div className="min-w-0">
            <h2 className="dq-display flex items-center gap-2 text-lg text-ink"><Sparkles className="size-4 text-accent" />Your charts</h2>
            <p className="text-xs text-ink-3">Made by asking the bot · only you can see these</p>
          </div>
        </div>
        {charts.length ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {charts.map((c, i) => (
              <div key={c.id} className="card-in" style={{ animationDelay: `${i * 35}ms` }}>
                {c.error ? (
                  <section className="relative rounded-[1.25rem] border border-line bg-surface p-5 text-sm text-ink-3 shadow-card">
                    <b className="block text-ink">{c.spec.title}</b>{c.error}
                    <button onClick={() => void remove(c)} className="absolute right-3 top-3 flex size-7 items-center justify-center rounded-lg hover:bg-surface-3" aria-label="Remove"><X className="size-4" /></button>
                  </section>
                ) : <ChartView spec={c.spec} data={c.data} onRemove={() => void remove(c)} className="h-full" />}
              </div>
            ))}
          </div>
        ) : data ? (
          <div className="flex w-full flex-col items-start gap-1.5 rounded-[1.25rem] border border-dashed border-line-strong bg-surface/60 p-5">
            <span className="flex items-center gap-2 text-sm font-semibold text-ink"><MessageSquarePlus className="size-4 text-accent" />Want another number here?</span>
            <span className="text-xs text-ink-3">Use <b className="text-ink-2">Ask for a chart</b> at the bottom right and say it in your own words, for example “{EXAMPLES[1]}” or “total runs this month as a pie chart”.</span>
          </div>
        ) : null}
      </section>
      <ChartChat projectKey={projectKey} open={open} setOpen={setOpen} onPinned={reload} />
    </>
  );
}

function ChartChat({ projectKey, open, setOpen, onPinned }: { projectKey: string; open: boolean; setOpen: (v: boolean) => void; onPinned: () => void }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { if (open) requestAnimationFrame(() => input.current?.focus()); }, [open]);
  useEffect(() => { list.current?.scrollTo({ top: list.current.scrollHeight, behavior: "smooth" }); }, [msgs, busy]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  const send = async (content: string) => {
    const q = content.trim();
    if (!q || busy) return;
    const next: Msg[] = [...msgs, { role: "user", content: q }];
    setMsgs(next);
    setText("");
    setBusy(true);
    try {
      const r = await post<{ reply: string; spec: ChartSpec | null; data: ChartData | null }>(`/api/projects/${projectKey}/charts/ask`,
        { messages: next.map((m) => ({ role: m.role, content: m.content })).slice(-12) });
      setMsgs((m) => [...m, { role: "assistant", content: r.reply, spec: r.spec, data: r.data }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: (e as Error).message, error: true }]);
    } finally { setBusy(false); }
  };

  const pin = async (i: number) => {
    const m = msgs[i];
    if (!m?.spec) return;
    try {
      await post(`/api/projects/${projectKey}/charts`, { spec: m.spec });
      setMsgs((all) => all.map((x, k) => (k === i ? { ...x, pinned: true } : x)));
      toast.success(`Added “${m.spec.title}” to your overview`);
      onPinned();
    } catch (e) { toast.error("Couldn't add it", { description: (e as Error).message }); }
  };

  return createPortal(
    <>
      {!open && (
        <button onClick={() => setOpen(true)} className="chart-fab fixed z-[60] flex items-center gap-2 rounded-full bg-accent px-4 py-3 text-sm font-semibold text-accent-ink shadow-[var(--elev-3)]"
          aria-label="Ask the bot for a chart">
          <Sparkles className="size-4" /><span className="hidden sm:inline">Ask for a chart</span>
        </button>
      )}
      {open && (
        <div className="chart-chat fixed z-[60] flex flex-col overflow-hidden rounded-[1.5rem] border border-line-strong bg-surface shadow-[var(--elev-3)]" role="dialog" aria-label="Ask for a chart">
          <header className="flex items-center gap-3 border-b border-line px-4 py-3">
            <span className="flex size-8 items-center justify-center rounded-xl bg-accent-soft text-accent"><Sparkles className="size-4" /></span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-ink">Ask for a chart</p>
              <p className="truncate text-[0.6875rem] text-ink-3">From the bot's own records · added to your overview, just for you</p>
            </div>
            <button onClick={() => setOpen(false)} className="flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="Close"><X className="size-4" /></button>
          </header>
          <div ref={list} className="scrollbar-thin min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
            {!msgs.length && (
              <div>
                <p className="dq-serif text-[1.0625rem] leading-snug text-ink">What would you like to see? Describe it in your own words, and say if you want a card, bars or a line.</p>
                <div className="mt-3 flex flex-col items-start gap-1.5">
                  {EXAMPLES.map((e) => (
                    <button key={e} onClick={() => void send(e)} className="rounded-full bg-surface-2 px-3 py-1.5 text-left text-xs text-ink-2 shadow-[inset_0_0_0_1px_var(--line)] hover:text-ink hover:shadow-[inset_0_0_0_1px_var(--accent)]">{e}</button>
                  ))}
                </div>
              </div>
            )}
            {msgs.map((m, i) => m.role === "user" ? (
              <p key={i} className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-md bg-accent px-3.5 py-2 text-sm text-accent-ink">{m.content}</p>
            ) : (
              <div key={i} className="max-w-[95%] space-y-2">
                <p className={cn("w-fit rounded-2xl rounded-bl-md px-3.5 py-2 text-sm", m.error ? "bg-[var(--critical-soft)] text-ink" : "bg-surface-2 text-ink")}>{m.content}</p>
                {!m.spec && !m.error && i === msgs.length - 1 && !busy && (
                  <div className="flex flex-wrap gap-1.5">
                    {EXAMPLES.slice(0, 3).map((e) => (
                      <button key={e} onClick={() => void send(e)} className="rounded-full bg-surface-2 px-3 py-1 text-left text-xs text-ink-2 shadow-[inset_0_0_0_1px_var(--line)] hover:text-ink hover:shadow-[inset_0_0_0_1px_var(--accent)]">{e}</button>
                    ))}
                  </div>
                )}
                {m.spec && (
                  <div className="space-y-2">
                    <ChartView spec={m.spec} data={m.data ?? null} compact />
                    <Button size="sm" variant={m.pinned ? "secondary" : "primary"} disabled={m.pinned} onClick={() => void pin(i)}>
                      {m.pinned ? <><Check />Added to your overview</> : <><Plus />Add to overview</>}
                    </Button>
                  </div>
                )}
              </div>
            ))}
            {busy && <p className="flex items-center gap-2 text-xs text-ink-3"><Loader2 className="size-3.5 animate-spin" />Working it out…</p>}
          </div>
          <form className="flex items-end gap-2 border-t border-line p-3" onSubmit={(e) => { e.preventDefault(); void send(text); }}>
            <textarea ref={input} value={text} onChange={(e) => setText(e.target.value)} rows={1} maxLength={1000}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(text); } }}
              placeholder="e.g. failed runs last month as a bar graph" aria-label="Your request"
              className="max-h-28 min-h-9 flex-1 resize-none rounded-xl border border-line-strong bg-surface-2 px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus-visible:border-accent/60 light:bg-surface" />
            <Button type="submit" size="icon" variant="primary" disabled={!text.trim() || busy} aria-label="Send"><ArrowUp /></Button>
          </form>
        </div>
      )}
    </>,
    document.body,
  );
}

