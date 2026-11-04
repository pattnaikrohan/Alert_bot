// Save a conversation with the bot as a document: who said what and when, what the bot proposed and what became of it
// (confirmed and done, cancelled, failed, or never confirmed), the incidents it pointed to, and its links as full
// addresses. Two forms: a styled web document (opens anywhere, prints to PDF, opens in Word) and Markdown text.
import type { Msg, Proposal } from "./useAssistant";

export interface ChatMeta { person: string; email?: string; project?: string | null }

const STATE: Record<string, string> = {
  done: "Confirmed and done", cancelled: "Cancelled", error: "Failed", running: "Was running", pending: "Not confirmed",
};

const stamp = (at: number) => new Date(at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const clock = (at: number) => new Date(at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const full = (to: string) => (to.startsWith("http") ? to : `${window.location.origin}${to}`);
const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]!));
const actionState = (a: Proposal) => (!a.allowed ? `Not allowed${a.why_not ? `: ${a.why_not}` : ""}` : STATE[a.state ?? "pending"] ?? "Not confirmed");

/** The file name: alertbot-chat-2026-10-30-1842 */
export function fileBase(at = Date.now()): string {
  const d = new Date(at), p = (n: number) => String(n).padStart(2, "0");
  return `alertbot-chat-${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

export function toMarkdown(msgs: Msg[], meta: ChatMeta): string {
  const out = [`# AlertBot conversation`, "",
    `- **Person:** ${meta.person}${meta.email ? ` (${meta.email})` : ""}`,
    ...(meta.project ? [`- **Project in view:** ${meta.project}`] : []),
    `- **Started:** ${msgs[0] ? stamp(msgs[0].at) : "–"}`,
    `- **Saved:** ${stamp(Date.now())}`,
    `- **Messages:** ${msgs.length}`, "", "---", ""];
  for (const m of msgs) {
    if (m.role === "user") { out.push(`**You** · ${clock(m.at)}`, "", `> ${m.text.replace(/\n/g, "\n> ")}`, ""); continue; }
    out.push(`**AlertBot** · ${clock(m.at)}`, "");
    if (m.error) out.push(`_Couldn't answer: ${m.error}_`, "");
    const r = m.reply;
    if (r?.text) out.push(r.text, "");
    for (const i of r?.incidents ?? []) out.push(`- Incident #${i.id} (${i.severity}): ${i.title} · ${i.project_name}`);
    if (r?.incidents?.length) out.push("");
    for (const a of r?.actions ?? []) {
      out.push(`- **Proposed: ${a.title}** · ${actionState(a)}`);
      for (const d of a.detail) out.push(`  - ${d}`);
      if (a.result) out.push(`  - Result: ${a.result}`);
    }
    if (r?.actions?.length) out.push("");
    const links = [...(r?.links ?? []), ...(r?.navigate ? [{ label: "Opened", to: r.navigate }] : [])];
    for (const l of links) out.push(`- ${l.label}: ${full(l.to)}`);
    if (links.length) out.push("");
  }
  out.push("---", "", `_Saved from AlertBot on ${stamp(Date.now())}. Changes were only made where an action says "Confirmed and done"._`, "");
  return out.join("\n");
}

export function toHtml(msgs: Msg[], meta: ChatMeta): string {
  const rows = msgs.map((m) => {
    if (m.role === "user") {
      return `<div class="msg you"><div class="who">You <span>${clock(m.at)}</span></div><div class="bubble">${esc(m.text).replace(/\n/g, "<br>")}</div></div>`;
    }
    const r = m.reply;
    const parts: string[] = [];
    if (m.error) parts.push(`<p class="err">Couldn't answer: ${esc(m.error)}</p>`);
    if (r?.text) parts.push(`<p>${esc(r.text).replace(/\n/g, "<br>")}</p>`);
    if (r?.incidents?.length) {
      parts.push(`<ul class="incs">${r.incidents.map((i) => `<li><b class="sev ${esc(i.severity)}">${esc(i.severity)}</b> #${i.id} ${esc(i.title)} <span>· ${esc(i.project_name)}</span></li>`).join("")}</ul>`);
    }
    for (const a of r?.actions ?? []) {
      const st = !a.allowed ? "blocked" : a.state ?? "pending";
      parts.push(`<div class="action ${st}"><div class="atitle">Proposed: ${esc(a.title)} <em>${esc(actionState(a))}</em></div>${
        a.detail.length ? `<ul>${a.detail.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}${a.result ? `<p class="result">${esc(a.result)}</p>` : ""}</div>`);
    }
    const links = [...(r?.links ?? []), ...(r?.navigate ? [{ label: "Opened", to: r.navigate }] : [])];
    if (links.length) parts.push(`<p class="links">${links.map((l) => `<a href="${esc(full(l.to))}">${esc(l.label)}</a>`).join(" · ")}</p>`);
    return `<div class="msg bot"><div class="who">AlertBot <span>${clock(m.at)}</span></div><div class="bubble">${parts.join("")}</div></div>`;
  }).join("\n");
  const title = `AlertBot conversation · ${msgs[0] ? stamp(msgs[0].at) : stamp(Date.now())}`;
  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(title)}</title>
<style>
  body { margin: 0; background: #f4f6f9; color: #18222e; font: 15px/1.55 "Segoe UI", system-ui, -apple-system, Arial, sans-serif; }
  .page { max-width: 820px; margin: 32px auto; padding: 32px 40px; background: #fff; border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  h1 { margin: 0 0 4px; font-size: 22px; } .sub { margin: 0 0 18px; color: #5b6777; font-size: 13px; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0 0 22px; padding: 12px 16px; background: #f4f7fb; border-radius: 10px; font-size: 13px; }
  dt { color: #5b6777; } dd { margin: 0; font-weight: 600; }
  .msg { margin: 14px 0; } .who { font-size: 12px; font-weight: 700; color: #1f4e78; margin-bottom: 4px; } .who span { font-weight: 400; color: #8a95a3; margin-left: 6px; }
  .you .who { text-align: right; color: #5b6777; } .you .bubble { margin-left: auto; max-width: 75%; background: #1f6fb2; color: #fff; }
  .bubble { padding: 10px 14px; border-radius: 12px; background: #eef2f7; } .bubble p { margin: 0 0 6px; } .bubble p:last-child { margin: 0; }
  .incs { margin: 6px 0; padding-left: 18px; font-size: 13px; } .incs span { color: #5b6777; }
  .sev { text-transform: capitalize; font-size: 11px; padding: 1px 6px; border-radius: 999px; background: #e7ebf0; } .sev.critical { background: #fbe1e1; color: #a12828; } .sev.warning { background: #fff1d6; color: #8a5a00; }
  .action { margin: 8px 0; padding: 8px 12px; border-left: 4px solid #9aa7b6; background: #fff; border-radius: 8px; font-size: 13px; }
  .action.done { border-color: #2e8b57; } .action.cancelled, .action.pending { border-color: #b0b8c4; } .action.error, .action.blocked { border-color: #c0392b; }
  .atitle { font-weight: 600; } .atitle em { font-style: normal; font-weight: 600; font-size: 11px; margin-left: 6px; padding: 1px 8px; border-radius: 999px; background: #eef2f7; color: #3c4855; }
  .action.done .atitle em { background: #e3f3e9; color: #1f6b3f; } .action ul { margin: 4px 0; padding-left: 18px; } .result { margin: 4px 0 0; color: #1f6b3f; }
  .err { color: #a12828; } .links { font-size: 13px; } a { color: #1f6fb2; }
  .foot { margin-top: 26px; padding-top: 12px; border-top: 1px solid #e2e7ee; color: #5b6777; font-size: 12px; }
  @media print { body { background: #fff; } .page { box-shadow: none; margin: 0; padding: 0; } }
</style></head>
<body><div class="page">
<h1>AlertBot conversation</h1>
<p class="sub">A record of what was asked, what the bot answered and proposed, and what was actually done.</p>
<dl><dt>Person</dt><dd>${esc(meta.person)}${meta.email ? ` (${esc(meta.email)})` : ""}</dd>${meta.project ? `<dt>Project in view</dt><dd>${esc(meta.project)}</dd>` : ""}
<dt>Started</dt><dd>${msgs[0] ? stamp(msgs[0].at) : "–"}</dd><dt>Saved</dt><dd>${stamp(Date.now())}</dd><dt>Messages</dt><dd>${msgs.length}</dd></dl>
${rows}
<p class="foot">Saved from AlertBot. Changes were only made where an action says “Confirmed and done”.</p>
</div></body></html>`;
}

/** Hand the document to the browser as a download. */
export function download(name: string, body: string, type: string) {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}
