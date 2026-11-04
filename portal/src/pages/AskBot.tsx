// Ask the bot: a conversation that can also do things. Ask a question, or say what to do ("ack #12", "resolve the pl_sql
// one, re-ran it", "mute pl_uat for 2 hours", "send the morning report"); each change is confirmed before it runs.
import { BellRing, CircleCheck, Hand, MessageSquareText, MessageSquarePlus, Play, ShieldCheck, Sparkles } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useShell } from "@/app/Layout";
import { AssistantChat } from "@/components/assistant/Assistant";
import { PageIntro } from "@/components/Cards";
import { SeverityPill } from "@/components/Status";

const SAY = [
  { icon: Sparkles, head: "Ask", lines: ["What's wrong right now?", "Why did pl_sql fail?", "Is today's data ready?"] },
  { icon: Hand, head: "Acknowledge & resolve", lines: ["Ack #12", "Ack everything critical", "Resolve the pl_sql one, re-ran it"] },
  { icon: MessageSquarePlus, head: "Comment & learn", lines: ["Note on #12: waiting for the vendor", "Accept #5 as the new normal"] },
  { icon: BellRing, head: "Teams", lines: ["Send #12 to Teams", "Send the morning report", "Mute pl_uat for 2 hours", "Unmute"] },
  { icon: Play, head: "Run & go", lines: ["Run the checks", "Refresh everything", "Take me to lineage", "Open #12"] },
];

export function AskBot() {
  const { key = "" } = useParams();
  const { feed } = useShell();
  const needs = (feed?.active ?? []).filter((i) => i.status === "open");
  const held = (feed?.active ?? []).filter((i) => i.status === "acknowledged");
  const fill = (t: string) => {
    const box = document.querySelector<HTMLTextAreaElement>(".as-input textarea");
    if (!box) return;
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    set.call(box, t);
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.focus();
  };

  return (
    <div>
      <PageIntro eyebrow={<><MessageSquareText className="size-3.5" /> Ask the bot</>} title="Your assistant for the whole app">
        Ask anything about your pipelines, or tell it what to do: acknowledge, resolve, comment, mute, send alerts and the morning report, run checks,
        go anywhere. It answers from what the bot has checked, and every change waits for your confirm.
      </PageIntro>
      <div className="grid grid-cols-12 gap-4">
        <section className="col-span-12 overflow-hidden rounded-[1.25rem] border border-line bg-surface shadow-card xl:col-span-8">
          <AssistantChat project={key} className="h-[min(68vh,44rem)] min-h-[28rem]" />
        </section>
        <aside className="col-span-12 space-y-4 xl:col-span-4">
          <section className="rounded-[1.25rem] border border-line bg-surface p-4 shadow-card">
            <p className="flex items-center justify-between text-[0.8125rem] font-semibold text-ink">
              Needs attention now <span className="text-[0.6875rem] font-normal text-ink-3">{needs.length} open · {held.length} being handled</span>
            </p>
            {needs.length === 0 ? <p className="mt-2 flex items-center gap-1.5 text-[0.8125rem] text-ink-2"><CircleCheck className="size-4 text-[var(--good)]" />Nothing needs you right now.</p> : (
              <ul className="mt-2 space-y-2">
                {needs.slice(0, 5).map((i) => (
                  <li key={`${i.project}-${i.id}`} className="rounded-xl bg-surface-2 p-2.5 shadow-[inset_0_0_0_1px_var(--line)]">
                    <Link to={`/p/${i.project}/i/${i.id}`} className="flex items-start gap-2 hover:underline">
                      <SeverityPill severity={i.severity} />
                      <span className="min-w-0 text-[0.8125rem] font-medium leading-5 text-ink [overflow-wrap:anywhere]">#{i.id} {i.title}</span>
                    </Link>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {[["Ack", `Acknowledge #${i.id}`, Hand], ["Resolve…", `Resolve #${i.id}: `, CircleCheck], ["Teams", `Send #${i.id} to Teams`, BellRing], ["Why?", `Why did #${i.id} happen?`, Sparkles]]
                        .map(([label, cmd, Icon]) => {
                          const I = Icon as typeof Hand;
                          return (
                            <button key={label as string} type="button" onClick={() => fill(cmd as string)} className="as-quick"><I className="size-3" aria-hidden />{label as string}</button>
                          );
                        })}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="rounded-[1.25rem] border border-line bg-surface p-4 shadow-card">
            <p className="text-[0.8125rem] font-semibold text-ink">Things you can say</p>
            <div className="mt-2 space-y-3">
              {SAY.map((g) => (
                <div key={g.head}>
                  <p className="flex items-center gap-1.5 text-[0.6875rem] font-semibold uppercase tracking-[0.06em] text-ink-3"><g.icon className="size-3.5 text-accent" />{g.head}</p>
                  <div className="mt-1 flex flex-wrap gap-1.5">{g.lines.map((l) => <button key={l} type="button" onClick={() => fill(l)} className="as-quick">{l}</button>)}</div>
                </div>
              ))}
            </div>
          </section>
          <p className="flex items-start gap-2 px-1 text-[0.71875rem] leading-5 text-ink-3">
            <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-accent" />
            <span>It acts with your own rights: owners-only actions (mute, the morning report) stay owners-only, and it never changes your data or pipelines.
              Press <kbd className="rounded bg-surface-3 px-1 font-mono text-[0.625rem]">/</kbd> to type, or use the mic.</span>
          </p>
        </aside>
      </div>
    </div>
  );
}
