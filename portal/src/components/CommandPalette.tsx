// Search: jump anywhere or do anything by typing. Pages, projects, open incidents, every pipeline and Logic App (and the
// Logic Apps and Function Apps they call), the tables of the project in view, and actions.
import {
  ArrowRight, Database, History, LogOut, Moon, Play, RefreshCw, Search, Siren, Sun, Workflow, Zap,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { lookAgain, refreshEverything } from "@/app/actions";
import { GLOBAL_NAV, PROJECT_NAV } from "@/app/nav";
import { useShell } from "@/app/shell";
import { serviceIcon } from "@/app/projects";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { applyTheme, useAppearance } from "@/lib/theme";
import { cn } from "@/lib/utils";

interface Entry { id: string; group: string; label: string; sub?: string; icon: LucideIcon; words?: string; tone?: "critical" | "warning"; color?: string; run: () => void }
interface PipelineItem {
  name: string; project: string; project_name: string; service: string; type: string; state: string; folder: string | null;
  connections?: { kind: string; name: string }[];
}
interface TableItem { table: string; pipeline: string; env: string | null; inserted?: number }

const RECENT = "alertbot-recent-jumps";
const GROUPS = ["Recent", "Go to", "Projects", "Open incidents", "Pipelines and Logic Apps", "Tables", "Actions"];

function readRecent(): { id: string; label: string; sub?: string; to: string }[] {
  try { return JSON.parse(localStorage.getItem(RECENT) ?? "[]"); } catch { return []; }
}
function remember(e: { id: string; label: string; sub?: string; to: string }) {
  try { localStorage.setItem(RECENT, JSON.stringify([e, ...readRecent().filter((r) => r.id !== e.id)].slice(0, 5))); } catch { /* ignore */ }
}

/** Every typed word must start a word in the entry (so "ba id" finds BA_ID, not "sidebar"); 4+ letters may match inside a word. */
function score(e: Entry, q: string) {
  if (!q) return 1;
  const hay = `${e.label} ${e.sub ?? ""} ${e.words ?? ""}`.toLowerCase();
  const words = hay.split(/[^a-z0-9]+/).filter(Boolean);
  const tokens = q.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  if (!tokens.length) return 0;
  const hit = (t: string) => words.some((w) => w.startsWith(t)) || (t.length >= 4 && hay.includes(t));
  if (!tokens.every(hit)) return 0;
  const l = e.label.toLowerCase(), whole = q.toLowerCase().trim();
  return l === whole || l.replace(/[^a-z0-9]+/g, " ").trim() === tokens.join(" ") ? 4 : l.startsWith(whole) ? 3 : l.includes(whole) ? 2 : 1;
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const { me, project, feed, reload } = useShell();
  const { signOut } = useAuth();
  const { theme } = useAppearance();
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const [pipes, setPipes] = useState<PipelineItem[] | null>(null);
  const [tables, setTables] = useState<TableItem[] | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const before = useRef<HTMLElement | null>(null);
  const listId = useId();

  // load what can be searched when the palette opens
  useEffect(() => {
    if (!open) return;
    before.current = document.activeElement as HTMLElement | null;
    setQ("");
    setAt(0);
    window.setTimeout(() => input.current?.focus(), 0);
    void api<{ items: PipelineItem[] }>("/api/pipelines?days=7").then((r) => setPipes(r.items), () => setPipes([]));
    if (project?.kind === "adf" && project.service !== "Logic Apps") {
      void api<{ tables: TableItem[] }>(`/api/projects/${project.key}/loads?days=7`).then((r) => setTables(r.tables), () => setTables([]));
    } else setTables([]);
    return () => before.current?.focus?.();
  }, [open, project?.key, project?.kind, project?.service]);

  const go = (to: string, id: string, label: string, sub?: string) => () => { remember({ id, label, sub, to }); onClose(); nav(to); };

  const entries = useMemo<Entry[]>(() => {
    const out: Entry[] = [];
    for (const n of GLOBAL_NAV) out.push({ id: `page:${n.id}`, group: "Go to", label: n.label, sub: n.hint, icon: n.icon, words: n.words, color: n.id, run: go(n.path(""), `page:${n.id}`, n.label) });
    if (project) {
      for (const n of PROJECT_NAV) {
        if (n.id === "tables" && project.service === "Logic Apps") continue;
        out.push({ id: `page:${n.id}`, group: "Go to", label: n.label, sub: `${project.display_name} · ${n.hint}`, icon: n.icon, words: n.words, color: n.id, run: go(n.path(project.key), `page:${project.key}:${n.id}`, n.label, project.display_name) });
      }
    }
    for (const p of me.projects) {
      out.push({ id: `project:${p.key}`, group: "Projects", label: p.display_name, sub: `${p.service ?? "Project"} · ${p.open_incidents ? `${p.open_incidents} open incident${p.open_incidents > 1 ? "s" : ""}` : "no open incidents"}`,
        icon: serviceIcon(p), words: `${p.key} project`, tone: p.status === "critical" ? "critical" : p.status === "warning" ? "warning" : undefined, run: go(`/p/${p.key}`, `project:${p.key}`, p.display_name, "Project overview") });
      for (const n of PROJECT_NAV) {
        if (p.key === project?.key) continue;
        out.push({ id: `pp:${p.key}:${n.id}`, group: "Go to", label: `${n.label} · ${p.display_name}`, sub: n.hint, icon: n.icon, words: n.words, color: n.id, run: go(n.path(p.key), `pp:${p.key}:${n.id}`, n.label, p.display_name) });
      }
    }
    for (const i of feed?.active ?? []) {
      out.push({ id: `inc:${i.project}:${i.id}`, group: "Open incidents", label: i.title, sub: `${i.project_name} · ${(i.likely_cause || "").split("\n")[0]}`, icon: Siren,
        tone: i.severity === "critical" ? "critical" : "warning", words: "incident problem", run: go(`/p/${i.project}/i/${i.id}`, `inc:${i.project}:${i.id}`, i.title, i.project_name) });
    }
    for (const p of pipes ?? []) {
      const calls = (p.connections ?? []).filter((c) => c.kind === "logic_app" || c.kind === "function_app").map((c) => c.name);
      out.push({ id: `pl:${p.project}:${p.name}`, group: "Pipelines and Logic Apps", label: p.name,
        sub: `${p.project_name} · ${p.service}${p.folder ? ` · ${p.folder}` : ""}${calls.length ? ` · calls ${calls.join(", ")}` : ""}`,
        icon: p.type === "logic_app" ? Zap : Workflow, tone: p.state === "failing" ? "critical" : undefined,
        words: `${p.type === "logic_app" ? "logic app workflow" : "pipeline"} ${p.state} ${calls.join(" ")} function app`,
        run: go(`/p/${p.project}/runs?pipeline=${encodeURIComponent(p.name)}`, `pl:${p.project}:${p.name}`, p.name, `Runs · ${p.project_name}`) });
    }
    for (const t of tables ?? []) {
      const short = t.table.split(".").pop()!;
      out.push({ id: `tb:${project?.key}:${t.table}:${t.pipeline}`, group: "Tables", label: short, sub: `${t.table}${t.env ? ` · ${t.env}` : ""} · loaded by ${t.pipeline}`, icon: Database, words: `table ${t.table}`,
        run: go(`/p/${project!.key}/tables?table=${encodeURIComponent(t.table)}&pipeline=${encodeURIComponent(t.pipeline)}`, `tb:${t.table}`, short, project?.display_name) });
    }
    const act = (id: string, label: string, sub: string, icon: LucideIcon, run: () => void, words = ""): Entry => ({ id: `act:${id}`, group: "Actions", label, sub, icon, words, run: () => { onClose(); run(); } });
    out.push(
      act("check", "Refresh everything", "Re-read Azure, pipelines and databases, then run every check", Play, () => void refreshEverything(), "refresh run check now sync azure database"),
      act("azure", "Find my Azure pipelines again", "Look in Azure with your sign-in for new or removed resources", RefreshCw, () => void lookAgain(reload), "discover sync"),
      act("theme", theme === "dark" ? "Switch to light theme" : "Switch to dark theme", "Change the look; shades are in the top bar", theme === "dark" ? Sun : Moon, () => applyTheme(theme === "dark" ? "light" : "dark"), "appearance mode colour color"),
      act("signout", "Sign out", me.user.email, LogOut, signOut, "log out logout"),
    );
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, project, feed, pipes, tables, theme]);

  const results = useMemo(() => {
    const query = q.trim();
    if (!query) {
      const recent = readRecent().map<Entry>((r) => ({ id: `recent:${r.id}`, group: "Recent", label: r.label, sub: r.sub, icon: History, run: go(r.to, r.id, r.label, r.sub) }));
      const suggested = entries.filter((e) => (e.group === "Go to" && !e.id.startsWith("pp:")) || e.group === "Open incidents" || e.group === "Actions");
      return [...recent, ...suggested];
    }
    const scored = entries.map((e) => ({ e, s: score(e, query) })).filter((x) => x.s > 0);
    const out: Entry[] = [];
    for (const g of GROUPS) {
      out.push(...scored.filter((x) => x.e.group === g).sort((a, b) => b.s - a.s).slice(0, g === "Go to" ? 6 : 8).map((x) => x.e));
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entries, q]);

  useEffect(() => { setAt(0); }, [q]);
  useEffect(() => {
    list.current?.querySelector(`[data-index="${at}"]`)?.scrollIntoView({ block: "nearest" });
  }, [at]);

  if (!open) return null;

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); return onClose(); }
    if (e.key === "ArrowDown") { e.preventDefault(); setAt((i) => Math.min(results.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setAt((i) => Math.max(0, i - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); results[at]?.run(); }
  };

  let lastGroup = "";
  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center px-4 pt-[12vh]" onKeyDown={onKey}>
      <div className="anim-fade absolute inset-0 bg-[var(--scrim)] backdrop-blur-[2px]" onClick={onClose} aria-hidden />
      <div role="dialog" aria-modal="true" aria-label="Search"
        className="anim-pop glass relative flex max-h-[70vh] w-full max-w-[40rem] flex-col overflow-hidden rounded-2xl border border-line-strong shadow-float">
        <div className="flex items-center gap-3 border-b border-line px-4">
          <Search className="size-5 shrink-0 text-ink-3" aria-hidden />
          <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search pipelines, Logic Apps, tables, projects, pages…"
            className="palette-input h-14 min-w-0 flex-1 bg-transparent text-[0.9375rem] text-ink outline-none placeholder:text-ink-3"
            role="combobox" aria-expanded="true" aria-controls={listId} aria-activedescendant={results[at] ? `${listId}-${at}` : undefined} aria-autocomplete="list" />
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-xs text-ink-3 hover:bg-surface-3 hover:text-ink">Close</button>
        </div>
        <div ref={list} id={listId} role="listbox" aria-label="Results" className="scrollbar-thin min-h-0 flex-1 overflow-y-auto p-2">
          {results.length === 0 && (
            <p className="px-3 py-8 text-center text-sm text-ink-3">Nothing matches “{q}”. Try a pipeline, a Logic App, a table or a page name.</p>
          )}
          {results.map((r, i) => {
            const header = r.group !== lastGroup ? (lastGroup = r.group) : null;
            return (
              <div key={`${r.id}-${i}`}>
                {header && <p className="eyebrow px-3 pb-1 pt-3 text-ink-3 first:pt-1">{header}</p>}
                <div id={`${listId}-${i}`} role="option" aria-selected={i === at} data-index={i}
                  onMouseMove={() => setAt(i)} onClick={() => r.run()}
                  className={cn("flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5", i === at ? "bg-accent-soft" : "")}>
                  <span className={cn("flex size-8 shrink-0 items-center justify-center rounded-lg",
                    r.tone === "critical" ? "bg-[var(--critical-soft)] text-[var(--critical-ink)]" : r.tone === "warning" ? "bg-[var(--warning-soft)] text-[var(--warning-ink)]" : i === at ? "bg-accent text-accent-ink" : "bg-surface-3 text-ink-2")}>
                    <r.icon className="size-4" aria-hidden />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-ink">{r.label}</span>
                    {r.sub && <span className="block truncate text-xs text-ink-3">{r.sub}</span>}
                  </span>
                  {i === at && <ArrowRight className="size-4 shrink-0 text-ink-3" aria-hidden />}
                </div>
              </div>
            );
          })}
          {(pipes === null || tables === null) && q && <p className="px-3 py-2 text-xs text-ink-3">Loading pipelines and tables…</p>}
        </div>
      </div>
    </div>
  );
}

