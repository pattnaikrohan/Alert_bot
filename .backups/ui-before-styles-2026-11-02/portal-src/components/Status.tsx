// Status vocabulary: severity, project health, incident / run / delivery state. Status colours are fixed tokens and always
// paired with an icon + label (never colour alone), as in Team Tesseract's StatusPill.
import { Ban, CircleCheck, CircleDashed, Clock, Info, Loader, OctagonAlert, Send, TriangleAlert, VolumeX, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface Meta { label: string; color: string; soft: string; icon: LucideIcon }
export const SEVERITY_META: Record<string, Meta> = {
  critical: { label: "Critical", color: "var(--critical)", soft: "var(--critical-soft)", icon: OctagonAlert },
  warning: { label: "Warning", color: "var(--warning)", soft: "var(--warning-soft)", icon: TriangleAlert },
  info: { label: "Info", color: "var(--overmax)", soft: "var(--overmax-soft)", icon: Info },
  ok: { label: "Healthy", color: "var(--good)", soft: "var(--good-soft)", icon: CircleCheck },
  unknown: { label: "No data", color: "var(--nocons)", soft: "var(--nocons-soft)", icon: CircleDashed },
};

const ring = (c: string) => `inset 0 0 0 1px color-mix(in srgb, ${c} 45%, transparent)`;

export function SeverityPill({ severity, muted, label, className }: { severity: string; muted?: boolean; label?: string; className?: string }) {
  const m = SEVERITY_META[severity] ?? SEVERITY_META.unknown!;
  const Icon = m.icon;
  return (
    <span className={cn("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[0.6875rem] font-semibold text-ink", muted && "opacity-60", className)}
      style={{ background: m.soft, boxShadow: ring(m.color) }}>
      <Icon className="size-3" style={{ color: m.color }} aria-hidden />
      {label ?? m.label}
    </span>
  );
}

const STATUS: Record<string, { label: string; color: string; icon?: LucideIcon }> = {
  open: { label: "Open", color: "var(--critical)" },
  acknowledged: { label: "Acknowledged", color: "var(--cat-1)" },
  resolved: { label: "Resolved", color: "var(--good)" },
  Succeeded: { label: "Succeeded", color: "var(--good)" },
  succeeded: { label: "Succeeded", color: "var(--good)" },
  Failed: { label: "Failed", color: "var(--critical)" },
  failed: { label: "Failed", color: "var(--critical)" },
  Cancelled: { label: "Cancelled", color: "var(--nocons)" },
  InProgress: { label: "Running", color: "var(--accent)", icon: Loader },
  running: { label: "Running", color: "var(--accent)", icon: Loader },
  Queued: { label: "Queued", color: "var(--cat-4)", icon: Clock },
  NoData: { label: "No new rows", color: "var(--nocons)", icon: CircleDashed },
  pending: { label: "Pending", color: "var(--warning)", icon: Clock },
  sent: { label: "Sent", color: "var(--good)", icon: Send },
  dry_run: { label: "Not sent", color: "var(--warning)" },
  muted: { label: "Muted", color: "var(--nocons)", icon: VolumeX },
  unknown: { label: "Unknown", color: "var(--nocons)", icon: Ban },
};

/** Generic status chip: a coloured dot with a soft halo on a neutral pill. */
export function StatusChip({ status, className }: { status: string | null | undefined; className?: string }) {
  const s = STATUS[status ?? ""] ?? { label: status ?? "—", color: "var(--line-strong)" };
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-surface-2 px-2 py-0.5 text-[0.6875rem] font-medium text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]", className)}>
      {s.icon ? <s.icon className={cn("size-3", s.icon === Loader && "animate-spin")} style={{ color: s.color }} aria-hidden />
        : <span className="size-1.5 rounded-full" style={{ background: s.color, boxShadow: `0 0 0 2px color-mix(in srgb, ${s.color} 22%, transparent)` }} aria-hidden />}
      {s.label}
    </span>
  );
}

export function StatusDot({ status, className }: { status: string; className?: string }) {
  const m = SEVERITY_META[status] ?? SEVERITY_META.unknown!;
  return <span className={cn("size-2 shrink-0 rounded-full", className)} style={{ background: m.color, boxShadow: `0 0 0 3px color-mix(in srgb, ${m.color} 22%, transparent)` }} aria-hidden />;
}
