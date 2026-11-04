import { AlertCircle, Inbox, RotateCw, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Button } from "./button";

/** Loading placeholder shaped like the final content; `.skeleton` adds a compositor-only shimmer. */
export const Skeleton = ({ className }: { className?: string }) => <div aria-hidden className={cn("skeleton rounded-lg bg-surface-3", className)} />;

export const Kbd = ({ children, className }: { children: ReactNode; className?: string }) => (
  <kbd className={cn("num inline-flex h-5 min-w-5 items-center justify-center rounded border border-line-strong bg-surface-2 px-1 text-[0.625rem] font-medium text-ink-2", className)}>{children}</kbd>
);

/** Empty state: an icon in a soft disc, one sentence, one action. */
export function EmptyState({ icon: Icon = Inbox, children, action, className, tone }: { icon?: LucideIcon; children: ReactNode; action?: ReactNode; className?: string; tone?: "good" }) {
  return (
    <div className={cn("flex min-h-36 flex-col items-center justify-center gap-3 px-4 py-8 text-center", className)}>
      <span className={cn("flex size-10 items-center justify-center rounded-full", tone === "good" ? "bg-good-soft" : "bg-surface-2 shadow-[inset_0_0_0_1px_var(--line)]")}>
        <Icon className={cn("size-[1.125rem]", tone === "good" ? "text-good" : "text-ink-3")} aria-hidden />
      </span>
      <p className="max-w-sm text-sm text-ink-2">{children}</p>
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry, what = "this", className }: { error: unknown; onRetry?: () => void; what?: string; className?: string }) {
  return (
    <div role="alert" className={cn("flex min-h-36 flex-col items-center justify-center gap-3 px-4 py-8 text-center", className)}>
      <span className="flex size-10 items-center justify-center rounded-full bg-critical-soft">
        <AlertCircle className="size-[1.125rem] text-critical" aria-hidden />
      </span>
      <div>
        <p className="text-sm text-ink">Couldn't load {what}.</p>
        {(error as Error | null)?.message ? <p className="mt-1 max-w-sm text-xs text-ink-3">{(error as Error).message}</p> : null}
      </div>
      {onRetry ? (
        <Button size="sm" onClick={onRetry}>
          <RotateCw /> Retry
        </Button>
      ) : null}
    </div>
  );
}

/** Pill segmented control (radiogroup): the active segment is a raised chip on a recessed track. */
export function Segmented<T extends string | number>({ value, onChange, options, label, size = "md" }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode; hint?: string }[]; label: string; size?: "sm" | "md";
}) {
  return (
    <div role="radiogroup" aria-label={label} className="scrollbar-thin inline-flex max-w-full items-center overflow-x-auto rounded-lg bg-surface-2 p-0.5 shadow-[inset_0_0_0_1px_var(--line)]">
      {options.map((o) => {
        const on = value === o.value;
        return (
          <button key={String(o.value)} type="button" role="radio" aria-checked={on} title={o.hint} onClick={() => onChange(o.value)}
            className={cn("tnum shrink-0 whitespace-nowrap rounded-md font-medium transition-[background-color,color,box-shadow] duration-150",
              size === "sm" ? "px-2 py-0.5 text-[0.6875rem]" : "px-2.5 py-1 text-xs",
              on ? "bg-surface-3 text-ink shadow-[0_1px_2px_rgb(0_0_0/0.18),inset_0_0_0_1px_var(--line-strong)] light:bg-surface" : "text-ink-3 hover:text-ink")}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

const field = "h-9 rounded-lg border border-line-strong bg-surface-2 px-2.5 text-sm text-ink shadow-soft transition-[border-color,background-color] placeholder:text-ink-3 hover:border-[color-mix(in_srgb,var(--ink-3)_45%,transparent)] focus-visible:border-accent/60 focus-visible:outline-2 focus-visible:outline-focus light:bg-surface";
export const Select = ({ className, ...p }: React.SelectHTMLAttributes<HTMLSelectElement>) => <select className={cn(field, "select-chevron pr-8", className)} {...p} />;
export const Input = ({ className, ...p }: React.InputHTMLAttributes<HTMLInputElement>) => <input className={cn(field, className)} {...p} />;
