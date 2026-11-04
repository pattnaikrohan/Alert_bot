// Lineage: where data comes from and where it goes. Files and tables on any platform → pipelines → tables → what reads
// them, across factories and projects. Hover anything to light up its upstream and downstream; click it for what a
// failure there would affect. "Show what failures reach" paints everything downstream of failing pipelines.
import {
  AlertTriangle, ArrowRight, Boxes, Building2, CheckCircle2, CircleDashed, Database, FolderOpen, Globe, Loader2, Minus, Network, OctagonAlert,
  Hand, Plus, Search, ShieldCheck, Snowflake, SquareFunction, Workflow, X, Zap, type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChartCard, Notice, PageIntro } from "@/components/Cards";
import { EnvBadge } from "@/components/TableLoads";
import { Input, Select, Skeleton } from "@/components/ui/primitives";
import { ago } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface GNode {
  id: string; kind: "pipeline" | "data" | "service"; label: string;
  // pipeline
  project?: string; project_name?: string; service?: string; type?: string; state?: string; last_run?: { at: string; status: string } | null;
  incident?: { id: number; title: string; severity: string; project?: string } | null;
  // data
  data_kind?: string; platform?: string; host?: string | null; database?: string | null; object?: string; env?: string | null; dynamic?: boolean;
  dataset?: string | null; procedure?: string | null; projects?: string[];
  staged_by?: string[]; cleaned_by?: string[]; quality_checked?: boolean;
  // service
  service_kind?: string; function?: string | null;
}
interface GEdge { from: string; to: string; kind: "reads" | "writes" | "runs" | "calls"; steps: string[]; staging?: boolean }
interface Graph { nodes: GNode[]; edges: GEdge[]; platforms: string[]; projects: { key: string; name: string }[]; summary: { pipelines: number; data: number; platforms: number; failing: string[] } }

const W = 264, H = 66, GX = 96, GY = 16, PAD = 28;

/** "adfpipelinerohan (Data Factory)" · "Data Factory" says the service twice: keep the factory name. */
const projectLabel = (n: GNode) => (n.service ? (n.project_name ?? "").replace(new RegExp(`\\s*\\(${n.service}\\)$`), "") : n.project_name ?? "");

const PLATFORM_ICON: [RegExp, LucideIcon][] = [
  [/snowflake/i, Snowflake], [/blob|lake|files|file share|sftp|ftp|s3|cloud storage/i, FolderOpen], [/rest|http|odata/i, Globe],
  [/salesforce|dynamics|dataverse|sap|sharepoint|office/i, Building2], [/databricks|spark|hive/i, Boxes],
];
const platformIcon = (p?: string): LucideIcon => PLATFORM_ICON.find(([r]) => r.test(p ?? ""))?.[1] ?? Database;

const STATE: Record<string, { label: string; color: string; icon: LucideIcon }> = {
  failing: { label: "Failing", color: "var(--critical)", icon: OctagonAlert },
  handled: { label: "Failing, being handled", color: "var(--accent)", icon: Hand },
  unstable: { label: "Failed this week, fine since", color: "var(--warning)", icon: AlertTriangle },
  running: { label: "Running", color: "var(--accent)", icon: Loader2 },
  healthy: { label: "Healthy", color: "var(--good)", icon: CheckCircle2 },
  idle: { label: "No runs this week", color: "var(--ink-3)", icon: CircleDashed },
  never_run: { label: "No runs on record", color: "var(--ink-3)", icon: CircleDashed },
  disabled: { label: "Disabled", color: "var(--ink-3)", icon: CircleDashed },
};

/** Everything reachable from `start` following edges forward (downstream) or backward (upstream). */
function reach(start: string[], adj: Map<string, string[]>): Set<string> {
  const seen = new Set<string>(), todo = [...start];
  while (todo.length) {
    const v = todo.pop()!;
    for (const w of adj.get(v) ?? []) if (!seen.has(w)) { seen.add(w); todo.push(w); }
  }
  return seen;
}

/** Left-to-right layers (longest path from the sources), ordered within a layer to keep lines short. */
function layout(nodes: GNode[], edges: GEdge[]) {
  const ids = nodes.map((n) => n.id);
  // Real factories have cycles (a step reads a table and writes it back): find the edges that close a cycle with a
  // depth-first walk and lay out the rest; those edges are still drawn (as loops).
  const out = new Map<string, GEdge[]>();
  for (const e of edges) (out.get(e.from) ?? out.set(e.from, []).get(e.from)!).push(e);
  const mark = new Map<string, 1 | 2>();
  const back = new Set<GEdge>();
  for (const root of ids) {
    if (mark.has(root)) continue;
    const stack: [string, number][] = [[root, 0]];
    mark.set(root, 1);
    while (stack.length) {
      const top = stack[stack.length - 1]!;
      const list = out.get(top[0]) ?? [];
      if (top[1] < list.length) {
        const e = list[top[1]++]!;
        const m = mark.get(e.to);
        if (m === 1) back.add(e);
        else if (m === undefined) { mark.set(e.to, 1); stack.push([e.to, 0]); }
      } else { mark.set(top[0], 2); stack.pop(); }
    }
  }
  const dag = edges.filter((e) => !back.has(e));
  // longest path from the sources, in topological order
  const indeg = new Map(ids.map((i) => [i, 0]));
  for (const e of dag) indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
  const layer = new Map(ids.map((i) => [i, 0]));
  const queue = ids.filter((i) => !indeg.get(i));
  const next = new Map<string, string[]>();
  for (const e of dag) (next.get(e.from) ?? next.set(e.from, []).get(e.from)!).push(e.to);
  while (queue.length) {
    const v = queue.shift()!;
    for (const w of next.get(v) ?? []) {
      layer.set(w, Math.max(layer.get(w)!, layer.get(v)! + 1));
      indeg.set(w, indeg.get(w)! - 1);
      if (!indeg.get(w)) queue.push(w);
    }
  }
  const used = [...new Set(layer.values())].sort((a, b) => a - b);  // no empty columns
  const dense = new Map(used.map((l, i) => [l, i]));
  const cols: string[][] = used.map(() => []);
  for (const n of nodes) cols[dense.get(layer.get(n.id)!)!]!.push(n.id);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  cols.forEach((c) => c.sort((a, b) => (byId.get(a)!.kind.localeCompare(byId.get(b)!.kind)) || byId.get(a)!.label.localeCompare(byId.get(b)!.label)));
  const parents = new Map<string, string[]>(), kids = new Map<string, string[]>();
  for (const e of edges) { (parents.get(e.to) ?? parents.set(e.to, []).get(e.to)!).push(e.from); (kids.get(e.from) ?? kids.set(e.from, []).get(e.from)!).push(e.to); }
  const pos = new Map<string, number>();
  const index = () => cols.forEach((c) => c.forEach((id, i) => pos.set(id, i)));
  index();
  const bary = (id: string, rel: Map<string, string[]>) => { const r = (rel.get(id) ?? []).filter((x) => pos.has(x)); return r.length ? r.reduce((a, x) => a + pos.get(x)!, 0) / r.length : pos.get(id)!; };
  for (let sweep = 0; sweep < 4; sweep++) {
    const rel = sweep % 2 === 0 ? parents : kids;
    const order = sweep % 2 === 0 ? cols.slice(1) : cols.slice(0, -1).reverse();
    for (const c of order) { c.sort((a, b) => bary(a, rel) - bary(b, rel)); c.forEach((id, i) => pos.set(id, i)); }
  }
  // each card level with what feeds it, packed from the top so nothing overlaps and the map starts top-left
  const at = new Map<string, { x: number; y: number }>();
  let bottom = PAD;
  cols.forEach((c, li) => {
    let next = PAD;
    for (const id of c) {
      const from = (parents.get(id) ?? []).map((x) => at.get(x)?.y).filter((y): y is number => y !== undefined);
      const want = from.length ? from.reduce((a, y) => a + y, 0) / from.length : next;
      const y = Math.max(want, next);
      at.set(id, { x: PAD + li * (W + GX), y });
      next = y + H + GY;
    }
    bottom = Math.max(bottom, next - GY);
  });
  return { at, width: PAD * 2 + cols.length * (W + GX) - GX, height: bottom + PAD, columns: cols.length };
}

function NodeCard({ n, dim, lit, affected, selected, onEnter, onLeave, onClick }: {
  n: GNode; dim: boolean; lit: boolean; affected: boolean; selected: boolean; onEnter: () => void; onLeave: () => void; onClick: () => void;
}) {
  const st = n.kind === "pipeline" ? STATE[n.state ?? "never_run"] ?? STATE.never_run! : null;
  const Icon = n.kind === "pipeline" ? (n.type === "logic_app" ? Zap : Workflow) : n.kind === "service" ? (n.service_kind === "function_app" ? SquareFunction : Zap) : platformIcon(n.platform);
  const sub = n.kind === "pipeline" ? `${projectLabel(n)}${n.service ? ` · ${n.service}` : ""}`
    : n.kind === "service" ? (n.service_kind === "function_app" ? `Function App${n.function ? ` · ${n.function}` : ""}` : "Logic App")
    : n.data_kind === "database" ? `${n.platform}${n.host ? ` · ${n.host}` : ""}${n.procedure ? ` · runs ${n.procedure}` : ""}`
    : [n.platform, n.host, n.data_kind === "files" ? null : n.object?.split(".").slice(0, -1).join(".")].filter(Boolean).join(" · ");
  return (
    <button type="button" onMouseEnter={onEnter} onMouseLeave={onLeave} onFocus={onEnter} onBlur={onLeave} onClick={onClick}
      className={cn("lin-node", `lin-${n.kind}`, dim && "is-dim", lit && "is-lit", affected && "is-affected", selected && "is-selected",
        (n.incident || n.state === "failing") && "is-bad")}
      aria-label={`${n.label}. ${sub}${st ? `. ${st.label}` : ""}`} title={`${n.kind === "data" ? n.object ?? n.label : n.label}\n${sub}`}>
      <span className="lin-icon"><Icon className="size-4" aria-hidden /></span>
      <span className="min-w-0 flex-1 text-left">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[0.8125rem] font-semibold text-ink">{n.label}</span>
          {n.env && <EnvBadge env={n.env} />}
          {n.dynamic && <span className="rounded bg-surface-3 px-1 text-[0.5625rem] font-semibold text-ink-3" title="The table or folder name is set when the pipeline runs">runtime</span>}
          {n.staged_by?.length ? <span className="shrink-0 rounded bg-surface-3 px-1 text-[0.5625rem] font-semibold text-ink-3" title={`Staging: ${n.staged_by.join(", ")} lands data here, then loads it onwards`}>staging</span> : null}
          {n.quality_checked && <ShieldCheck className="size-3.5 shrink-0 text-good" aria-label="Its data is checked" />}
        </span>
        <span className="block truncate text-[0.6875rem] text-ink-3">{sub || " "}</span>
      </span>
      {st && <span className="lin-state" style={{ background: st.color }} title={st.label} />}
      {n.incident && <span className="lin-state" style={{ background: n.incident.severity === "critical" ? "var(--critical)" : "var(--warning)" }} title={n.incident.title} />}
    </button>
  );
}

export function Lineage() {
  const { data, error, reload } = useApi<Graph>("/api/lineage", { refreshMs: 60000 });
  const [params, setParams] = useSearchParams();
  const [project, setProject] = useState("");
  const [platform, setPlatform] = useState("");
  const [q, setQ] = useState("");
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [failures, setFailures] = useState(false);
  const [zoom, setZoom] = useState(1);
  const canvas = useRef<HTMLDivElement>(null);

  // the visible graph: a project / platform filter keeps whatever is connected to what matches
  const g = useMemo(() => {
    if (!data) return null;
    let nodes = data.nodes;
    const keep = (pred: (n: GNode) => boolean) => {
      const seed = new Set(nodes.filter(pred).map((n) => n.id));
      const near = new Set(seed);
      for (const e of data.edges) { if (seed.has(e.from)) near.add(e.to); if (seed.has(e.to)) near.add(e.from); }
      nodes = nodes.filter((n) => near.has(n.id));
    };
    if (project) keep((n) => n.kind === "pipeline" && n.project === project);
    if (platform) keep((n) => n.kind === "data" && n.platform === platform);
    const ids = new Set(nodes.map((n) => n.id));
    const edges = data.edges.filter((e) => ids.has(e.from) && ids.has(e.to));
    const down = new Map<string, string[]>(), up = new Map<string, string[]>();
    for (const e of edges) { (down.get(e.from) ?? down.set(e.from, []).get(e.from)!).push(e.to); (up.get(e.to) ?? up.set(e.to, []).get(e.to)!).push(e.from); }
    return { nodes, edges, down, up, byId: new Map(nodes.map((n) => [n.id, n])), ...layout(nodes, edges) };
  }, [data, project, platform]);

  // opened with ?focus=… (from an incident): select it
  useEffect(() => {
    const f = params.get("focus");
    if (!f || !g) return;
    const hit = g.byId.get(f) ?? g.nodes.find((n) => f.startsWith("table:") && n.object?.toUpperCase() === f.slice(6).toUpperCase());
    if (hit) {
      setSelected(hit.id);
      const p = g.at.get(hit.id);
      if (p) canvas.current?.scrollTo({ left: Math.max(0, p.x * zoom - 200), top: Math.max(0, p.y * zoom - 160), behavior: "smooth" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, g]);

  const failing = useMemo(() => (g ? g.nodes.filter((n) => n.kind === "pipeline" && (n.state === "failing" || n.state === "handled")).map((n) => n.id) : []), [g]);
  const handledN = data ? data.nodes.filter((n) => n.kind === "pipeline" && n.state === "handled").length : 0;
  const impacted = useMemo(() => (g && failures ? reach(failing, g.down) : new Set<string>()), [g, failing, failures]);
  const focus = hover ?? selected;
  const chain = useMemo(() => {
    if (!g || !focus) return null;
    return { down: reach([focus], g.down), up: reach([focus], g.up) };
  }, [g, focus]);
  const matches = useMemo(() => {
    if (!g || !q.trim()) return null;
    const s = q.trim().toLowerCase();
    return new Set(g.nodes.filter((n) => `${n.label} ${n.object ?? ""} ${n.platform ?? ""} ${n.host ?? ""} ${n.project_name ?? ""}`.toLowerCase().includes(s)).map((n) => n.id));
  }, [g, q]);

  if (error && !data) return <Notice tone="warn">Couldn't load lineage: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice>;

  const sel = selected && g ? g.byId.get(selected) ?? null : null;
  const affectedTotal = g ? { tables: [...impacted].filter((i) => g.byId.get(i)?.kind === "data").length, pipelines: [...impacted].filter((i) => g.byId.get(i)?.kind === "pipeline").length } : null;

  return (
    <div>
      <PageIntro eyebrow={<><Network className="size-3.5" /> Lineage</>} title="Where your data comes from, and where it goes">
        Files and tables on any platform, the pipelines that move them, and what reads them next, across factories and projects.
        Hover anything to trace it; click it to see what a failure there would affect.
      </PageIntro>

      {data && data.summary.failing.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-2xl bg-[var(--critical-soft)] px-4 py-3 shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--critical)_30%,transparent)]">
          <OctagonAlert className="size-5 shrink-0 text-[var(--critical)]" aria-hidden />
          <p className="min-w-0 flex-1 text-sm text-ink">
            <b>{data.summary.failing.length} pipeline{data.summary.failing.length > 1 ? "s are" : " is"} failing{handledN ? ` (${handledN === data.summary.failing.length ? (handledN === 1 ? "someone is on it" : "all being handled") : `${handledN} being handled`})` : ""}.</b>{" "}
            {failures && affectedTotal ? `Downstream of them: ${affectedTotal.tables} data source${affectedTotal.tables === 1 ? "" : "s"} and ${affectedTotal.pipelines} pipeline${affectedTotal.pipelines === 1 ? "" : "s"}.` : "See everything their data reaches downstream."}
          </p>
          <button onClick={() => setFailures((f) => !f)} aria-pressed={failures}
            className={cn("rounded-xl px-3 py-1.5 text-sm font-semibold transition-colors", failures ? "bg-[var(--critical)] text-white" : "bg-surface text-ink shadow-[inset_0_0_0_1px_var(--line-strong)] hover:bg-surface-2")}>
            {failures ? "Showing what failures reach" : "Show what failures reach"}
          </button>
        </div>
      )}

      <ChartCard flush icon={Network} title="Data lineage"
        subtitle={data ? `${data.summary.pipelines} pipelines · ${data.summary.data} data sources and targets · ${data.summary.platforms} platform${data.summary.platforms === 1 ? "" : "s"}: ${data.platforms.join(", ") || "none yet"}` : undefined}
        loading={!data && !error} skeleton={<div className="px-6 pb-6"><Skeleton className="h-[28rem] w-full" /></div>}
        empty={!!data && !data.nodes.length} emptyText="No lineage yet. It appears as the bot reads your pipelines' definitions (within a few minutes of finding them)."
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a table, file, pipeline, server…" aria-label="Find in lineage" className="w-64 pl-8" />
            </div>
            {data && data.projects.length > 1 && (
              <Select value={project} onChange={(e) => setProject(e.target.value)} aria-label="Project" className="w-52">
                <option value="">All projects</option>
                {data.projects.map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}
              </Select>
            )}
            {data && data.platforms.length > 1 && (
              <Select value={platform} onChange={(e) => setPlatform(e.target.value)} aria-label="Platform" className="w-44">
                <option value="">All platforms</option>
                {data.platforms.map((p) => <option key={p}>{p}</option>)}
              </Select>
            )}
            <div className="ml-auto flex items-center gap-1 rounded-xl bg-surface-2 p-0.5 shadow-[inset_0_0_0_1px_var(--line)]">
              <button onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.1).toFixed(2)))} className="flex size-7 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-3" aria-label="Zoom out"><Minus className="size-3.5" /></button>
              <button onClick={() => setZoom(1)} className="w-12 text-center text-xs font-semibold tabular-nums text-ink-2" aria-label="Reset zoom">{Math.round(zoom * 100)}%</button>
              <button onClick={() => setZoom((z) => Math.min(1.5, +(z + 0.1).toFixed(2)))} className="flex size-7 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-3" aria-label="Zoom in"><Plus className="size-3.5" /></button>
            </div>
          </div>
        }>
        {g && (
          <div className="relative flex">
            <div ref={canvas} className="lin-canvas scrollbar-thin min-w-0 flex-1 overflow-auto" style={{ height: "min(70vh, 44rem)" }}
              onClick={(e) => { if (e.target === e.currentTarget) setSelected(null); }}>
              <div style={{ width: g.width * zoom, height: g.height * zoom }}>
                <div className="relative origin-top-left" style={{ width: g.width, height: g.height, transform: `scale(${zoom})` }}>
                  <svg className="pointer-events-none absolute inset-0" width={g.width} height={g.height} aria-hidden>
                    <defs>
                      {(["line-strong", "ink-3", "accent", "critical"] as const).map((c) => (
                        <marker key={c} id={`lin-arrow-${c}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
                          <path d="M0,0 L8,4 L0,8 z" style={{ fill: `var(--${c})` }} />
                        </marker>
                      ))}
                    </defs>
                    {g.edges.map((e) => {
                      const a = g.at.get(e.from), b = g.at.get(e.to);
                      if (!a || !b) return null;
                      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
                      const back = x2 <= x1;
                      const d = back
                        ? `M${x1},${y1} C${x1 + 60},${y1 + 90} ${x2 - 60},${y2 + 90} ${x2},${y2}`
                        : `M${x1},${y1} C${x1 + GX * 0.55},${y1} ${x2 - GX * 0.55},${y2} ${x2},${y2}`;
                      const lit = chain && ((chain.down.has(e.to) && (e.from === focus || chain.down.has(e.from))) || (chain.up.has(e.from) && (e.to === focus || chain.up.has(e.to))));
                      const hot = failures && impacted.has(e.to) && (impacted.has(e.from) || failing.includes(e.from));
                      return (
                        <path key={`${e.from}>${e.to}>${e.kind}`} d={d} className={cn("lin-edge", `k-${e.kind}`, e.staging && "k-staging", lit && "is-lit", hot && "is-hot", chain && !lit && "is-dim")}
                          markerEnd={`url(#lin-arrow-${hot ? "critical" : lit ? "accent" : e.kind === "runs" || e.kind === "calls" ? "ink-3" : "line-strong"})`} />
                      );
                    })}
                  </svg>
                  {g.nodes.map((n) => {
                    const p = g.at.get(n.id)!;
                    const lit = !!chain && (n.id === focus || chain.down.has(n.id) || chain.up.has(n.id));
                    const dim = (!!chain && !lit) || (!!matches && !matches.has(n.id));
                    return (
                      <div key={n.id} className="absolute" style={{ left: p.x, top: p.y, width: W, height: H }}>
                        <NodeCard n={n} dim={dim} lit={lit && n.id !== focus} affected={failures && impacted.has(n.id)} selected={n.id === selected}
                          onEnter={() => setHover(n.id)} onLeave={() => setHover(null)}
                          onClick={() => { setSelected(n.id === selected ? null : n.id); setParams(n.id === selected ? {} : { focus: n.id }, { replace: true }); }} />
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
            {sel && <Details n={sel} g={g} onPick={(id) => { setSelected(id); setParams({ focus: id }, { replace: true }); }} onClose={() => { setSelected(null); setParams({}, { replace: true }); }} />}
          </div>
        )}
        {g && (
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-line px-6 py-2.5 text-[0.6875rem] text-ink-3">
            <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-5 rounded bg-[var(--line-strong)]" />data moves</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-5 rounded border-t-2 border-dashed border-[var(--ink-3)]" />runs or calls</span>
            <span className="inline-flex items-center gap-1.5"><ShieldCheck className="size-3.5 text-good" />data checked by the bot</span>
            {Object.entries(STATE).slice(0, 4).map(([k, s]) => <span key={k} className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: s.color }} />{s.label}</span>)}
            <span>Hover to trace · click for impact</span>
          </div>
        )}
      </ChartCard>
    </div>
  );
}

function Details({ n, g, onPick, onClose }: {
  n: GNode; g: { byId: Map<string, GNode>; down: Map<string, string[]>; up: Map<string, string[]> }; onPick: (id: string) => void; onClose: () => void;
}) {
  // a cycle leads back to the node itself: that isn't something it affects
  const down = [...reach([n.id], g.down)].filter((i) => i !== n.id).map((i) => g.byId.get(i)!).filter(Boolean);
  const up = [...reach([n.id], g.up)].filter((i) => i !== n.id).map((i) => g.byId.get(i)!).filter(Boolean);
  const st = n.kind === "pipeline" ? STATE[n.state ?? "never_run"] ?? STATE.never_run! : null;
  const List = ({ items, empty }: { items: GNode[]; empty: string }) => items.length ? (
    <ul className="space-y-1">
      {items.slice(0, 40).map((x) => {
        const I = x.kind === "pipeline" ? (x.type === "logic_app" ? Zap : Workflow) : x.kind === "service" ? Zap : platformIcon(x.platform);
        return (
          <li key={x.id}>
            <button onClick={() => onPick(x.id)} className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-surface-3">
              <I className="size-3.5 shrink-0 text-ink-3" aria-hidden />
              <span className="min-w-0 flex-1 truncate text-ink">{x.label}</span>
              <span className="shrink-0 text-[0.625rem] text-ink-3">{x.kind === "data" ? x.platform : x.kind === "pipeline" ? "pipeline" : "service"}</span>
            </button>
          </li>
        );
      })}
    </ul>
  ) : <p className="px-2 text-xs text-ink-3">{empty}</p>;
  const downData = down.filter((x) => x.kind === "data"), downPipes = down.filter((x) => x.kind !== "data");
  return (
    <aside className="lin-details scrollbar-thin w-80 shrink-0 overflow-y-auto overflow-x-hidden border-l border-line p-4" style={{ height: "min(70vh, 44rem)" }} aria-label={`${n.label} details`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="eyebrow text-ink-3">{n.kind === "pipeline" ? (n.type === "logic_app" ? "Logic App" : "Pipeline") : n.kind === "service" ? "Called service" : n.data_kind === "files" ? "Files" : n.data_kind === "database" ? "Database" : "Table"}</p>
          <h3 className="display mt-0.5 break-words text-base font-semibold text-ink">{n.label}</h3>
        </div>
        <button onClick={onClose} className="flex size-7 shrink-0 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="Close"><X className="size-4" /></button>
      </div>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
        {n.kind === "data" && <>
          <dt className="text-ink-3">Platform</dt><dd className="text-ink">{n.platform}</dd>
          {n.host && <><dt className="text-ink-3">Server</dt><dd className="break-all font-mono text-[0.6875rem] text-ink">{n.host}</dd></>}
          {n.database && <><dt className="text-ink-3">Database</dt><dd className="text-ink">{n.database}</dd></>}
          <dt className="text-ink-3">{n.data_kind === "files" ? "Folder" : "Object"}</dt><dd className="break-all font-mono text-[0.6875rem] text-ink">{n.object}</dd>
          {n.procedure && <><dt className="text-ink-3">Runs</dt><dd className="font-mono text-[0.6875rem] text-ink">{n.procedure}</dd></>}
          {n.dynamic && <><dt className="text-ink-3">Note</dt><dd className="text-ink-2">The name is decided when the pipeline runs (a parameter).</dd></>}
          {n.staged_by?.length ? <><dt className="text-ink-3">Staging</dt><dd className="text-ink-2">{n.staged_by.join(", ")} land{n.staged_by.length === 1 ? "s" : ""} data here, then load{n.staged_by.length === 1 ? "s" : ""} it onwards.</dd></> : null}
          {n.cleaned_by?.length ? <><dt className="text-ink-3">Cleared by</dt><dd className="text-ink-2">{n.cleaned_by.join(", ")} delete{n.cleaned_by.length === 1 ? "s" : ""} the files here after loading them.</dd></> : null}
          {n.quality_checked && <><dt className="text-ink-3">Data checks</dt><dd className="flex items-center gap-1 text-ink"><ShieldCheck className="size-3.5 text-good" />Checked after every load</dd></>}
          {n.projects && n.projects.length > 0 && <><dt className="text-ink-3">Seen in</dt><dd className="text-ink">{n.projects.join(", ")}</dd></>}
        </>}
        {n.kind === "pipeline" && <>
          <dt className="text-ink-3">Project</dt><dd className="text-ink">{n.project_name}</dd>
          {(() => {
            const outs = g.down.get(n.id)?.map((i) => g.byId.get(i)).filter((x): x is GNode => !!x && x.kind === "data") ?? [];
            return outs.length ? <><dt className="text-ink-3">Loads</dt><dd className="break-words text-ink">{outs.map((x) => x.label).join(", ")}</dd></> : null;
          })()}
          {st && <><dt className="text-ink-3">State</dt><dd className="flex items-center gap-1.5 text-ink"><span className="size-2 rounded-full" style={{ background: st.color }} />{st.label}</dd></>}
          {n.last_run && <><dt className="text-ink-3">Last run</dt><dd className="text-ink">{n.last_run.status} · {ago(n.last_run.at)}</dd></>}
        </>}
      </dl>
      {n.incident && (
        <Link to={`/p/${n.incident.project ?? n.project}/i/${n.incident.id}`} className="mt-3 flex items-start gap-2 rounded-xl bg-[var(--critical-soft)] p-2.5 text-xs text-ink hover:underline">
          <OctagonAlert className="mt-0.5 size-3.5 shrink-0 text-[var(--critical)]" /><span className="min-w-0 [overflow-wrap:anywhere]">{n.incident.title}</span>
        </Link>
      )}
      {n.kind === "pipeline" && n.project && (
        <Link to={`/p/${n.project}/runs?pipeline=${encodeURIComponent(n.label)}`} className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-accent hover:underline">Open its runs <ArrowRight className="size-3" /></Link>
      )}

      <div className="mt-5 rounded-xl bg-surface-2 p-3 shadow-[inset_0_0_0_1px_var(--line)]">
        <p className="text-xs font-semibold text-ink">If this fails, it affects</p>
        <p className="mt-0.5 text-[0.6875rem] text-ink-3">{downData.length} data source{downData.length === 1 ? "" : "s"} and {downPipes.length} pipeline{downPipes.length === 1 ? "" : "s"} or services downstream</p>
        <div className="mt-2"><List items={down} empty="Nothing downstream that the bot can see." /></div>
      </div>
      <div className="mt-3 rounded-xl bg-surface-2 p-3 shadow-[inset_0_0_0_1px_var(--line)]">
        <p className="text-xs font-semibold text-ink">It depends on</p>
        <div className="mt-2"><List items={up} empty="Nothing upstream: this is where the data starts." /></div>
      </div>
    </aside>
  );
}
