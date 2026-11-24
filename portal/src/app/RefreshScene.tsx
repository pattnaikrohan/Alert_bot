// "Refresh everything" as a scene: the whole app blurs behind the bot's core while it walks to Azure, thinks, checks
// every pipeline and studies the data; when the refresh is back it cheers, the core flashes and the blur dissolves.
// The steps follow the order the server works in, paced on a timer (the server reports once, at the end).
import { Activity, Check, Cloud, Database, ShieldCheck, Table2, Workflow, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { RobotSVG } from "@/bot/RobotSVG";
import { cn } from "@/lib/utils";
import "@/bot/bot.css";
import "./refresh-scene.css";

const STEPS = [
  { key: "walk", verb: "Walking", line: "Heading over to Azure: Data Factories, Synapse and Logic Apps" },
  { key: "think", verb: "Thinking", line: "Working out what changed in each of your projects" },
  { key: "check", verb: "Checking", line: "Every pipeline: latest runs, failures and slow runs" },
  { key: "study", verb: "Studying", line: "Your databases, tables and every data check" },
] as const;
const STEP_MS = 1600;    // pace while the server is still working
const RUSH_MS = 450;     // pace for the steps left once it has answered
const DONE_MS = 1500;    // the cheer before the blur lifts
const LEAVE_MS = 800;    // matches .rs-leaving in refresh-scene.css

/** What orbits the bot: the things a refresh re-reads. */
const ORBIT: LucideIcon[] = [Workflow, Database, Cloud, Table2, Activity, ShieldCheck];
/** Where the "thinking" sparks fire to, around the bot's head (viewBox 200×120, head at 100,70). */
const SYNAPSES: [number, number][] = [[52, 30], [148, 26], [30, 78], [172, 70], [78, 8], [126, 6]];

export interface SceneOutcome { ok: boolean }
interface SceneState { run: number; outcome: SceneOutcome | null; done: (() => void) | null }

let state: SceneState = { run: 0, outcome: null, done: null };
const listeners = new Set<() => void>();
const set = (s: SceneState) => { state = s; listeners.forEach((l) => l()); };
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };

/** Start the scene. Call the returned function with the outcome; its promise resolves once the app is unblurred. */
export function beginRefreshScene(): (outcome: SceneOutcome) => Promise<void> {
  const run = state.run + 1;
  set({ run, outcome: null, done: null });
  return (outcome) => new Promise<void>((resolve) => {
    if (state.run !== run || !listeners.size) { resolve(); return; }  // replaced by a newer refresh, or no scene on screen
    set({ run, outcome, done: resolve });
  });
}

type Phase = "idle" | "working" | "done" | "leaving";

export function RefreshScene() {
  const s = useSyncExternalStore(subscribe, () => state);
  const [phase, setPhase] = useState<Phase>("idle");
  const [step, setStep] = useState(0);
  const [cheer, setCheer] = useState(0);
  const [hidden, setHidden] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const after = (ms: number, fn: () => void) => { window.clearTimeout(timer.current); timer.current = window.setTimeout(fn, ms); };

  // a new refresh: start from the first step
  useEffect(() => {
    if (!s.run) return;
    setPhase("working"); setStep(0); setHidden(false);
    return () => window.clearTimeout(timer.current);
  }, [s.run]);

  // walk through the steps; once the server has answered, hurry through the rest, cheer, then lift the blur
  useEffect(() => {
    if (phase !== "working") return;
    const last = STEPS.length - 1;
    if (!s.outcome) { if (step < last) after(STEP_MS, () => setStep((n) => n + 1)); return; }
    if (step < last) { after(RUSH_MS, () => setStep((n) => n + 1)); return; }
    after(RUSH_MS, () => { setPhase("done"); setCheer((n) => n + 1); });
  }, [phase, step, s.outcome]);

  const finish = useCallback(() => {
    setPhase("leaving");
    after(LEAVE_MS, () => { setPhase("idle"); state.done?.(); });
  }, []);
  useEffect(() => { if (phase === "done") after(DONE_MS, finish); }, [phase, finish]);

  // Esc hides the scene; the refresh carries on and still reports when it's done
  useEffect(() => {
    if (phase === "idle" || hidden) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setHidden(true); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase, hidden]);
  useEffect(() => { if (hidden && s.outcome) { setPhase("idle"); state.done?.(); } }, [hidden, s.outcome]);

  if (phase === "idle" || hidden) return null;
  const working = phase === "working";
  const stage = working ? STEPS[step].key : "done";
  const ok = s.outcome?.ok ?? true;
  const title = working ? STEPS[step].verb : ok ? "All caught up" : "Done, with a few problems";
  const line = working ? STEPS[step].line : ok ? "Everything you're connected to is up to date" : "Some sources couldn't be read: the summary says which";

  return createPortal(
    <div className={cn("rs-root", `rs-${stage}`, !ok && "rs-problems", phase === "leaving" && "rs-leaving")}
      role="dialog" aria-modal="true" aria-label="Refreshing everything" style={{ "--step-ms": `${STEP_MS}ms` } as CSSProperties}>
      <div className="rs-vignette" aria-hidden />
      <div className="rs-floor" aria-hidden />

      <div className="rs-scene">
        <div className="rs-core" aria-hidden>
          <div className="rs-halo" />
          <svg className="rs-rings" viewBox="0 0 400 400">
            <defs>
              <linearGradient id="rs-arc" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="var(--rs-c1)" stopOpacity="0" />
                <stop offset="0.5" stopColor="var(--rs-c1)" />
                <stop offset="1" stopColor="var(--rs-c2)" />
              </linearGradient>
            </defs>
            <circle className="rs-ring rs-ring-ticks" cx="200" cy="200" r="190" />
            <circle className="rs-ring rs-ring-arc" cx="200" cy="200" r="168" stroke="url(#rs-arc)" />
            <circle className="rs-ring rs-ring-dash" cx="200" cy="200" r="148" />
            <circle className="rs-ring rs-ring-arc2" cx="200" cy="200" r="128" stroke="url(#rs-arc)" />
            <path className="rs-sweep" d="M200 200 L200 12 A188 188 0 0 1 362.8 106 Z" />
          </svg>

          <div className="rs-orbit-plane">
            <div className="rs-orbit-spin">
              {ORBIT.map((Icon, i) => (
                <div key={i} className="rs-chip-arm" style={{ "--a": `${(360 / ORBIT.length) * i}deg`, "--i": i } as CSSProperties}>
                  <div className="rs-chip"><Icon className="rs-chip-icon" /><Check className="rs-chip-ok" strokeWidth={3} /></div>
                </div>
              ))}
            </div>
          </div>

          <svg className="rs-synapse" viewBox="0 0 200 120">
            {SYNAPSES.map(([x, y], i) => (
              <g key={i} style={{ "--i": i } as CSSProperties}>
                <path className="rs-syn-line" d={`M100 70 L${x} ${y}`} pathLength={1} />
                <circle className="rs-syn-node" cx={x} cy={y} r="3.2" />
              </g>
            ))}
          </svg>

          <div className="rs-holo rs-holo-left"><i /><i /><i /><i /><i /><i /></div>
          <div className="rs-holo rs-holo-right"><b /><b /><b /><b /><b /></div>

          <div className="rs-bot">
            <div className="rs-bot-motion">
              <RobotSVG mood={working || ok ? "ok" : "warning"} speaking={false} pokeSignal={0} onPoke={() => {}} thinking={working} cheerSignal={cheer} gesture={working ? STEPS[step].key : null} />
            </div>
          </div>
          <div className="rs-scanline" />
          <div className="rs-flash" />
        </div>
      </div>

      <div className="rs-caption">
        <div key={stage} className="rs-caption-text">
          <h2 className="rs-title display" role="status" aria-live="polite">{title}{working && <span className="rs-ellipsis" aria-hidden><i /><i /><i /></span>}</h2>
          <p className="rs-line">{line}</p>
        </div>
        <div className="rs-segments" aria-hidden>
          {STEPS.map((st, i) => (
            <span key={st.key} className={cn("rs-seg", (!working || i < step) && "rs-seg-done", working && i === step && "rs-seg-now")}><i /></span>
          ))}
        </div>
        {working && <p className="rs-esc">Esc to keep working while it finishes</p>}
      </div>
    </div>,
    document.body,
  );
}
