// What you can say to the bot: three steps on how to use it, and five groups of examples built from the person's own
// incidents, pipelines and projects. Shared by the Home page's invitation and the bot panel's empty chat.
import {
  ArrowUpRight, BellRing, Compass, Hand, MessageSquareText, MousePointerClick, SlidersHorizontal, Sparkles, type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { shortName } from "@/app/projects";
import { useShell } from "@/app/shell";
import { useAppearance } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { UI_EVENT } from "./useAssistant";

/** How to use the app, through the bot: three short steps. */
export const HOW: [LucideIcon, string, string][] = [
  [MessageSquareText, "Ask", "in plain words, or speak"],
  [MousePointerClick, "Confirm", "nothing changes until you do"],
  [Compass, "Go anywhere", "any page, project or setting"],
];

export type Example = { text: string; note?: string };
export type ExampleGroup = { icon: LucideIcon; title: string; hint: string; asks?: boolean; items: Example[] };

function examples({ worst, pipeline, otherProject, theme, layout }: {
  worst?: { id: number; title: string }; pipeline?: string; otherProject?: string; theme: string; layout: string;
}): ExampleGroup[] {
  return [
    { icon: Sparkles, title: "Find out", hint: "Answers from live data", items: [
      { text: "What's wrong right now?" },
      worst ? { text: `Why did #${worst.id} happen?`, note: worst.title } : { text: "Is today's data ready?" },
      pipeline ? { text: `How is ${pipeline} doing?` } : { text: "Which pipelines failed this week?" },
    ] },
    { icon: Hand, title: "Handle problems", hint: "Asks you to confirm", asks: true, items: [
      worst ? { text: `Acknowledge #${worst.id}` } : { text: "Acknowledge the worst one" },
      worst ? { text: `How do I fix #${worst.id}?` } : { text: "Ack everything critical" },
      { text: "Run the checks now" },
    ] },
    { icon: BellRing, title: "Teams & alerts", hint: "Asks you to confirm", asks: true, items: [
      pipeline ? { text: `Mute ${pipeline} for 2 hours` } : { text: "Mute everything for 1 hour" },
      { text: "Send the morning report" },
      { text: "Unmute everything" },
    ] },
    { icon: Compass, title: "Go anywhere", hint: "Opens straight away", items: [
      { text: "Take me to compute cost" },
      { text: "Open the pipeline report" },
      otherProject ? { text: `Switch to ${otherProject}` } : { text: "Show me lineage" },
    ] },
    { icon: SlidersHorizontal, title: "Change the app", hint: "Instant, just for you", items: [
      { text: theme === "dark" ? "Light mode" : "Dark mode" },
      { text: layout === "side" ? "Put the menu back in the dock" : "Move the menu to a side panel" },
      { text: "Start the tour" },
    ] },
  ];
}

/** The examples for this person right now: their worst incident, a pipeline worth asking about, another project. */
export function useExampleGroups(): ExampleGroup[] {
  const { me, feed, project, pipelineItems } = useShell();
  const { theme } = useAppearance();
  // the menu's place, kept current when the bot moves it, so the example offers the other one
  const [layout, setLayout] = useState<string>(me.prefs?.nav_layout ?? "dock");
  useEffect(() => {
    const on = (e: Event) => { const d = (e as CustomEvent<{ do: string; value: string }>).detail; if (d?.do === "layout") setLayout(d.value); };
    window.addEventListener(UI_EVENT, on);
    return () => window.removeEventListener(UI_EVENT, on);
  }, []);
  const active = feed?.active ?? [];
  const worst = active.find((a) => a.status === "open") ?? active[0];
  const pipeline = pipelineItems.find((x) => x.state === "failing")?.name ?? pipelineItems[0]?.name;
  const other = me.projects.find((p) => p.key !== project?.key);
  return examples({ worst: worst ? { id: worst.id, title: worst.title } : undefined, pipeline, otherProject: other ? shortName(other) : undefined, theme, layout });
}

/** The five groups as small cards. One click on an example says it to the bot. */
export function ExampleCards({ groups, onSay, title, subtitle, steps }: {
  groups: ExampleGroup[]; onSay: (text: string) => void; title?: string; subtitle?: string; steps?: boolean;
}) {
  return (
    <div className="home-empty">
      {title && <p className="display text-center text-[1.0625rem] font-semibold text-ink">{title}</p>}
      {subtitle && <p className="mt-1 text-center text-[0.8125rem] text-ink-2">{subtitle}</p>}
      {steps && <HowSteps className="mt-3 justify-center !pl-0" />}
      <div className="home-cards">
        {groups.map((g) => (
          <section key={g.title} className="home-card" aria-label={g.title}>
            <div className="flex items-center gap-2">
              <span className="home-card-icon"><g.icon className="size-4" aria-hidden /></span>
              <span className="min-w-0">
                <span className="block text-[0.8125rem] font-semibold leading-tight text-ink">{g.title}</span>
                <span className={cn("block text-[0.65625rem] font-medium leading-tight", g.asks ? "text-[var(--warning-ink)]" : "text-ink-3")}>{g.hint}</span>
              </span>
            </div>
            <ul className="mt-2.5 space-y-0.5">
              {g.items.map((x) => (
                <li key={x.text}>
                  <button type="button" className="home-say" onClick={() => onSay(x.text)} title={x.note} aria-label={`Say to the bot: ${x.text}`}>
                    <span className="min-w-0 flex-1 truncate">{x.text}</span><ArrowUpRight className="home-say-go" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}

/** Once a conversation is going: the same examples, in the chat's row of things to say next. */
export function ExampleStrip({ groups, onSay }: { groups: ExampleGroup[]; onSay: (text: string) => void }) {
  return (
    <>
      {groups.flatMap((g) => g.items.map((x) => (
        <button key={`${g.title}-${x.text}`} type="button" onClick={() => onSay(x.text)} title={x.note}>{x.text}</button>
      )))}
    </>
  );
}

/** The three steps, as a row of pills with arrows between them. */
export function HowSteps({ className }: { className?: string }) {
  return (
    <ol className={cn("home-steps", className)} aria-label="How to use it">
      {HOW.map(([Icon, title, text], i) => (
        <li key={title}>
          {i > 0 && <span className="home-step-sep" aria-hidden>›</span>}
          <span className="home-step">
            <Icon className="size-3.5 shrink-0 text-accent" aria-hidden />
            <span><b className="font-semibold text-ink">{i + 1}. {title}</b> <span className="text-ink-3">{text}</span></span>
          </span>
        </li>
      ))}
    </ol>
  );
}
