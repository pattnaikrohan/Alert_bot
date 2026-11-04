import { BellOff, BellRing, ChevronRight, Mail, MessagesSquare, Send, Webhook } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { AdaptiveCardPreview } from "@/components/AdaptiveCardPreview";
import { ChartCard, Em, Metric, MetricStrip, Notice, PageIntro } from "@/components/Cards";
import { StatusChip } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Input, Select, Skeleton } from "@/components/ui/primitives";
import { del, post } from "@/lib/api";
import { ago, dateTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { AlertRow, MuteRow, Notifier } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useProject } from "@/app/Layout";

export function Alerts() {
  const { key = "" } = useParams();
  const owner = useProject(key)?.role === "owner";
  const { data, error, reload } = useApi<{ notifier: Notifier; alerts: AlertRow[] }>(`/api/projects/${key}/alerts`, { refreshMs: 30000 });
  const mutes = useApi<MuteRow[]>(`/api/projects/${key}/mutes`);
  const [open, setOpen] = useState<number | null>(null);
  const [hours, setHours] = useState(4);
  const [reason, setReason] = useState("");
  const active = (mutes.data ?? []).filter((m) => m.active);
  const by = (s: string) => (data?.alerts ?? []).filter((a) => a.status === s).length;

  const mute = async () => {
    try {
      mutes.setData(await post<MuteRow[]>(`/api/projects/${key}/mutes`, { scope: "*", hours, reason }));
      setReason("");
      toast.success(`Alerts muted for ${hours} h`, { description: "Checks keep running and incidents are still recorded." });
    } catch (e) {
      toast.error("Couldn't mute", { description: (e as Error).message });
    }
  };
  const unmute = async (id: number) => {
    mutes.setData(await del<MuteRow[]>(`/api/projects/${key}/mutes/${id}`));
    toast.success("Mute ended");
  };

  return (
    <div>
      <PageIntro eyebrow={<><BellRing className="size-3.5" /> Alerts</>} title={data?.notifier.mode === "live" ? "Alerts are live" : "Alerts are in dry-run"}>
        The bot only alerts when an incident opens, gets worse or resolves. Everything it sends, or would send, is listed here with the exact Teams card.
      </PageIntro>

      <MetricStrip className="mb-4" cols="grid-cols-2 lg:grid-cols-4">
        <Metric icon={Send} iconColor="var(--good)" label="Sent" value={data ? by("sent") : "–"} footnote="delivered to the flow" />
        <Metric icon={Webhook} iconColor="var(--warning)" label="Dry run" value={data ? by("dry_run") : "–"} footnote="built, not sent (no flow URL)" />
        <Metric icon={BellOff} label="Muted" value={data ? by("muted") : "–"} footnote="suppressed by an owner" />
        <Metric icon={BellRing} iconColor={by("failed") ? "var(--critical)" : undefined} label="Failed" value={data ? by("failed") : "–"} footnote="gave up after retries" />
      </MetricStrip>

      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 xl:col-span-8">
          <ChartCard flush icon={BellRing} title="Alert log" subtitle={data ? `${data.alerts.length} alert${data.alerts.length === 1 ? "" : "s"} · newest first` : undefined}
            loading={!data && !error} error={error} onRetry={reload} empty={!!data && !data.alerts.length} emptyText="No alerts yet. They appear as incidents open and resolve."
            skeleton={<div className="space-y-2">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>}>
            <ul className="pb-1">
              {data?.alerts.map((a) => (
                <li key={a.id} className="border-t border-line first:border-t-0">
                  <button onClick={() => setOpen(open === a.id ? null : a.id)} aria-expanded={open === a.id} className="flex w-full items-center gap-3 px-6 py-3 text-left transition-colors hover:bg-row-hover">
                    <ChevronRight className={cn("size-3.5 shrink-0 text-ink-3 transition-transform", open === a.id && "rotate-90")} aria-hidden />
                    <StatusChip status={a.status} />
                    <span className="min-w-0 flex-1 truncate text-sm"><span className="font-medium text-ink">{a.title}</span> <span className="text-ink-3">· {a.event.replace("incident.", "")}</span></span>
                    <span className="num shrink-0 text-[0.6875rem] text-ink-3">{ago(a.created_at)}</span>
                  </button>
                  {open === a.id ? (
                    <div className="grid gap-4 px-6 pb-5 lg:grid-cols-2">
                      {a.payload.card ? <AdaptiveCardPreview card={a.payload.card} /> : null}
                      <div className="min-w-0 space-y-2">
                        {a.incident_id ? <Link to={`/p/${key}/i/${a.incident_id}`} className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline">Open incident #{a.incident_id} <ChevronRight className="size-3" /></Link> : null}
                        {a.last_error ? <Notice tone="warn">{a.last_error}</Notice> : null}
                        <p className="eyebrow text-ink-3">JSON sent to Power Automate</p>
                        <pre className="scrollbar-thin max-h-72 overflow-auto rounded-xl bg-surface-2 p-3 font-mono text-[0.6875rem] leading-5 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">{JSON.stringify({ ...a.payload, card: "…" }, null, 2)}</pre>
                      </div>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </ChartCard>
        </div>

        <div className="col-span-12 space-y-4 xl:col-span-4">
          <ChartCard icon={MessagesSquare} title="Delivery" subtitle="Where alerts go">
            <div className="space-y-3 text-[0.8125rem]">
              <div className="flex items-center gap-2.5"><MessagesSquare className="size-4 text-accent" /><span><Em>{data?.notifier.teams_channel || "No channel"}</Em><span className="block text-[0.6875rem] text-ink-3">Teams card for every alert</span></span></div>
              <div className="flex items-center gap-2.5"><Mail className="size-4 text-accent" /><span><Em>Owners by email</Em><span className="block text-[0.6875rem] text-ink-3">on {data?.notifier.email_owners_on.join(", ") || "never"} incidents</span></span></div>
            </div>
            {data && data.notifier.mode !== "live" ? (
              <Notice tone="warn" className="mt-4">
                <Em>No flow URL yet.</Em>
                <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-ink-2">
                  <li>Create a flow that starts with <i>When an HTTP request is received</i>.</li>
                  <li>Put its URL in <code className="font-mono">.env</code> as <code className="font-mono">{data.notifier.flow_url_ref?.replace("env://", "") ?? "flow_url"}</code> (Key Vault in production).</li>
                  <li>Run <code className="font-mono">alertbot notify-test {key}</code>.</li>
                </ol>
              </Notice>
            ) : null}
          </ChartCard>

          <ChartCard icon={BellOff} title="Mute alerts" subtitle={active.length ? `${active.length} active` : "Pause Teams and email for planned work"}>
            <div className="space-y-3">
              {active.map((m) => (
                <div key={m.id} className="flex items-center justify-between gap-2 rounded-xl bg-surface-2 px-3 py-2 shadow-[inset_0_0_0_1px_var(--line)]">
                  <span className="min-w-0 text-[0.8125rem]"><Em>Until {dateTime(m.until)}</Em><span className="block truncate text-[0.6875rem] text-ink-3">{m.reason || "no reason given"} · {m.created_by}</span></span>
                  {owner ? <Button size="sm" variant="outline" onClick={() => unmute(m.id)}>End</Button> : null}
                </div>
              ))}
              {owner ? (
                <>
                  <div className="flex gap-2">
                    <Select value={hours} onChange={(e) => setHours(Number(e.target.value))} aria-label="Mute duration" className="w-32">
                      {[1, 4, 12, 24, 72].map((h) => <option key={h} value={h}>{h < 24 ? `${h} hours` : `${h / 24} day${h > 24 ? "s" : ""}`}</option>)}
                    </Select>
                    <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason, e.g. maintenance" className="min-w-0 flex-1" />
                  </div>
                  <Button className="w-full" onClick={mute}><BellOff /> Mute alerts</Button>
                  <p className="text-[0.6875rem] text-ink-3">Checks keep running and incidents are still recorded while muted.</p>
                </>
              ) : <p className="text-[0.8125rem] text-ink-3">Only project owners can mute alerts.</p>}
            </div>
          </ChartCard>
        </div>
      </div>
    </div>
  );
}
