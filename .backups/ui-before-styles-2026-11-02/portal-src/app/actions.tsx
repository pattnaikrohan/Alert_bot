// Actions available from several places (sidebar, top bar, Home, command palette).
import { toast } from "sonner";
import { post } from "@/lib/api";
import { refreshAll } from "@/lib/hooks";

/** Ask Azure again (with my own sign-in) which Data Factories, Synapse workspaces and Logic Apps I can see. */
export async function lookAgain(reload: () => void) {
  const id = toast.loading("Looking in Azure…");
  try {
    const r = await post<{ found: number; joined: string[]; removed: string[] }>("/api/azure/discover");
    toast.success(r.found ? `Found ${r.found} in Azure` : "Azure shows you nothing to watch yet", {
      id, description: r.joined.length ? `Added ${r.joined.length} project${r.joined.length > 1 ? "s" : ""}.` : r.removed.length ? `Removed ${r.removed.length} you can no longer see.` : "Your projects are up to date.",
    });
  } catch (e) {
    toast.error("Couldn't look in Azure", { id, description: (e as Error).message });
  } finally {
    reload();
  }
}

interface Source { key: string; name: string; service: string; status: "ok" | "error"; error: string | null; added: string[]; removed: string[]; count: number | null; checks?: number }
interface Db { id: number; name: string; kind: string; status: string; error: string | null; tables: number; missing: string[]; back: string[] }
export interface RefreshReport {
  azure: { status: "ok" | "error"; error?: string; found?: number; joined?: string[]; removed?: string[] } | null;
  sources: Source[]; databases: Db[]; checks: { opened: number; resolved: number; ongoing: number }; at: string;
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** One line per thing re-read, saying what changed (or that nothing did). */
function Summary({ r }: { r: RefreshReport }) {
  const lines: { tone: "ok" | "change" | "bad"; text: string }[] = [];
  if (r.azure?.status === "error") lines.push({ tone: "bad", text: `Azure sign-in: ${r.azure.error}` });
  else if (r.azure && (r.azure.joined?.length || r.azure.removed?.length))
    lines.push({ tone: "change", text: `Azure: ${[r.azure.joined?.length && `${plural(r.azure.joined.length, "new project")}`, r.azure.removed?.length && `${r.azure.removed.length} you can no longer see`].filter(Boolean).join(", ")}` });
  for (const s of r.sources) {
    if (s.status === "error") { lines.push({ tone: "bad", text: `${s.name}: couldn't read it (${s.error})` }); continue; }
    const change = [s.removed.length && `removed ${s.removed.join(", ")}`, s.added.length && `new ${s.added.join(", ")}`].filter(Boolean).join(" · ");
    lines.push({ tone: change ? "change" : "ok", text: `${s.name}: ${s.count != null ? plural(s.count, s.service === "Logic Apps" ? "Logic App" : "pipeline") : s.service}${change ? ` · ${change}` : " · no change"}` });
  }
  for (const d of r.databases) {
    if (d.status !== "ok") { lines.push({ tone: "bad", text: `${d.name}: can't connect (${d.error ?? "unknown error"})` }); continue; }
    const change = [d.missing.length && `gone: ${d.missing.join(", ")}`, d.back.length && `back: ${d.back.join(", ")}`].filter(Boolean).join(" · ");
    lines.push({ tone: change ? "change" : "ok", text: `${d.name}: connected · ${plural(d.tables, "table")}${change ? ` · ${change}` : ""}` });
  }
  const c = r.checks;
  const checks = [c.resolved && `${c.resolved} fixed`, c.opened && `${plural(c.opened, "new problem")}`, c.ongoing && `${c.ongoing} still open`].filter(Boolean).join(" · ");
  const azureQuiet = r.sources.every((s) => s.status !== "ok" || (!s.added.length && !s.removed.length));
  return (
    <div className="mt-1 space-y-1 text-[0.75rem] leading-[1.125rem]">
      {lines.map((l, i) => (
        <div key={i} className="flex gap-1.5">
          <span className="mt-[0.4rem] size-1.5 shrink-0 rounded-full" style={{ background: l.tone === "bad" ? "var(--critical)" : l.tone === "change" ? "var(--accent)" : "var(--good)" }} />
          <span className="min-w-0 [overflow-wrap:anywhere]">{l.text}</span>
        </div>
      ))}
      <div className="pt-0.5 text-ink-2">Checks: {checks || "nothing changed"}.</div>
      {azureQuiet && r.sources.some((s) => s.service !== "Snowflake feeds") && (
        <div className="pt-0.5 text-ink-3">Deleted or renamed something in ADF Studio? Azure only changes once you click <b>Publish all</b>.</div>
      )}
    </div>
  );
}

/** Re-read everything I'm connected to (Azure and each project's pipelines, every database, Snowflake feeds), then run
 * every check, and say what changed. */
export async function refreshEverything(): Promise<RefreshReport | null> {
  const id = toast.loading("Re-reading everything you're connected to…", { description: "Azure, each project's pipelines, your databases, then every check." });
  try {
    const r = await post<RefreshReport>("/api/refresh");
    const bad = r.sources.some((s) => s.status === "error") || r.databases.some((d) => d.status !== "ok") || r.azure?.status === "error";
    (bad ? toast.warning : toast.success)(bad ? "Up to date, with problems" : "Up to date with everything you're connected to",
      { id, description: <Summary r={r} />, duration: 12000 });
    return r;
  } catch (e) {
    toast.error("Couldn't refresh", { id, description: (e as Error).message });
    return null;
  } finally {
    refreshAll();
  }
}

/** Read Azure now (latest runs, the list of pipelines) and run every check, for all my projects. A pipeline that failed
 * and then succeeded after a fix shows as fixed straight away. */
export async function checkNow() {
  const id = toast.loading("Reading the latest runs from Azure…", { description: "A few seconds per project." });
  try {
    const r = await post<{ projects: { name: string; status: string; error: string | null; opened: number; resolved: number; worsened: number; ongoing: number }[] }>("/api/check-now");
    const opened = r.projects.reduce((a, p) => a + p.opened, 0);
    const resolved = r.projects.reduce((a, p) => a + p.resolved, 0);
    const ongoing = r.projects.reduce((a, p) => a + p.ongoing + p.worsened, 0);
    const failed = r.projects.filter((p) => p.status === "error");
    const parts = [resolved && `${resolved} fixed since last time`, opened && `${opened} new problem${opened > 1 ? "s" : ""}`,
      ongoing && `${ongoing} still open`].filter(Boolean);
    if (failed.length) toast.warning(`Couldn't read ${failed.length} project${failed.length > 1 ? "s" : ""}`, { id, description: failed.map((p) => `${p.name}: ${p.error}`).join("\n") });
    else toast.success(resolved && !opened ? "Up to date: things got better" : "Up to date with Azure", { id, description: parts.length ? `${parts.join(" · ")}.` : "Nothing changed. Every pipeline is as it was." });
  } catch (e) {
    toast.error("Couldn't run the checks", { id, description: (e as Error).message });
  } finally {
    refreshAll();
  }
}
