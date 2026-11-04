// A faithful-enough render of the Adaptive Card the bot builds, so people can see exactly what Teams will show
// before the Power Automate flow URL is configured.
import { ExternalLink, MessagesSquare } from "lucide-react";
import { cn } from "@/lib/utils";

type El = Record<string, any>;
const HEAD: Record<string, string> = {
  attention: "bg-critical-soft shadow-[inset_0_-1px_0_color-mix(in_srgb,var(--critical)_30%,transparent)]",
  warning: "bg-warning-soft",
  good: "bg-good-soft",
  accent: "bg-accent-soft",
};

function Block({ el }: { el: El }) {
  if (el.type === "TextBlock")
    return (
      <p className={cn("leading-snug", el.size === "Large" ? "display text-[1rem]" : el.size === "Small" ? "text-[0.6875rem]" : "text-[0.8125rem]",
        el.weight === "Bolder" ? "font-semibold" : "", el.isSubtle ? "text-ink-3" : "text-ink", el.spacing === "Medium" && "mt-2")}>
        {el.text}
      </p>
    );
  if (el.type === "FactSet")
    return (
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[0.75rem]">
        {el.facts.map((f: { title: string; value: string }) => (
          <div key={f.title} className="contents"><dt className="font-semibold text-ink">{f.title}</dt><dd className="text-ink-2">{f.value}</dd></div>
        ))}
      </dl>
    );
  return null;
}

export function AdaptiveCardPreview({ card }: { card: El }) {
  const [head, ...rest] = card.body as El[];
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[0.6875rem] text-ink-3"><MessagesSquare className="size-3.5" /> Preview of the Teams card</div>
      <div className="overflow-hidden rounded-xl border border-line-strong bg-surface shadow-soft">
        {head?.type === "Container" ? <div className={cn("space-y-0.5 px-3.5 py-3", HEAD[head.style] ?? "")}>{head.items.map((el: El, i: number) => <Block key={i} el={el} />)}</div> : null}
        <div className="space-y-1.5 px-3.5 py-3">{(head?.type === "Container" ? rest : card.body).map((el: El, i: number) => <Block key={i} el={el} />)}</div>
        <div className="px-3.5 pb-3.5">
          {(card.actions ?? []).map((a: El) => (
            <a key={a.title} href={a.url} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium text-ink shadow-[inset_0_0_0_1px_var(--line-strong)] hover:bg-surface-3">
              {a.title} <ExternalLink className="size-3" />
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
