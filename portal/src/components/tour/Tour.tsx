// The guided tour: a spotlight that glides from section to section (opening the right page on the way) and a card that
// follows it, with a small animation of what each part does. Opens on someone's first sign-in and from the top bar's
// Tour button; "seen" is saved with their account. The spotlight is one SVG mask driven by a single
// requestAnimationFrame loop writing attributes and transforms directly (no React render per frame), with
// frame-rate-independent springs, so it's smooth at 60, 120 or 144 Hz. Keyboard: → / Enter next, ← back, Esc close.
import { ArrowLeft, ArrowRight, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import * as Art from "./art";
import "./tour.css";

export const TOUR_EVENT = "alertbot:tour";
export const openTour = () => window.dispatchEvent(new CustomEvent(TOUR_EVENT));

interface Step {
  id: string; eyebrow: string; title: string; body: ReactNode; art: ReactNode;
  route?: string; find?: () => HTMLElement | null; pad?: number;
}

const q = (sel: string) => () => document.querySelector<HTMLElement>(sel);
const near = (sel: string, up = "section") => () => (document.querySelector(sel)?.closest(up) as HTMLElement | null) ?? null;
const first = (...fs: (() => HTMLElement | null)[]) => () => fs.reduce<HTMLElement | null>((el, f) => el ?? f(), null);

function steps(project: string | null): Step[] {
  const p = project ? `/p/${project}` : null;
  const list: (Step | false)[] = [
    { id: "welcome", eyebrow: "Welcome", title: "Your data pipelines, watched for you", art: <Art.Welcome />,
      body: <>Alert Bot watches every Data Factory, Synapse and Logic Apps pipeline your Azure account can see, checks the data they load, and tells you what broke, why, and what to do. This two-minute tour shows you around.</> },
    { id: "dock", eyebrow: "The dock", title: "Everything is one click away", art: <Art.Dock />, find: q("[data-tour=dock]"), pad: 6,
      body: <>Every page lives down here. Hover to magnify, click to open. <b>Drag an icon sideways to rearrange the dock</b> however you like; your order is saved with your account.</> },
    { id: "home", eyebrow: "Home", title: "What needs attention, first", art: <Art.Home />, route: "/", find: first(q("main section"), q("main [class*='card']")),
      body: <>Home opens with whatever needs you now: open incidents, failing pipelines and tables that didn't get their data, worst first.</> },
    { id: "pipelines", eyebrow: "All pipelines", title: "Every pipeline, run or not", art: <Art.Pipelines />, route: "/pipelines", find: first(near("main table"), q("main section")),
      body: <>Each pipeline's latest run, the last 7 days and its health. New pipelines appear by themselves within minutes; renamed or deleted ones drop out.</> },
    { id: "lineage", eyebrow: "Lineage", title: "Where data comes from, and where it goes", art: <Art.Lineage />, route: "/lineage", find: first(near(".lin-canvas"), q("main section")),
      body: <>Files → pipelines → tables → whatever reads them next, across platforms. Click anything to see exactly what a failure there would affect downstream.</> },
    { id: "databases", eyebrow: "Databases", title: "Connect a database once", art: <Art.Databases />, route: "/databases", find: q("main section"),
      body: <>Connect SQL Server, Azure SQL or Snowflake (or paste a connection string). The bot maps the tables your pipelines load and checks their data after every load. It reads <b>counts and percentages only</b>, never rows.</> },
    !!p && { id: "overview", eyebrow: "Project overview", title: "One page for the whole project", art: <Art.Score />, route: p!, find: q("main section"),
      body: <>Health score, success rate, deliveries, slow runs and why runs fail, for the project you're in. Switch projects from the dock.</> },
    !!p && { id: "quality", eyebrow: "Data quality", title: "Fourteen checks after every load", art: <Art.Checks />, route: `${p}/tables?tab=quality`,
      find: first(q(".dq-hero"), q("main section")),
      body: <>Row counts, empty values, duplicates, freshness, schema, ranges, rules, references and more, against what's normal for each table. Open a table for its full report and the AI's explanation.</> },
    { id: "incidents", eyebrow: "Incidents", title: "What broke, why, and what to do", art: <Art.Incident />, route: "/incidents", find: q("main section"),
      body: <>Each problem becomes one incident with the evidence, the likely cause and the next step. Acknowledge it, mute it, or tell the bot "this is expected now" and it learns.</> },
    !!p && { id: "charts", eyebrow: "Your charts", title: "Ask for any chart, in your own words", art: <Art.Charts />, route: p!, find: q(".chart-fab"), pad: 8,
      body: <>"Failed runs per day this month as a bar chart", "runs by pipeline as a pie". It's added to your overview, just for you, and the ✕ removes it.</> },
    { id: "bot", eyebrow: "The bot", title: "Ask it anything, or tell it what to do", art: <Art.Bot />, route: "/", find: first(q(".home-bot"), q("main")),
      body: <>Click <b>Ask the bot</b> on Home (or <b>Ask about this page</b> on any page). It answers from your live data, changes things once you confirm, takes you anywhere and changes the app for you. Save or clear the conversation any time.</> },
    { id: "live", eyebrow: "Always watching", title: "Live, every 30 seconds", art: <Art.Live />, find: q("[data-tour=live]"), pad: 6,
      body: <>The bot looks at Azure every 30 seconds and checks a run the moment it finishes. Nothing to refresh: pages update by themselves.</> },
    { id: "tools", eyebrow: "Top bar", title: "Refresh and alerts", art: <Art.Tools />, find: q("[data-tour=tools]"), pad: 6,
      body: <>Refresh reads Azure right now. The bell lists open incidents. Theme, full screen and the menu's place are in your account menu (your initials, in the dock).</> },
    { id: "search", eyebrow: "Search", title: "Find anything", art: <Art.Search />, find: first(q("[data-tour=search]:not([hidden])"), q("[data-tour=search-sm]")), pad: 6,
      body: <>Pipelines, Logic Apps, tables and pages, as you type. Ctrl+K opens it from anywhere.</> },
    { id: "done", eyebrow: "That's it", title: "You're all set", art: <Art.Done />, find: first(q("[data-tour=dock-account]"), q(".dock-side-footer")), pad: 6,
      body: <>Everything runs by itself from here: checks, learning, alerts to Teams. Take this tour again any time from your <b>account menu</b> (your initials), or just ask the bot.</> },
  ];
  return list.filter(Boolean) as Step[];
}

type Box = { x: number; y: number; w: number; h: number };

export function Tour({ open, project, onClose }: { open: boolean; project: string | null; onClose: (finished: boolean) => void }) {
  const nav = useNavigate();
  const loc = useLocation();
  const all = useMemo(() => steps(project), [project]);
  const [i, setI] = useState(0);
  const step = all[Math.min(i, all.length - 1)]!;
  const hole = useRef<SVGRectElement>(null);
  const ring = useRef<SVGRectElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const target = useRef<HTMLElement | null>(null);
  const anim = useRef({ cur: null as Box | null, card: null as { x: number; y: number } | null, raf: 0, last: 0, since: 0, scrolled: false, foundAt: 0 });

  useEffect(() => { if (open) setI(0); }, [open]);

  // the right page for the step, then wait for its section to appear
  useEffect(() => {
    if (!open) return;
    target.current = null;
    anim.current.since = performance.now();
    anim.current.scrolled = false;
    anim.current.foundAt = 0;
    if (step.route && `${loc.pathname}${loc.search}` !== step.route && loc.pathname !== step.route) nav(step.route);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, i]);

  const frame = useCallback((t: number) => {
    const a = anim.current;
    const dt = Math.min(0.05, a.last ? (t - a.last) / 1000 : 1 / 120);
    a.last = t;
    const vw = window.innerWidth, vh = window.innerHeight;
    // look only once the step's page is showing (never on the page being left), and keep looking while it loads
    const onPage = !step.route || `${window.location.pathname}${window.location.search}` === step.route || window.location.pathname === step.route;
    if (target.current && !document.contains(target.current)) target.current = null;
    if (!target.current && step.find && onPage) { target.current = step.find(); a.foundAt = t; }
    const el = target.current;
    if (el && !a.scrolled && t - a.foundAt > 320) {  // after the new page's own scroll-to-top has happened
      a.scrolled = true;
      const r0 = el.getBoundingClientRect();
      if (r0.top < 72 || r0.bottom > vh - 120) el.scrollIntoView({ block: r0.height > vh * 0.55 ? "start" : "center", behavior: "smooth" });
    }
    const pad = step.pad ?? 10;
    let goal: Box;
    if (el) {
      const r = el.getBoundingClientRect();
      // a section taller than half the screen: light up its top part and leave room for the card below it
      const tall = r.height > vh * 0.55;
      const top = Math.max(8, r.top - pad), bottom = Math.min(vh - 8, tall ? Math.max(r.top, 72) + vh * 0.42 : r.bottom + pad);
      goal = { x: Math.max(8, r.left - pad), y: top, w: Math.min(vw - 16, r.width + pad * 2), h: Math.max(0, bottom - top) };
    } else goal = { x: vw / 2, y: vh / 2, w: 0, h: 0 };
    const k = 1 - Math.exp(-13 * dt);
    const c = a.cur ?? goal;
    const cur = { x: c.x + (goal.x - c.x) * k, y: c.y + (goal.y - c.y) * k, w: c.w + (goal.w - c.w) * k, h: c.h + (goal.h - c.h) * k };
    a.cur = cur;
    const r = Math.min(18, cur.w / 2, cur.h / 2);
    for (const node of [hole.current, ring.current]) {
      if (!node) continue;
      node.setAttribute("x", cur.x.toFixed(1)); node.setAttribute("y", cur.y.toFixed(1));
      node.setAttribute("width", Math.max(0, cur.w).toFixed(1)); node.setAttribute("height", Math.max(0, cur.h).toFixed(1));
      node.setAttribute("rx", r.toFixed(1));
    }
    if (ring.current) ring.current.style.opacity = cur.w > 4 ? "1" : "0";
    // the card: beside the spotlight where there's room (below, above, right, left), else centred
    const cw = card.current?.offsetWidth ?? 400, ch = card.current?.offsetHeight ?? 300, gap = 18;
    let cx: number, cy: number;
    if (!el || goal.w < 1) { cx = (vw - cw) / 2; cy = (vh - ch) / 2; }
    else if (vh - (goal.y + goal.h) > ch + gap + 96) { cx = goal.x + goal.w / 2 - cw / 2; cy = goal.y + goal.h + gap; }
    else if (goal.y > ch + gap + 8) { cx = goal.x + goal.w / 2 - cw / 2; cy = goal.y - ch - gap; }
    else if (vw - (goal.x + goal.w) > cw + gap + 8) { cx = goal.x + goal.w + gap; cy = goal.y + goal.h / 2 - ch / 2; }
    else if (goal.x > cw + gap + 8) { cx = goal.x - cw - gap; cy = goal.y + goal.h / 2 - ch / 2; }
    else { cx = (vw - cw) / 2; cy = vh - ch - 112; }
    cx = Math.max(12, Math.min(vw - cw - 12, cx));
    cy = Math.max(12, Math.min(vh - ch - 12, cy));
    const kc = 1 - Math.exp(-11 * dt);
    const pc = a.card ?? { x: cx, y: cy };
    a.card = { x: pc.x + (cx - pc.x) * kc, y: pc.y + (cy - pc.y) * kc };
    if (card.current) card.current.style.transform = `translate3d(${a.card.x.toFixed(1)}px,${a.card.y.toFixed(1)}px,0)`;
    a.raf = requestAnimationFrame(frame);
  }, [step]);

  useLayoutEffect(() => {
    if (!open) return;
    const a = anim.current;
    cancelAnimationFrame(a.raf);
    a.last = 0;
    a.raf = requestAnimationFrame(frame);
    return () => { cancelAnimationFrame(a.raf); a.raf = 0; };
  }, [open, frame]);
  useEffect(() => { if (!open) { anim.current.cur = null; anim.current.card = null; } }, [open]);

  const next = useCallback(() => (i >= all.length - 1 ? onClose(true) : setI(i + 1)), [i, all.length, onClose]);
  const back = useCallback(() => setI((x) => Math.max(0, x - 1)), []);
  useEffect(() => {
    if (!open) return;
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(false);
      else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); next(); }
      else if (e.key === "ArrowLeft") back();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [open, next, back, onClose]);

  if (!open) return null;
  const pct = ((i + 1) / all.length) * 100;
  return createPortal(
    <div className="tour-root" role="dialog" aria-modal="true" aria-label={`Tour: ${step.title}`}>
      <svg className="tour-scrim" width="100%" height="100%" aria-hidden>
        <defs>
          <mask id="tour-mask">
            <rect x="0" y="0" width="100%" height="100%" fill="white" />
            <rect ref={hole} fill="black" />
          </mask>
        </defs>
        <rect x="0" y="0" width="100%" height="100%" className="tour-shade" mask="url(#tour-mask)" />
        <rect ref={ring} className="tour-ring" fill="none" />
      </svg>
      <div ref={card} className="tour-card" aria-live="polite">
        <div key={step.id} className="tour-swap">
          <div className="tour-art">{step.art}</div>
          <div className="px-6 pb-2 pt-4">
            <p className="tour-eyebrow">{step.eyebrow}<span>{i + 1} / {all.length}</span></p>
            <h2 className="tour-title">{step.title}</h2>
            <p className="tour-body">{step.body}</p>
          </div>
        </div>
        <div className="tour-progress" aria-hidden><span style={{ width: `${pct}%` }} /></div>
        <footer className="flex items-center gap-2 px-6 pb-5 pt-3">
          <button onClick={() => onClose(false)} className="text-xs font-medium text-ink-3 hover:text-ink">{i === all.length - 1 ? "Close" : "Skip tour"}</button>
          <span className="ml-auto" />
          {i > 0 && <button onClick={back} className="tour-btn tour-btn-ghost" aria-label="Back"><ArrowLeft className="size-4" /></button>}
          <button onClick={next} className="tour-btn tour-btn-primary" autoFocus>
            {i === all.length - 1 ? "Start using Alert Bot" : i === 0 ? "Show me around" : "Next"}{i < all.length - 1 && <ArrowRight className="size-4" />}
          </button>
        </footer>
        <button onClick={() => onClose(false)} className="tour-close" aria-label="Close the tour"><X className="size-4" /></button>
      </div>
      <div className={cn("sr-only")}>Use the arrow keys to move through the tour, Escape to close it.</div>
    </div>,
    document.body,
  );
}
