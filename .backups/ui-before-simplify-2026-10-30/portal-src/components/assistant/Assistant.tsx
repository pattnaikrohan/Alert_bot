// The assistant: ask anything, or tell it what to do. Every change it proposes arrives as a card that says exactly what
// will happen; nothing happens until you confirm it.
import {
  ArrowUp, BellOff, BellRing, Check, CheckCheck, CircleCheck, Compass, Hand, Loader2, MessageSquarePlus, Mic, MicOff, Play, RefreshCw, RotateCcw,
  ShieldAlert, Sparkles, Sunrise, X, type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { BotAvatar } from "@/components/BotAvatar";
import { SeverityPill } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useAssistant, useDictation, type MiniIncident, type Msg, type Proposal } from "./useAssistant";

export const TOOL_ICON: Record<string, LucideIcon> = {
  acknowledge: Hand, resolve: CircleCheck, comment: MessageSquarePlus, accept_normal: RotateCcw, send_alert: BellRing, mute: BellOff,
  unmute: BellRing, send_report: Sunrise, run_checks: Play, refresh: RefreshCw,
};

export function ActionCard({ a, onConfirm, onCancel, compact }: { a: Proposal; onConfirm: () => void; onCancel: () => void; compact?: boolean }) {
  const Icon = TOOL_ICON[a.tool] ?? Sparkles;
  const state = a.state ?? "pending";
  return (
    <div className={cn("as-action", `is-${state}`, !a.allowed && "is-blocked", compact && "is-compact")}>
      <div className="flex items-start gap-3">
        <span className="as-action-icon"><Icon className="size-4" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2 text-[0.875rem] font-semibold text-ink">
            {a.title}
            {a.owner_only && <span className="as-badge">owners</span>}
            {state === "done" && <span className="as-badge good"><Check className="size-3" />done</span>}
            {state === "cancelled" && <span className="as-badge">cancelled</span>}
          </p>
          {!compact && a.detail.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-[0.78125rem] leading-5 text-ink-2">
              {a.detail.map((d, i) => <li key={i} className="[overflow-wrap:anywhere]">{d}</li>)}
            </ul>
          )}
          {state === "pending" && !compact && <p className="mt-1 text-[0.6875rem] text-ink-3">{a.does}</p>}
          {!a.allowed && <p className="mt-1.5 flex items-center gap-1.5 text-[0.75rem] text-[var(--warning-ink)]"><ShieldAlert className="size-3.5" />{a.why_not}</p>}
          {a.result && <p className={cn("mt-1.5 text-[0.8125rem] leading-5", state === "error" ? "text-[var(--critical-ink)]" : "text-ink")}>{a.result}</p>}
          {a.links && a.links.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">{a.links.map((l) => <Button key={l.to} size="sm" variant="secondary" asChild><Link to={l.to}>{l.label}</Link></Button>)}</div>
          )}
        </div>
      </div>
      {(state === "pending" || state === "running" || state === "error") && a.allowed && (
        <div className="mt-3 flex items-center justify-end gap-2">
          {state !== "running" && <Button size="sm" variant="ghost" onClick={onCancel}><X />Cancel</Button>}
          <Button size="sm" variant="primary" onClick={onConfirm} disabled={state === "running"}>
            {state === "running" ? <Loader2 className="animate-spin" /> : <Check />}{state === "error" ? "Try again" : "Confirm"}
          </Button>
        </div>
      )}
    </div>
  );
}

function IncidentChip({ i }: { i: MiniIncident }) {
  return (
    <Link to={`/p/${i.project}/i/${i.id}`} className="as-inc">
      <SeverityPill severity={i.severity} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[0.8125rem] font-medium text-ink">#{i.id} {i.title}</span>
        <span className="block truncate text-[0.6875rem] text-ink-3">
          {i.status === "acknowledged" ? `Acknowledged by ${i.acknowledged_by ?? "someone"}` : "Needs attention"} · {i.project_name}
        </span>
      </span>
    </Link>
  );
}

const STARTERS = [
  { icon: Sparkles, text: "What's wrong right now?" },
  { icon: Hand, text: "Acknowledge the worst one" },
  { icon: CheckCheck, text: "Ack everything critical" },
  { icon: Sunrise, text: "Send the morning report" },
  { icon: BellOff, text: "Mute pl_uat for 2 hours" },
  { icon: Compass, text: "Take me to lineage" },
];

export function AssistantChat({ project, className, starters = STARTERS, prompt, intro, empty, tray, toolbar, autoFocus }: {
  project?: string | null; className?: string; starters?: typeof STARTERS;
  /** something said from outside the chat (an example clicked elsewhere on the page): sent as if typed */
  prompt?: { text: string; n: number } | null;
  /** what the empty chat says first, in place of the default */
  intro?: { title: string; text: string };
  /** shown in place of the default welcome while the conversation is empty */
  empty?: ReactNode;
  /** more things to say, in the row just above the input once a conversation has started (after the bot's own suggestions) */
  tray?: ReactNode;
  /** controls drawn above the conversation, with the conversation itself (to save it, or clear it) */
  toolbar?: (c: { msgs: Msg[]; clear: () => void; busy: boolean }) => ReactNode;
  /** put the cursor in the input straight away */
  autoFocus?: boolean;
}) {
  const { msgs, busy, send, confirm, cancel, clear } = useAssistant(project);
  const [text, setText] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const dictation = useDictation((t) => { setText(t); window.setTimeout(() => void submit(t), 150); });
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [msgs]);
  useEffect(() => { if (autoFocus) box.current?.focus(); }, [autoFocus]);
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "/" && document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA") { e.preventDefault(); box.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const submit = async (t = text) => {
    if (!t.trim()) return;
    setText("");
    await send(t);
    box.current?.focus();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); }
    if (e.key === "ArrowUp" && !text) { const last = [...msgs].reverse().find((m) => m.role === "user"); if (last) { e.preventDefault(); setText(last.text); } }
  };
  const said = useRef(0);
  useEffect(() => {
    if (!prompt || prompt.n === said.current) return;
    said.current = prompt.n;
    void submit(prompt.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prompt]);
  const lastReply = [...msgs].reverse().find((m) => m.role === "assistant" && m.reply)?.reply;
  const suggestions = !busy && lastReply?.suggestions?.length ? lastReply.suggestions : [];

  return (
    <div className={cn("as-chat", className)}>
      {toolbar?.({ msgs, clear, busy })}
      <div className="as-scroll scrollbar-thin" aria-live="polite">
        {msgs.length === 0 && empty}
        {msgs.length === 0 && !empty && (
          <div className="as-empty">
            <BotAvatar mood="ok" className="mx-auto size-14" />
            <p className="display mt-3 text-center text-lg font-semibold text-ink">{intro?.title ?? "Ask me anything, or tell me what to do"}</p>
            <p className="mx-auto mt-1 max-w-md text-center text-[0.8125rem] text-ink-2">{intro?.text ?? "I can acknowledge, resolve, comment, mute, send alerts and reports, run checks and take you anywhere in the app. Nothing changes until you confirm."}</p>
            {starters.length > 0 && <div className="mx-auto mt-5 grid max-w-xl gap-2 sm:grid-cols-2">
              {starters.map((s) => (
                <button key={s.text} type="button" className="as-starter" onClick={() => void submit(s.text)}><s.icon className="size-4 shrink-0 text-accent" aria-hidden />{s.text}</button>
              ))}
            </div>}
          </div>
        )}
        {msgs.map((m) => <Message key={m.id} m={m} onConfirm={(a) => void confirm(m.id, a)} onCancel={(a) => cancel(m.id, a)}
          onConfirmAll={() => m.reply?.actions.filter((a) => a.allowed && (a.state ?? "pending") === "pending").forEach((a) => void confirm(m.id, a))} />)}
        <div ref={end} />
      </div>
      {(suggestions.length > 0 || (msgs.length > 0 && tray)) && (
        <div className="as-suggest scrollbar-thin" aria-label="Things to say next">
          {suggestions.map((s) => <button key={s} type="button" onClick={() => void submit(s)}>{s}</button>)}
          {suggestions.length > 0 && msgs.length > 0 && tray ? <span className="as-suggest-sep" aria-hidden /> : null}
          {msgs.length > 0 && tray}
        </div>
      )}
      <form className="as-input" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <textarea ref={box} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} rows={1} maxLength={2000}
          placeholder={dictation.listening ? "Listening…" : "Ask, or tell me what to do: “ack #12”, “resolve the pl_sql one, re-ran it”…"} aria-label="Message the assistant" />
        <div className="flex items-center gap-1">
          {msgs.length > 0 && !toolbar && <Button type="button" size="icon-sm" variant="ghost" onClick={clear} aria-label="New conversation" title="New conversation"><RotateCcw /></Button>}
          {dictation.supported && (
            <Button type="button" size="icon-sm" variant={dictation.listening ? "primary" : "ghost"} onClick={dictation.listening ? dictation.stop : dictation.start}
              aria-label={dictation.listening ? "Stop listening" : "Speak"} title={dictation.listening ? "Stop listening" : "Speak instead of typing"}>
              {dictation.listening ? <MicOff /> : <Mic />}
            </Button>
          )}
          <Button type="submit" size="icon-sm" variant="primary" disabled={!text.trim() || busy} aria-label="Send">{busy ? <Loader2 className="animate-spin" /> : <ArrowUp />}</Button>
        </div>
      </form>
    </div>
  );
}

function Message({ m, onConfirm, onCancel, onConfirmAll }: { m: Msg; onConfirm: (a: Proposal) => void; onCancel: (a: Proposal) => void; onConfirmAll: () => void }) {
  if (m.role === "user") return <div className="as-msg is-user"><div className="as-bubble">{m.text}</div></div>;
  const r = m.reply;
  const pending = r?.actions.filter((a) => a.allowed && (a.state ?? "pending") === "pending") ?? [];
  return (
    <div className="as-msg is-bot">
      <BotAvatar mood={m.error ? "warning" : "ok"} className="size-8 shrink-0" />
      <div className="min-w-0 flex-1 space-y-2">
        <div className="as-bubble">
          {m.pending ? <span className="as-typing" aria-label="Thinking"><i /><i /><i /></span>
            : m.error ? <span className="text-[var(--critical-ink)]">I couldn't do that: {m.error}</span>
            : <p className="whitespace-pre-line">{r?.text}</p>}
          {r?.navigate && <p className="mt-1.5 flex items-center gap-1.5 text-[0.75rem] text-ink-3"><Compass className="size-3.5" />Taking you there…</p>}
        </div>
        {r && r.incidents.length > 0 && r.actions.length === 0 && <div className="grid gap-1.5 sm:grid-cols-2">{r.incidents.map((i) => <IncidentChip key={i.id} i={i} />)}</div>}
        {r?.actions.map((a) => <ActionCard key={a.id} a={a} onConfirm={() => onConfirm(a)} onCancel={() => onCancel(a)} />)}
        {pending.length > 1 && <div className="flex justify-end"><Button size="sm" variant="primary" onClick={onConfirmAll}><CheckCheck />Confirm all {pending.length}</Button></div>}
        {r && r.links.length > 0 && (
          <div className="flex flex-wrap gap-1.5">{r.links.slice(0, 3).map((l) => <Button key={l.to} size="sm" variant="secondary" asChild><Link to={l.to}>{l.label}</Link></Button>)}</div>
        )}
        {r && !m.pending && (
          <p className="text-[0.65625rem] text-ink-3">{r.mode === "ai" && r.model ? `Understood with AI (${r.model.split("/").pop()!.replace(/:free$/, "")})` : "Understood from your words"} · from your live data</p>
        )}
      </div>
    </div>
  );
}

