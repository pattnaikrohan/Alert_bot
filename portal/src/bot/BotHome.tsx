import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight as IconGo, Bell as IconBell, BellOff as IconBellOff, ChevronRight as IconChevron, CircleCheck as IconOk,
  Hand as IconHand, MessageCircleQuestion as IconAsk,
  RefreshCw as IconCheck, Send as IconSend, Siren as IconSiren, TriangleAlert as IconWarning, Volume2 as IconVoice,
  VolumeX as IconVoiceOff, X as IconClose,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { api, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { refreshAll, useApi, useNow } from "@/lib/hooks";
import type { IncidentBrief, ProjectCard } from "@/lib/types";
import type { BotReply, Proposal } from "@/components/assistant/useAssistant";
import { RobotSVG, type Mood, type Point } from "./RobotSVG";
import { useVoice } from "./voice";
import "./bot.css";

// Soft out-of-focus dots behind the robot (CSS-animated transforms only).
const BOKEH = Array.from({ length: 14 }, (_, i) => ({
  left: `${8 + ((i * 37) % 84)}%`,
  top: `${6 + ((i * 53) % 80)}%`,
  size: 8 + ((i * 7) % 5) * 7,
  delay: `${-(i * 1.3).toFixed(1)}s`,
  dur: `${9 + (i % 5) * 2}s`,
  warm: i % 4 === 1,
}));

const ASK_EXAMPLES = ["What's wrong right now?", "Acknowledge the worst one", "Send the morning report", "Run the checks", "Why did it fail?"];
const SLEEP_AFTER_S = 60;

type FeedIncident = IncidentBrief & { project: string; project_name: string };
interface Feed {
  user: { name: string; first_name: string };
  mood: Mood;
  projects: ProjectCard[];
  active: FeedIncident[];
  recent: { at: string; kind: string; incident_id: number; title: string; severity: string; project: string; project_name: string }[];
  bot: { last_cycle_at: string | null; stale: boolean; error?: string | null };
}
interface BotAnswer { answer: string; links: { label: string; to: string }[]; mode: string; topic: string }
interface CheckResult { projects: { key: string; name: string; status: string; error: string | null; opened: number; worsened: number; resolved: number; ongoing: number }[] }

interface Line {
  id: string;
  text: string;
  tone: "ok" | "warning" | "critical" | "info";
  link?: string;
  linkLabel?: string;
  lead?: string;      // the opening words, in the tone's colour
  detailAt?: number;  // where the explanation starts: shown smaller, on its own line
  links?: { label: string; to: string }[];
  actions?: Proposal[];  // what the assistant proposed: confirm right here
}

const keyOf = (i: { project: string; id: number }) => `${i.project}-${i.id}`;

/** A warehouse name (DB.SCHEMA.TABLE) reads as its table; the full name stays in the tooltip and on the incident. */
const tidy = (s: string) => s.replace(/\b[A-Z_][A-Z0-9_$]*(?:\.[A-Z_][A-Z0-9_$]*){1,3}\b/g, (m) => m.split(".").pop()!);
/** "15 minutes ago" → "15m ago": the card keeps its width for the name. */
const brief = (s: string) => s.replace(/ minutes?/, "m").replace(/ hours?/, "h").replace(/ days?/, "d");
const TONE_TAG: Record<Line["tone"], string | null> = { critical: "Critical", warning: "Needs a look", ok: "All good", info: null };

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function linesFrom(feed: Feed): Line[] {
  const lines: Line[] = [];
  const name = feed.user.first_name;
  const n = feed.projects.length;
  if (!n) {
    return [{ id: "none", tone: "info", text: `Hi ${name}! I'm the alert bot. You're not on a project yet. Once an owner adds you, I'll watch its data and shout here when something breaks.` }];
  }
  const fresh = feed.active.filter((i) => i.status === "open");  // acknowledged ones are being handled
  const handled = feed.active.length - fresh.length;
  const crit = fresh.filter((i) => i.severity === "critical").length;
  const onIt = handled ? ` ${handled === 1 ? "One more is" : `${handled} more are`} acknowledged and being handled.` : "";
  const opener = fresh.length
    ? `${greeting()}, ${name}! I'm watching ${n} project${n > 1 ? "s" : ""} for you, and ${fresh.length === 1 ? "one thing needs" : `${fresh.length} things need`} attention${crit ? `, ${crit === 1 ? "one of them" : `${crit} of them`} critical` : ""}.${onIt}`
    : handled
      ? `${greeting()}, ${name}! I'm watching ${n} project${n > 1 ? "s" : ""} for you. Nothing new needs attention: ${handled === 1 ? "one problem is" : `${handled} problems are`} acknowledged and being handled.`
      : `${greeting()}, ${name}! I'm watching ${n} project${n > 1 ? "s" : ""} for you. Everything looks healthy right now.`;
  lines.push({ id: "hello", tone: feed.active.length ? feed.mood : "ok", text: opener });

  for (const i of feed.active) {
    const lead = i.severity === "critical" ? "Heads up!" : "Keep an eye on this:";
    const head = `${lead} ${tidy(i.title)}, in ${i.project_name}.`;
    const cause = i.likely_cause ? ` ${tidy(i.likely_cause.split("\n")[0]!)}` : "";
    lines.push({
      id: `inc-${keyOf(i)}`,
      tone: i.severity === "critical" ? "critical" : "warning",
      text: `${head}${cause}${i.status === "acknowledged" && i.acknowledged_by ? ` ${i.acknowledged_by} is on it.` : ""}`,
      lead,
      detailAt: head.length,
      link: `/p/${i.project}/i/${i.id}`,
      linkLabel: "Show me",
    });
  }

  const fixed = feed.recent.find((e) => e.kind === "resolved" && Date.now() - new Date(e.at).getTime() < 24 * 3600 * 1000);
  if (fixed) {
    lines.push({ id: `fixed-${fixed.incident_id}`, tone: "ok", lead: "Good news:", text: `Good news: “${tidy(fixed.title)}” in ${fixed.project_name} resolved ${ago(fixed.at)}. All its checks pass again.`,
      link: `/p/${fixed.project}/i/${fixed.incident_id}`, linkLabel: "See what happened" });
  }
  if (!feed.active.length) {
    const healthy = feed.projects.map((p) => `${p.display_name} ${p.health != null ? Math.round(p.health) + "%" : ""}`.trim()).join(", ");
    lines.push({ id: "clear", tone: "ok", text: `Every check passed on my last round (${healthy}). I check again every few minutes and I'll shout the moment something breaks.` });
  }
  if (feed.bot.error) {
    const signIn = /sign-?in|adf-login|SignInRequired/i.test(feed.bot.error);
    lines.splice(1, 0, { id: "bot-error", tone: "warning", text: signIn
      ? "I can't read Azure Data Factory right now: my sign-in needs renewing. Someone with access should run “alertbot adf-login” on the bot's machine."
      : `My last round failed, so what you see may be out of date: ${feed.bot.error}` });
  }
  if (feed.bot.stale) {
    lines.push({ id: "stale", tone: "warning", text: `Hmm, my last round was ${ago(feed.bot.last_cycle_at)}. If that's longer than 30 minutes, something may be stopping me from checking.` });
  }
  lines.push({ id: "tip", tone: "info", text: "Click me for a menu: I can find the worst problem, run a check, read you a summary or answer a question." });
  return lines;
}

function chime(critical: boolean) {
  try {
    const ctx = new AudioContext();
    const notes = critical ? [880, 660, 880] : [660, 880];
    notes.forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + i * 0.16);
      g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + i * 0.16 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i * 0.16 + 0.15);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.16);
      o.stop(ctx.currentTime + i * 0.16 + 0.16);
    });
  } catch {
    /* audio unavailable */
  }
}

function useTypewriter(text: string, cps = 46) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    setShown(0);
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setShown(text.length);
      return;
    }
    const step = Math.max(1, Math.round(text.length / 400)); // long answers type faster
    const id = window.setInterval(() => setShown((s) => (s >= text.length ? s : s + step)), 1000 / cps);
    return () => window.clearInterval(id);
  }, [text, cps]);
  return { visible: text.slice(0, shown), done: shown >= text.length };
}

function centerOf(el: Element | null | undefined): Point | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

const CONFETTI_COLORS = ["var(--accent)", "var(--good)", "var(--warning)", "var(--cat-2)", "var(--cat-4)", "#ffffff"];
function Confetti() {
  const pieces = useMemo(() => Array.from({ length: 36 }, (_, i) => ({
    x: `${(Math.random() - 0.5) * 520}px`, y: `${-140 - Math.random() * 260}px`, r: `${(Math.random() - 0.5) * 900}deg`,
    d: `${(Math.random() * 0.25).toFixed(2)}s`, c: CONFETTI_COLORS[i % CONFETTI_COLORS.length], w: i % 3 === 0 ? 10 : 7,
  })), []);
  return (
    <div className="confetti" aria-hidden="true">
      {pieces.map((p, i) => (
        <span key={i} style={{ "--x": p.x, "--y": p.y, "--r": p.r, animationDelay: p.d, background: p.c, width: p.w, height: p.w * 0.45 } as React.CSSProperties} />
      ))}
    </div>
  );
}

function AlertCard({ i, index, flash, cardRef, onEnter, onLeave }: {
  i: FeedIncident; index: number; flash: boolean; cardRef: (el: HTMLAnchorElement | null) => void; onEnter: () => void; onLeave: () => void;
}) {
  return (
    <Link ref={cardRef} to={`/p/${i.project}/i/${i.id}`} className={`float-card ${i.severity}${flash ? " fc-flash" : ""}`}
      style={{ animationDelay: `${index * 0.12}s, ${index * 0.7}s` }} onMouseEnter={onEnter} onMouseLeave={onLeave} onFocus={onEnter} onBlur={onLeave}>
      <span className="fc-icon">{i.severity === "critical" ? <IconBell size={20} /> : <IconWarning size={20} />}</span>
      <span className="fc-body">
        <span className="fc-meta"><span className="fc-project">{i.project_name}</span><span className="fc-time" title={new Date(i.opened_at).toLocaleString()}>{brief(ago(i.opened_at))}</span></span>
        <span className="fc-title" title={i.title}>{tidy(i.title)}</span>
        <span className="fc-cause">{tidy(i.likely_cause.split("\n")[0] ?? "")}</span>
      </span>
      <IconChevron className="fc-go" size={16} />
    </Link>
  );
}

export function BotHome() {
  const { data: feed, error } = useApi<Feed>("/api/feed", { refreshMs: 15000 });
  useNow();
  const navigate = useNavigate();
  const voice = useVoice();
  const [index, setIndex] = useState(0);
  const [override, setOverride] = useState<Line | null>(null);
  const [poke, setPoke] = useState(0);
  const [cheer, setCheer] = useState(0);
  const [expression, setExpression] = useState<Mood | undefined>();
  const [pointAt, setPointAt] = useState<Point | null>(null);
  const [flashKey, setFlashKey] = useState<string | null>(null);
  const [thinking, setThinking] = useState(false);
  const [confetti, setConfetti] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [asking, setAsking] = useState(false);
  const [question, setQuestion] = useState("");
  const [asleep, setAsleep] = useState(false);
  const [hovering, setHovering] = useState(false);
  const [sound, setSound] = useState(() => {
    try { return localStorage.getItem("alertbot.sound") === "on"; } catch { return false; }
  });
  const seen = useRef<Map<string, FeedIncident> | null>(null);
  const cards = useRef(new Map<string, HTMLElement>());
  const lastActivity = useRef(Date.now());
  const pointTimer = useRef(0);

  const lines = useMemo(() => (feed ? linesFrom(feed) : []), [feed]);
  const line = override ?? (lines.length ? lines[index % lines.length]! : null);
  const { visible, done } = useTypewriter(line?.text ?? "");
  const mood: Mood = feed?.mood ?? "ok";
  const worst = feed?.active[0];
  const worstRole = worst ? feed?.projects.find((p) => p.key === worst.project)?.role : undefined;

  const next = useCallback(() => { setOverride(null); setIndex((i) => i + 1); }, []);
  const say = useCallback((l: Line) => { setOverride(l); setAsking(false); }, []);
  const pointBriefly = useCallback((el: Element | null | undefined, ms = 3500) => {
    const p = centerOf(el);
    if (!p) return;
    setPointAt(p);
    window.clearTimeout(pointTimer.current);
    pointTimer.current = window.setTimeout(() => setPointAt(null), ms);
  }, []);

  // Speak each new line when the voice is on.
  const spokenId = useRef("");
  useEffect(() => {
    if (!line || !voice.enabled) return;
    const sig = `${line.id}:${line.text}`;
    if (spokenId.current === sig) return;
    spokenId.current = sig;
    void voice.say(line.text);
  }, [line, voice]);

  // Auto-advance after a pause, unless the user is reading, it's still talking, or it's answering something.
  useEffect(() => {
    if (!done || hovering || override || asking || voice.speaking || lines.length < 2) return;
    const id = window.setTimeout(next, line?.tone === "critical" ? 9000 : 6500);
    return () => window.clearTimeout(id);
  }, [done, hovering, override, asking, voice.speaking, next, lines.length, line?.tone]);

  // React to what changed since the last refresh: new incidents → alarm and point; resolved ones → celebrate.
  useEffect(() => {
    if (!feed) return;
    const now = new Map(feed.active.map((i) => [keyOf(i), i]));
    const before = seen.current;
    seen.current = now;
    if (!before) return;
    const fresh = feed.active.find((i) => !before.has(keyOf(i)));
    const gone = [...before.values()].filter((i) => !now.has(keyOf(i)));
    if (fresh) {
      setAsleep(false);
      setPoke((p) => p + 1);
      setOverride(null);
      const at = linesFrom(feed).findIndex((l) => l.id === `inc-${keyOf(fresh)}`);
      if (at >= 0) setIndex(at);
      setFlashKey(keyOf(fresh));
      window.setTimeout(() => setFlashKey(null), 3200);
      window.setTimeout(() => pointBriefly(cards.current.get(keyOf(fresh)), 4000), 450);
      if (sound) chime(fresh.severity === "critical");
    } else if (gone.length) {
      const g = gone[0]!;
      const resolved = feed.recent.some((e) => e.kind === "resolved" && e.incident_id === g.id && e.project === g.project);
      setAsleep(false);
      setCheer((c) => c + 1);
      setExpression("ok");
      window.setTimeout(() => setExpression(undefined), 2800);
      setConfetti((c) => c + 1);
      say({ id: `cheer-${keyOf(g)}`, tone: "ok", text: `Yay! “${g.title}” in ${g.project_name} ${resolved ? "is resolved" : "is closed"}. ${gone.length > 1 ? `${gone.length - 1} more cleared too. ` : ""}${feed.active.length ? `${feed.active.length} still open.` : "Everything is clear now!"}`,
        link: `/p/${g.project}/i/${g.id}`, linkLabel: "See what happened" });
      if (sound) chime(false);
    }
  }, [feed, sound, say, pointBriefly]);

  useEffect(() => {
    if (!confetti) return;
    const id = window.setTimeout(() => setConfetti(0), 2600);
    return () => window.clearTimeout(id);
  }, [confetti]);

  // Doze off when all is calm and nobody is around (sooner at night); any movement wakes it with a start.
  useEffect(() => {
    const wake = () => {
      lastActivity.current = Date.now();
      setAsleep((was) => {
        if (was) setPoke((p) => p + 1);
        return false;
      });
    };
    window.addEventListener("pointermove", wake, { passive: true });
    window.addEventListener("keydown", wake);
    window.addEventListener("pointerdown", wake);
    return () => {
      window.removeEventListener("pointermove", wake);
      window.removeEventListener("keydown", wake);
      window.removeEventListener("pointerdown", wake);
    };
  }, []);
  useEffect(() => {
    const id = window.setInterval(() => {
      const h = new Date().getHours();
      const idle = (Date.now() - lastActivity.current) / 1000;
      const limit = h >= 22 || h < 6 ? 15 : SLEEP_AFTER_S;
      setAsleep(mood === "ok" && !thinking && !menuOpen && !asking && !voice.speaking && !override && idle > limit);
    }, 3000);
    return () => window.clearInterval(id);
  }, [mood, thinking, menuOpen, asking, voice.speaking, override]);

  const toggleSound = () => {
    const on = !sound;
    setSound(on);
    try { localStorage.setItem("alertbot.sound", on ? "on" : "off"); } catch { /* ignore */ }
    if (on) chime(false);
  };

  // ── menu actions ──
  const showWorst = useCallback(() => {
    if (!feed || !worst) {
      say({ id: "worst-none", tone: "ok", text: "Nothing is wrong right now: no open incidents. I'll shout the moment that changes." });
      return;
    }
    setOverride(null);
    const at = linesFrom(feed).findIndex((l) => l.id === `inc-${keyOf(worst)}`);
    if (at >= 0) setIndex(at);
    setFlashKey(keyOf(worst));
    window.setTimeout(() => setFlashKey(null), 2400);
    pointBriefly(cards.current.get(keyOf(worst)), 4500);
  }, [feed, worst, say, pointBriefly]);

  const checkNow = useCallback(async () => {
    setThinking(true);
    say({ id: "checking", tone: "info", text: "On it! Running every check against the latest data now…" });
    try {
      const r = await post<CheckResult>("/api/check-now");
      const parts = r.projects.map((p) => p.status === "error"
        ? `${p.name}: I couldn't finish (${p.error ?? "unknown error"}).`
        : p.status === "skipped" ? `${p.name}: a check was already running, so I left it to finish.`
        : `${p.name}: ${p.opened ? `${p.opened} new` : "nothing new"}${p.resolved ? `, ${p.resolved} resolved` : ""}${p.worsened ? `, ${p.worsened} got worse` : ""}, ${p.ongoing + p.opened + p.worsened} open.`);
      say({ id: `checked-${Date.now()}`, tone: r.projects.some((p) => p.opened || p.worsened) ? "warning" : "ok",
        text: `Done! ${parts.join(" ")}` });
      refreshAll();
    } catch (e) {
      say({ id: "check-failed", tone: "warning", text: `I couldn't run the checks: ${(e as Error).message}` });
    } finally {
      setThinking(false);
    }
  }, [say]);

  const muteWorst = useCallback(async () => {
    if (!worst) return;
    try {
      await post(`/api/projects/${worst.project}/mutes`, { scope: worst.scope, hours: 1, reason: "Muted from the alert bot" });
      say({ id: `muted-${keyOf(worst)}`, tone: "info", text: `Done. I won't send Teams alerts about “${worst.title}” for the next hour, but I'll keep watching it here.` });
    } catch (e) {
      say({ id: "mute-failed", tone: "warning", text: `I couldn't mute it: ${(e as Error).message}` });
    }
  }, [worst, say]);

  const readSummary = useCallback(async () => {
    setThinking(true);
    try {
      const a = await api<BotAnswer>("/api/summary");
      const l: Line = { id: `summary-${Date.now()}`, tone: "info", text: a.answer, links: a.links };
      say(l);
      spokenId.current = `${l.id}:${l.text}`;
      void voice.say(a.answer); // asked to read it, so speak even with the voice switched off
    } catch (e) {
      say({ id: "summary-failed", tone: "warning", text: `I couldn't put a summary together: ${(e as Error).message}` });
    } finally {
      setThinking(false);
    }
  }, [say, voice]);

  // the conversation: questions answered, commands proposed (confirmed in the bubble), the incident in focus remembered
  const talk = useRef<{ role: "user" | "assistant"; text: string }[]>([]);
  const focusId = useRef<number | null>(null);
  const ask = useCallback(async (q: string) => {
    const text = q.trim();
    if (!text) return;
    setAsking(false);
    setQuestion("");
    setThinking(true);
    say({ id: "thinking", tone: "info", text: "Let me look…" });
    try {
      const r = await post<BotReply>("/api/bot/chat", { message: text, history: talk.current.slice(-8), focus: focusId.current ?? worst?.id ?? null });
      talk.current.push({ role: "user", text }, { role: "assistant", text: r.text });
      focusId.current = r.focus;
      say({ id: `answer-${Date.now()}`, tone: "info", text: r.text, links: r.links, actions: r.actions });
      if (r.navigate) window.setTimeout(() => navigate(r.navigate!), 900);
    } catch (e) {
      say({ id: "ask-failed", tone: "warning", text: `I couldn't answer that: ${(e as Error).message}` });
    } finally {
      setThinking(false);
    }
  }, [say, navigate, worst?.id]);

  const runAction = useCallback(async (a: Proposal) => {
    setThinking(true);
    say({ id: `doing-${a.id}`, tone: "info", text: `On it: ${a.title[0]!.toLowerCase() + a.title.slice(1)}…` });
    try {
      const r = await post<{ message: string; links: { label: string; to: string }[] }>("/api/bot/act", { tool: a.tool, args: a.args });
      setExpression("ok");
      window.setTimeout(() => setExpression(undefined), 2400);
      say({ id: `done-${a.id}`, tone: "ok", text: `Done! ${r.message}`, links: r.links });
      refreshAll();
    } catch (e) {
      say({ id: `fail-${a.id}`, tone: "warning", text: `I couldn't do that: ${(e as Error).message}` });
    } finally {
      setThinking(false);
    }
  }, [say]);

  const onAskSubmit = (e: FormEvent) => {
    e.preventDefault();
    void ask(question);
  };

  const onRobotClick = useCallback(() => {
    setAsleep(false);
    lastActivity.current = Date.now();
    setMenuOpen(true);
  }, []);

  const links = line ? [...(line.link ? [{ label: line.linkLabel ?? "Show me", to: line.link }] : []), ...(line.links ?? [])] : [];

  return (
    <main className={`bot-stage mood-${mood}${asleep ? " is-asleep" : ""}${thinking ? " is-thinking" : ""}`}>
      <div className="bot-backdrop" aria-hidden="true">
        {BOKEH.map((b, i) => (
          <span key={i} className={`bokeh${b.warm ? " warm" : ""}`}
            style={{ left: b.left, top: b.top, width: b.size, height: b.size, animationDelay: b.delay, animationDuration: b.dur }} />
        ))}
      </div>
      <div className="bot-figure">
        <div className="bot-aura" aria-hidden="true" />
        <RobotSVG mood={mood} speaking={!done || voice.speaking} pokeSignal={poke} onPoke={onRobotClick}
          pointAt={pointAt} asleep={asleep} expression={expression} cheerSignal={cheer} thinking={thinking} />
        {confetti > 0 && <Confetti key={confetti} />}
        <Menu open={menuOpen} onOpenChange={setMenuOpen} modal={false}>
          <MenuTrigger asChild>
            <button type="button" className="bot-menu-anchor" aria-label="Open the alert bot's menu" />
          </MenuTrigger>
          <MenuContent align="start" className="bot-menu">
            <MenuLabel>What can I do for you?</MenuLabel>
            <MenuItem onSelect={showWorst}><IconSiren className="size-4" style={{ color: "var(--critical-ink)" }} />What's the worst problem?</MenuItem>
            <MenuItem disabled={!worst} onSelect={() => worst && navigate(`/p/${worst.project}/i/${worst.id}`)}><IconGo className="size-4" />Take me to it</MenuItem>
            <MenuItem disabled={thinking} onSelect={() => void checkNow()}><IconCheck className="size-4" />Run a check now</MenuItem>
            {worst && worstRole === "owner" && (
              <MenuItem onSelect={() => void muteWorst()}><IconBellOff className="size-4" />Mute its alerts for 1 hour</MenuItem>
            )}
            <MenuItem disabled={thinking} onSelect={() => void readSummary()}><IconVoice className="size-4" />Read me a summary</MenuItem>
            {voice.supported && (
              <MenuItem onSelect={voice.toggle}>{voice.enabled ? <IconVoiceOff className="size-4" /> : <IconVoice className="size-4" />}
                {voice.enabled ? "Stop reading my messages aloud" : "Read my messages aloud"}</MenuItem>
            )}
            <MenuItem onSelect={toggleSound}>{sound ? <IconBellOff className="size-4" /> : <IconBell className="size-4" />}
              {sound ? "No chime for new incidents" : "Chime when a new incident opens"}</MenuItem>
            <MenuItem onSelect={() => { setAsking(true); setOverride(null); }}><IconAsk className="size-4" />Ask me something…</MenuItem>
            <MenuSeparator />
            <MenuItem onSelect={() => { setPoke((p) => p + 1); next(); }}><IconHand className="size-4" />Just say hi</MenuItem>
          </MenuContent>
        </Menu>
      </div>

      <div className="bot-layout">
        <section className="bot-cards" aria-label="Open incidents">
          {feed && feed.active.length > 0 ? (
            <>
              {feed.active.slice(0, 3).map((i, k) => (
                <AlertCard key={keyOf(i)} i={i} index={k} flash={flashKey === keyOf(i)}
                  cardRef={(el) => { if (el) cards.current.set(keyOf(i), el); else cards.current.delete(keyOf(i)); }}
                  onEnter={() => {
                    setPointAt(centerOf(cards.current.get(keyOf(i))));
                    setOverride(null);
                    const at = lines.findIndex((l) => l.id === `inc-${keyOf(i)}`);
                    if (at >= 0) setIndex(at);
                  }}
                  onLeave={() => setPointAt(null)} />
              ))}
              {feed.active.length > 3 && <div className="fc-more">+{feed.active.length - 3} more in the dashboard</div>}
            </>
          ) : feed && !feed.projects.length ? (
            <div className="float-card info" style={{ animationDelay: "0s, 0s" }}>
              <span className="fc-icon"><IconBell size={20} /></span>
              <span className="fc-body">
                <span className="fc-meta"><span className="fc-project">Access</span></span>
                <span className="fc-title">No projects yet</span>
                <span className="fc-cause">I’m looking in Azure for the pipelines you can see. Go to Home for the details.</span>
              </span>
            </div>
          ) : feed ? (
            <div className="float-card ok" style={{ animationDelay: "0s, 0s" }}>
              <span className="fc-icon"><IconOk size={20} /></span>
              <span className="fc-body">
                <span className="fc-meta"><span className="fc-project">All projects</span><span className="fc-time">checked {ago(feed.bot.last_cycle_at)}</span></span>
                <span className="fc-title">All clear</span>
                <span className="fc-cause">No open incidents. Every check passed.</span>
              </span>
            </div>
          ) : null}
        </section>

        <section className="bot-center">
          {(line || asking) && (
            <div className={`bubble tone-${asking ? "info" : line!.tone}`} onMouseEnter={() => setHovering(true)} onMouseLeave={() => setHovering(false)}>
              <div className="bubble-head">
                <span className="bubble-name">
                  <span className={`bubble-dot ${mood}`} />Alert Bot
                  {thinking && <span className="bubble-thinking" aria-label="working"><i /><i /><i /></span>}
                  {asleep && <span className="bubble-asleep">dozing</span>}
                  {!asking && line && TONE_TAG[line.tone] && <span className={`bubble-tag ${line.tone}`}>{TONE_TAG[line.tone]}</span>}
                </span>
                {override || asking
                  ? <button type="button" className="bubble-close" onClick={() => { setOverride(null); setAsking(false); voice.stop(); }} aria-label="Close"><IconClose size={14} /></button>
                  : lines.length > 1 && lines.length <= 10
                    ? <span className="bubble-dots" role="group" aria-label="Messages">
                        {lines.map((l, k) => (
                          <button key={l.id} type="button" className={k === index % lines.length ? "on" : undefined} aria-current={k === index % lines.length ? "true" : undefined}
                            onClick={() => { setOverride(null); setIndex(k); }} aria-label={`Message ${k + 1} of ${lines.length}`} />
                        ))}
                      </span>
                    : <span className="bubble-count">{(index % lines.length) + 1} / {lines.length}</span>}
              </div>
              {asking ? (
                <form className="bubble-ask" onSubmit={onAskSubmit}>
                  <p className="bubble-text">What would you like to know?</p>
                  <div className="ask-row">
                    <input autoFocus value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={500}
                      placeholder="Ask, or tell me: “ack the worst one”, “mute pl_uat for 2 hours”…" aria-label="Your question or command" />
                    <Button size="sm" variant="primary" type="submit" disabled={!question.trim()}><IconSend />Ask</Button>
                  </div>
                  <div className="ask-chips">
                    {ASK_EXAMPLES.map((q) => <button key={q} type="button" onClick={() => void ask(q)}>{q}</button>)}
                  </div>
                </form>
              ) : line && (
                <>
                  <p className="bubble-text" aria-hidden="true">
                    {line.lead && <span className="bubble-lead">{visible.slice(0, line.lead.length)}</span>}
                    {visible.slice(line.lead?.length ?? 0, line.detailAt)}
                    {line.detailAt != null && visible.length > line.detailAt && <span className="bubble-detail">{visible.slice(line.detailAt).trimStart()}</span>}
                    <span className={`caret${done ? " off" : ""}`} />
                  </p>
                  <p className="sr-only" aria-live="polite">{line.text}</p>
                  <div className="bubble-actions">
                    {line.actions?.filter((a) => a.allowed).slice(0, 2).map((a) => (
                      <Button key={a.id} size="sm" variant="primary" disabled={thinking} onClick={() => void runAction(a)} title={a.title} className="max-w-full">
                        <IconOk /><span className="truncate">{(line.actions?.filter((x) => x.allowed).length ?? 0) > 1 ? `Confirm: ${a.title}` : "Confirm"}</span>
                      </Button>
                    ))}
                    {line.actions?.some((a) => !a.allowed) && <span className="text-xs text-[var(--warning-ink)]">{line.actions.find((a) => !a.allowed)!.why_not}</span>}
                    {links.slice(0, 2).map((l, k) => (
                      <Button key={l.to} size="sm" variant={k === 0 && !line.actions?.length ? "primary" : "secondary"} onClick={() => navigate(l.to)}>{l.label}</Button>
                    ))}
                    {override
                      ? <Button size="sm" variant="ghost" onClick={() => setAsking(true)}><IconAsk />Ask another</Button>
                      : lines.length > 1 && <Button size="sm" variant="ghost" onClick={next}>Next <IconChevron /></Button>}
                  </div>
                </>
              )}
            </div>
          )}
          {error && !feed && <div className="bubble tone-warning"><p className="bubble-text">I can’t reach my API right now. {error.message}</p></div>}
        </section>
      </div>

    </main>
  );
}
