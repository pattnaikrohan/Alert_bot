// The assistant's conversation: what was said, the actions it proposed and what became of them. Kept for the browser
// session (per person), so leaving the page and coming back carries on where you were.
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { post } from "@/lib/api";
import { refreshAll } from "@/lib/hooks";

export interface Proposal {
  id: string; tool: string; args: Record<string, unknown>; title: string; detail: string[]; does: string;
  owner_only: boolean; allowed: boolean; why_not: string | null; project: string | null;
  state?: "pending" | "running" | "done" | "cancelled" | "error"; result?: string; links?: { label: string; to: string }[];
}
export interface MiniIncident { id: number; project: string; project_name: string; title: string; severity: string; status: string; acknowledged_by: string | null; likely_cause: string }
export interface BotReply {
  text: string; actions: Proposal[]; links: { label: string; to: string }[]; suggestions: string[]; navigate: string | null;
  focus: number | null; mode: string; model: string | null; incidents: MiniIncident[];
  /** a change to the app itself ("dark mode", "side panel", "start the tour"), carried out by the shell */
  ui?: { do: string; value: string } | null;
}

/** The shell listens for this and changes the app: theme, navigation layout, tour, full screen, search. */
export const UI_EVENT = "alertbot:ui";
export interface Msg { id: string; role: "user" | "assistant"; text: string; at: number; reply?: BotReply; error?: string; pending?: boolean }

const KEY = "alertbot-assistant";
/** "take me there", "open it", "yes please": a follow-up that means "the page you just offered". */
const FOLLOW = /^(?:ok(?:ay)?|yes|yeah|sure|please|great|then)?[\s,]*(?:take me (?:there|to it)|go there|go to it|open (?:it|that|this|that page|it up)|show (?:it|me(?: it)?|that)|let'?s go|go|yes(?: please)?|please do)[\s.!]*$/i;
const uid = () => Math.random().toString(36).slice(2, 10);

export function useAssistant(project?: string | null) {
  const nav = useNavigate();
  const [msgs, setMsgs] = useState<Msg[]>(() => { try { return JSON.parse(sessionStorage.getItem(KEY) ?? "[]") as Msg[]; } catch { return []; } });
  const [busy, setBusy] = useState(false);
  const focus = useRef<number | null>(null);
  useEffect(() => {
    focus.current = [...msgs].reverse().find((m) => m.reply?.focus)?.reply?.focus ?? null;
    try { sessionStorage.setItem(KEY, JSON.stringify(msgs.slice(-60).map((m) => ({ ...m, pending: false })))); } catch { /* storage full or off */ }
  }, [msgs]);

  const patch = useCallback((id: string, f: (m: Msg) => Msg) => setMsgs((all) => all.map((m) => (m.id === id ? f(m) : m))), []);

  const send = useCallback(async (text: string): Promise<BotReply | null> => {
    const message = text.trim();
    if (!message || busy) return null;
    // what the bot offered (its buttons) travels with its words, so "the second one" can be worked out
    const history = msgs.slice(-8).map((m) => ({ role: m.role, text: m.role === "assistant"
      ? `${m.reply?.text ?? m.text}${m.reply?.links?.length ? `
[offered: ${m.reply.links.map((l) => `${l.label} → ${l.to}`).join("; ")}]` : ""}` : m.text }));
    const mine: Msg = { id: uid(), role: "user", text: message, at: Date.now() };
    // "take me there" right after the bot offered one page: open that page, no guessing
    const offered = [...msgs].reverse().find((m) => m.role === "assistant" && m.reply)?.reply?.links ?? [];
    if (FOLLOW.test(message) && offered.length === 1) {
      const l = offered[0]!;
      const reply: BotReply = { text: `Opening ${l.label.replace(/^Open\s+/i, "")}.`, actions: [], links: [], suggestions: [], navigate: l.to,
        focus: null, mode: "rules", model: null, incidents: [] };
      setMsgs((all) => [...all, mine, { id: uid(), role: "assistant", text: reply.text, at: Date.now(), reply }]);
      window.setTimeout(() => nav(l.to), 450);
      return reply;
    }
    const theirs: Msg = { id: uid(), role: "assistant", text: "", at: Date.now(), pending: true };
    setMsgs((all) => [...all, mine, theirs]);
    setBusy(true);
    try {
      const r = await post<BotReply>("/api/bot/chat", { message, history, focus: focus.current, project: project ?? null });
      patch(theirs.id, (m) => ({ ...m, pending: false, text: r.text, reply: { ...r, actions: r.actions.map((a) => ({ ...a, state: "pending" })) } }));
      if (r.ui) window.dispatchEvent(new CustomEvent(UI_EVENT, { detail: r.ui }));  // now: full screen needs the click's moment
      if (r.navigate) window.setTimeout(() => nav(r.navigate!), 650);
      return r;
    } catch (e) {
      patch(theirs.id, (m) => ({ ...m, pending: false, error: (e as Error).message }));
      return null;
    } finally {
      setBusy(false);
    }
  }, [busy, msgs, nav, patch, project]);

  const setAction = useCallback((msgId: string, actionId: string, f: (a: Proposal) => Proposal) =>
    patch(msgId, (m) => (m.reply ? { ...m, reply: { ...m.reply, actions: m.reply.actions.map((a) => (a.id === actionId ? f(a) : a)) } } : m)), [patch]);

  const confirm = useCallback(async (msgId: string, a: Proposal) => {
    setAction(msgId, a.id, (x) => ({ ...x, state: "running" }));
    try {
      const r = await post<{ message: string; links: { label: string; to: string }[] }>("/api/bot/act", { tool: a.tool, args: a.args });
      setAction(msgId, a.id, (x) => ({ ...x, state: "done", result: r.message, links: r.links }));
      refreshAll();
      return r.message;
    } catch (e) {
      setAction(msgId, a.id, (x) => ({ ...x, state: "error", result: (e as Error).message }));
      return null;
    }
  }, [setAction]);

  const cancel = useCallback((msgId: string, a: Proposal) => setAction(msgId, a.id, (x) => ({ ...x, state: "cancelled" })), [setAction]);
  const clear = useCallback(() => setMsgs([]), []);
  return { msgs, busy, send, confirm, cancel, clear };
}

/** Speech to text where the browser has it (Chrome, Edge): hold a conversation by voice. */
export function useDictation(onText: (t: string) => void) {
  const rec = useRef<{ stop: () => void } | null>(null);
  const [listening, setListening] = useState(false);
  const Ctor = typeof window !== "undefined" ? ((window as unknown as Record<string, unknown>).SpeechRecognition ?? (window as unknown as Record<string, unknown>).webkitSpeechRecognition) as
    (new () => { lang: string; interimResults: boolean; onresult: (e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void; onend: () => void; onerror: () => void; start: () => void; stop: () => void }) | undefined : undefined;
  const start = () => {
    if (!Ctor || listening) return;
    const r = new Ctor();
    r.lang = navigator.language || "en-GB";
    r.interimResults = false;
    r.onresult = (e) => { const t = e.results[0]?.[0]?.transcript; if (t) onText(t); };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    rec.current = r;
    setListening(true);
    r.start();
  };
  const stop = () => { rec.current?.stop(); setListening(false); };
  return { supported: !!Ctor, listening, start, stop };
}
