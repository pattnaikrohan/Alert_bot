// "What can be done": the safe options for an incident, the recommended one first. The bot is read-only: options that
// change the factory open Data Factory Studio for a person to act; the bot only does its own things (mute, accept, re-check).
import { BellOff, Check, ExternalLink, GraduationCap, Hand, RefreshCw, ShieldCheck, Wrench } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ChartCard } from "@/components/Cards";
import { Button } from "@/components/ui/button";
import { post } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface RemediationOption { key: string; label: string; what: string; href: string | null; action: string | null; changes_factory: boolean }
export interface RemediationPlan { mode: "read_only"; recommended: string; why: string; options: RemediationOption[] }

const ICON: Record<string, typeof Wrench> = { acknowledge: Hand, mute: BellOff, accept_schema: ShieldCheck, recheck: RefreshCw, accept_normal: GraduationCap };
const BUTTON: Record<string, string> = { accept_normal: "Accept" };

export function Remediation({ plan, projectKey, incidentId, scope, owner, acknowledged, onAcknowledge, onDone }: {
  plan: RemediationPlan; projectKey: string; incidentId: number; scope: string; owner: boolean; acknowledged: boolean;
  onAcknowledge: () => Promise<void> | void; onDone: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const table = scope.startsWith("data:") ? scope.slice(5) : null;

  const run = async (o: RemediationOption) => {
    setBusy(o.key);
    try {
      if (o.action === "acknowledge") await onAcknowledge();
      else if (o.action === "mute") {
        await post(`/api/projects/${projectKey}/mutes`, { scope, hours: 24, reason: "Ignored for a day from the incident page" });
        toast.success("Muted for 24 hours", { description: "No alerts for this incident until tomorrow; it stays visible here." });
      } else if (o.action === "accept_schema" && table) {
        await post(`/api/projects/${projectKey}/quality/${encodeURIComponent(table)}/accept-schema`);
        toast.success("New schema accepted", { description: "The incident resolves on the next data check." });
      } else if (o.action === "accept_normal") {
        await post(`/api/projects/${projectKey}/incidents/${incidentId}/accept-normal`);
        toast.success("Accepted as the new normal", { description: "The bot now learns from these runs onwards; the incident resolves once the latest values fit." });
      } else if (o.action === "recheck") {
        const r = await post<{ failed_checks: number }>(`/api/projects/${projectKey}/quality/check`);
        toast.success("Data checked again", { description: r.failed_checks ? `${r.failed_checks} checks still failing.` : "Everything passes now." });
      }
      onDone();
    } catch (e) {
      toast.error(`Couldn't ${o.label.toLowerCase()}`, { description: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };

  const ordered = [...plan.options].sort((a, b) => (a.key === plan.recommended ? -1 : b.key === plan.recommended ? 1 : 0));
  return (
    <ChartCard icon={Wrench} title="What can be done" subtitle="Read-only bot: factory changes open Data Factory for you to do">
      <p className="mb-3 rounded-xl bg-accent-soft/50 px-3 py-2 text-[0.8125rem] leading-5 text-ink shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--accent)_22%,transparent)]">
        <b>Recommended:</b> {plan.options.find((o) => o.key === plan.recommended)?.label}. {plan.why}
      </p>
      <ul className="space-y-2">
        {ordered.map((o) => {
          const Icon = o.href ? ExternalLink : ICON[o.action ?? ""] ?? Check;
          const ownerOnly = o.action === "mute" || o.action === "accept_schema" || o.action === "recheck";
          const disabled = busy != null || (ownerOnly && !owner) || (o.action === "acknowledge" && acknowledged);
          return (
            <li key={o.key} className={cn("rounded-xl p-3 shadow-[inset_0_0_0_1px_var(--line)]", o.key === plan.recommended ? "bg-surface-2" : "")}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-ink">{o.label}{o.key === plan.recommended && <span className="ml-2 rounded-full bg-accent px-1.5 py-px text-[0.625rem] font-bold uppercase tracking-wide text-accent-ink">Recommended</span>}</p>
                  <p className="mt-0.5 text-xs leading-5 text-ink-2">{o.what}</p>
                </div>
                {o.href ? (
                  <Button size="sm" variant="secondary" asChild><a href={o.href} target="_blank" rel="noreferrer"><Icon />Open in ADF</a></Button>
                ) : (
                  <Button size="sm" variant={o.key === plan.recommended ? "primary" : "secondary"} disabled={disabled} onClick={() => run(o)}
                    title={ownerOnly && !owner ? "Project owners only" : undefined}>
                    <Icon />{o.action === "acknowledge" && acknowledged ? "Acknowledged" : busy === o.key ? "Working…" : BUTTON[o.action ?? ""] ?? o.label.split(" ")[0]}
                  </Button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </ChartCard>
  );
}
