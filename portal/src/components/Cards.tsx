// Page framing and data cards, ported from Team Tesseract: PageIntro (hero with sea-swell lines), ChartCard (title, actions,
// loading / error / empty states), Metric + MetricStrip (KPI tiles), Notice, Facts.
import { ArrowDownRight, ArrowUpRight, Info, TriangleAlert, type LucideIcon } from "lucide-react";
import { Fragment, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { EmptyState, ErrorState, Skeleton } from "./ui/primitives";

export function PageIntro({ children, actions, className, title, eyebrow }: {
  children?: ReactNode; actions?: ReactNode; className?: string; title: ReactNode; eyebrow?: ReactNode;
}) {
  return (
    <header className={cn("page-hero card-in relative mb-6 overflow-hidden rounded-[1.5rem] border border-line px-6 py-6 shadow-card sm:px-8", className)}>
      <HeroSwell />
      <div className="relative flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <div className="min-w-0 max-w-4xl">
          {eyebrow ? <p className="eyebrow mb-1.5 flex items-center gap-2 font-semibold text-accent">{eyebrow}</p> : null}
          <h2 className="display text-[1.6875rem] font-bold leading-[1.18] text-ink sm:text-[1.875rem]">{title}</h2>
          {children ? <div className="mt-2 flex flex-wrap items-center gap-y-1 text-[0.84375rem] leading-6 text-ink-2">{children}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}

/** Decorative sea-swell contour lines on the right of the hero (pure SVG, no interaction). */
function HeroSwell() {
  const lines = Array.from({ length: 9 }, (_, i) => i);
  return (
    <svg className="hero-swell pointer-events-none absolute -right-10 top-0 hidden h-full w-[35rem] text-accent md:block" viewBox="0 0 560 200" preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id="hero-fade" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="currentColor" stopOpacity="0" />
          <stop offset="0.55" stopColor="currentColor" stopOpacity="0.16" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0.28" />
        </linearGradient>
      </defs>
      {lines.map((i) => (
        <path key={i} d={`M0 ${40 + i * 16} C 90 ${20 + i * 16}, 170 ${70 + i * 15}, 260 ${46 + i * 16} S 430 ${20 + i * 17}, 560 ${38 + i * 15}`}
          fill="none" stroke="url(#hero-fade)" strokeWidth={i % 3 === 0 ? 1.4 : 0.8} />
      ))}
    </svg>
  );
}

/** Inline "a · b · c" context line; falsy parts are skipped. */
export function Facts({ items }: { items: ReactNode[] }) {
  const shown = items.filter((x) => x !== null && x !== undefined && x !== false && x !== "");
  return (
    <>
      {shown.map((x, i) => (
        <Fragment key={i}>
          {i ? <span className="mx-2 text-ink-3" aria-hidden>·</span> : null}
          <span className="whitespace-nowrap">{x}</span>
        </Fragment>
      ))}
    </>
  );
}
export const Em = ({ children, className }: { children: ReactNode; className?: string }) => <span className={cn("font-medium text-ink", className)}>{children}</span>;

export function Notice({ tone = "info", children, className, action }: { tone?: "info" | "warn"; children: ReactNode; className?: string; action?: ReactNode }) {
  const Icon = tone === "warn" ? TriangleAlert : Info;
  return (
    <div className={cn("flex items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-[0.8125rem] leading-5",
      tone === "warn" ? "border-warning/35 bg-warning-soft text-ink" : "border-line bg-surface-2/70 text-ink-2", className)} role={tone === "warn" ? "status" : undefined}>
      <Icon className={cn("mt-0.5 size-4 shrink-0", tone === "warn" ? "text-warning" : "text-ink-3")} aria-hidden />
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}

export function ChartCard({ title, subtitle, actions, loading, error, onRetry, empty, emptyText = "Nothing to show.", emptyIcon, emptyTone, skeleton, className, bodyClassName, children, index = 0, icon: Icon, variant = "default", flush, toolbar, footer }: {
  title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; loading?: boolean; error?: unknown; onRetry?: () => void; empty?: boolean;
  emptyText?: ReactNode; emptyIcon?: LucideIcon; emptyTone?: "good"; skeleton?: ReactNode; className?: string; bodyClassName?: string; children?: ReactNode;
  index?: number; icon?: LucideIcon; variant?: "default" | "hero"; flush?: boolean; toolbar?: ReactNode; footer?: ReactNode;
}) {
  return (
    <section style={{ animationDelay: `${Math.min(index, 6) * 30}ms` }}
      className={cn("card-in relative flex min-w-0 flex-col rounded-[1.25rem] border border-line shadow-card", variant === "hero" ? "surface-hero" : "bg-surface", flush && "card-flush", className)}>
      <header className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2.5 px-6 pb-3.5 pt-5">
        <div className="flex min-w-[min(100%,15rem)] flex-1 items-start gap-3">
          {Icon ? (
            <span className="flex size-8 shrink-0 items-center justify-center rounded-[0.625rem] bg-gradient-to-br from-accent-soft to-transparent shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--accent)_22%,transparent)]">
              <Icon className="size-4 text-accent" aria-hidden />
            </span>
          ) : null}
          <div className="min-w-0">
            <h2 className="display text-[0.96875rem] font-semibold leading-6 text-ink">{title}</h2>
            {subtitle ? <p className="truncate text-[0.78125rem] leading-5 text-ink-3">{subtitle}</p> : null}
          </div>
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">{actions}</div> : null}
      </header>
      {toolbar && !error && !loading ? <div className="border-y border-line px-6 py-2.5">{toolbar}</div> : null}
      <div className={cn("relative min-h-0 flex-1", flush && !error && !empty ? "pb-0" : "px-6 pb-6", toolbar && !flush && "pt-4", bodyClassName)}>
        {error ? <ErrorState error={error} onRetry={onRetry} />
          : loading ? <div className={cn(flush && "px-6 pb-6")} aria-busy>{skeleton ?? <Skeleton className="h-40 w-full" />}</div>
          : empty ? <EmptyState icon={emptyIcon} tone={emptyTone}>{emptyText}</EmptyState>
          : children}
      </div>
      {footer && !error && !loading && !empty ? <div className="border-t border-line px-6 py-3">{footer}</div> : null}
    </section>
  );
}

/** Delta pill: tone follows the verdict (+1 better, −1 worse, 0 neutral), never the sign alone. */
export function Delta({ text, verdict, up }: { text: ReactNode; verdict: number; up: boolean }) {
  const Icon = up ? ArrowUpRight : ArrowDownRight;
  return (
    <span className={cn("num inline-flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-px text-[0.6875rem] font-medium",
      verdict > 0 ? "bg-good-soft text-good" : verdict < 0 ? "bg-critical-soft text-critical" : "bg-surface-3 text-ink-2")}>
      <Icon className="size-3" aria-hidden />
      {text}
    </span>
  );
}

export function Metric({ label, value, corner, footnote, chart, size = "sm", icon: Icon, iconColor, onClick, pressed, className }: {
  label: ReactNode; value: ReactNode; corner?: ReactNode; footnote?: ReactNode; chart?: ReactNode; size?: "lg" | "sm";
  icon?: LucideIcon; iconColor?: string; onClick?: () => void; pressed?: boolean; className?: string;
}) {
  const lg = size === "lg";
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <p className={cn("flex min-w-0 items-center gap-1.5 truncate font-medium text-ink-3", lg ? "text-[0.75rem]" : "text-[0.6875rem]")}>
          {Icon ? <Icon className="size-3.5 shrink-0" style={iconColor ? { color: iconColor } : undefined} aria-hidden /> : null}
          <span className="truncate">{label}</span>
        </p>
        {corner}
      </div>
      <div className={cn("kpi text-ink", lg ? "mt-3 text-[2rem]" : "mt-1.5 text-[1.25rem]")}>{value}</div>
      {footnote ? <div className={cn("min-h-4 truncate text-ink-3", lg ? "mt-2 text-xs" : "mt-1 text-[0.6875rem]")}>{footnote}</div> : null}
      {chart ? <div className={cn(lg ? "mt-3" : "mt-2")}>{chart}</div> : null}
    </>
  );
  const cls = cn("group/metric relative flex min-w-0 flex-col text-left", lg ? "px-6 py-5" : "px-4 py-3.5");
  return onClick ? (
    <button type="button" onClick={onClick} aria-pressed={pressed} className={cn(cls, "transition-colors hover:bg-row-hover", pressed && "bg-row-hover shadow-[inset_0_-2px_0_var(--accent)]", className)}>{body}</button>
  ) : (
    <div className={cn(cls, className)}>{body}</div>
  );
}

/** A card-like strip of metrics separated by hairlines. */
export function MetricStrip({ children, className, cols = "grid-cols-2 lg:grid-cols-4" }: { children: ReactNode; className?: string; cols?: string }) {
  return (
    <div className={cn("card-in grid overflow-hidden rounded-2xl border border-line bg-surface shadow-card [&>*]:border-line max-lg:[&>*:nth-child(n+3)]:border-t lg:[&>*+*]:border-l max-lg:[&>*:nth-child(even)]:border-l", cols, className)}>
      {children}
    </div>
  );
}
