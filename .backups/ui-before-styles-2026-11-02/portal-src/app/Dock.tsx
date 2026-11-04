// The dock: every page, the project switcher, search and your account, floating at the bottom like the macOS dock.
//
// Magnification: icons swell under the pointer on a smooth bell curve; the point under the pointer stays put while the
// neighbours spread out and the glass behind them stretches. One requestAnimationFrame loop writes transforms straight to
// the DOM (no React render per frame), springs are frame-rate independent, so it stays fluid at 60, 120 or 144 Hz.
// Clicking an icon bounces it and asks the shell to open the page with a genie effect out of that icon.
// Rearranging: drag any app sideways (or Alt+←/→ on a focused one); Find and Account stay pinned at the end.
import { Check, ChevronDown, ChevronLeft, ChevronRight, Compass, LayoutGrid, LogOut, Maximize2, Palette, PanelBottom, PanelLeft, RefreshCw } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useNavigate } from "react-router-dom";
import { BotAvatar, type AvatarMood } from "@/components/BotAvatar";
import { BrandLockup } from "@/components/BrandMark";
import { openBot } from "@/components/assistant/BotPanel";
import { ThemePanel } from "@/components/ThemePicker";
import { openTour } from "@/components/tour/Tour";
import { toggleFullscreen } from "@/lib/fullscreen";
import { useAppearance } from "@/lib/theme";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { useAuth } from "@/lib/auth";
import { useNow } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import type { ProjectCard } from "@/lib/types";
import { lookAgain } from "./actions";
import { GLOBAL_NAV, NAV_GROUPS, PROJECT_NAV, type NavGroup } from "./nav";
import { serviceIcon, shortName, STATUS_COLOR, STATUS_TEXT } from "./projects";
import { useShell } from "./shell";

const MAX = 1.32;    // the biggest an icon gets (gentle: neighbours barely move, so the dock stays calm)
const RANGE = 120;   // how far from the pointer icons still grow (px)
const SPRING = 16;   // how quickly sizes follow the pointer (per second)
const SLIDE = 26;    // how quickly neighbours make room for a dragged icon (per second)
const DRAG_START = 6; // px of movement before a press becomes a drag

type Group = "apps";
type Order = { items?: string[]; global?: string[]; project?: string[] };

/** Items in the person's saved order; anything new keeps its default place at the end. */
function ordered<T extends { id: string }>(items: T[], saved?: string[]): T[] {
  if (!saved?.length) return items;
  const rank = new Map(saved.map((id, i) => [id, i]));
  return [...items].sort((a, b) => (rank.get(a.id) ?? 999 + items.indexOf(a)) - (rank.get(b.id) ?? 999 + items.indexOf(b)));
}

/** A project's dot: red/amber while something needs attention, blue while everything open is being handled, else its health. */
function dotColor(p: ProjectCard | undefined) {
  if (!p) return "var(--ink-3)";
  if (p.open_incidents && !(p.needs_attention ?? p.open_incidents)) return "var(--accent)";
  return STATUS_COLOR[(p.needs_attention ? p.attention_severity : null) ?? p.status] ?? "var(--ink-3)";
}

export async function saveDockOrder(order: Order) {
  try { await api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ dock: order }) }); } catch { /* kept in this session anyway */ }
}

type Badge = { n: number; tone: "critical" | "warning" };
type Slot =
  | { kind: "sep"; id: string }
  | { kind: "item"; id: string; label: string; to?: string; onClick?: () => void; active?: boolean; badge?: Badge;
      tile: ReactNode; menu?: (trigger: ReactNode, i: number) => ReactNode; group?: Group;
      /** a cluster: one icon that branches out to these pages */
      members?: ItemSlot[]; name?: string; hint?: string };
type ItemSlot = Extract<Slot, { kind: "item" }>;

const memberGroup = new Map(NAV_GROUPS.flatMap((g) => g.members.map((m) => [m, g] as const)));
const hintOf = new Map([...GLOBAL_NAV, ...PROJECT_NAV].map((n) => [n.id, n.hint]));
const labelOf = new Map([...GLOBAL_NAV, ...PROJECT_NAV].map((n) => [n.id, n.label]));

/** Pages of the same group become one icon (in the place of the group's first page); a group with one page left stays a page. */
function cluster(items: ItemSlot[], group: (g: NavGroup, members: ItemSlot[]) => ItemSlot): { slots: ItemSlot[]; slotOf: Map<string, string> } {
  const byId = new Map(items.map((s) => [s.id, s]));
  const slots: ItemSlot[] = [], slotOf = new Map<string, string>(), done = new Set<string>();
  for (const s of items) {
    const g = memberGroup.get(s.id);
    if (!g) { slots.push(s); continue; }
    if (done.has(g.id)) continue;
    done.add(g.id);
    const members = g.members.map((id) => byId.get(id)).filter((m): m is ItemSlot => !!m);
    const slot = members.length > 1 ? group(g, members) : members[0]!;
    slots.push(slot);
    for (const id of [g.id, `group:${g.id}`, ...g.members]) slotOf.set(id, slot.id);
  }
  return { slots, slotOf };
}

/** Badges of a cluster's pages, added up; critical wins. */
function sumBadges(members: ItemSlot[]): Badge | undefined {
  const bs = members.map((m) => m.badge).filter((b): b is Badge => !!b);
  return bs.length ? { n: bs.reduce((a, b) => a + b.n, 0), tone: bs.some((b) => b.tone === "critical") ? "critical" : "warning" } : undefined;
}

/** Ask the shell to open the next page out of this icon (the genie effect). */
export const GENIE_EVENT = "alertbot:genie";

export const Dock = memo(function Dock({ pathname, mode, onModeChange }: { pathname: string; mode: "dock" | "side"; onModeChange: (mode: "dock" | "side") => void }) {
  useNow();
  const nav = useNavigate();
  const { me, feed, project, pipelines, reload } = useShell();
  const { signOut } = useAuth();

  const active = feed?.active ?? [];
  const critical = active.filter((a) => a.severity === "critical" && a.status === "open").length;  // acknowledged = being handled
  const failing = pipelines?.failing ?? 0;
  const fresh = active.filter((a) => a.status === "open").length;
  const mood: AvatarMood = !me.projects.length ? "idle" : critical || failing ? "critical" : fresh ? "warning" : "ok";
  const PIcon = serviceIcon(project);
  const initials = me.user.name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
  const role = me.user.admin ? "Admin" : me.projects.some((p) => p.role === "owner") ? "Project owner" : "Viewer";
  const [order, setOrder] = useState<Order>(() => (me.prefs?.dock as Order) ?? {});
  const appearance = useAppearance();
  const [accountView, setAccountView] = useState<"main" | "theme">("main");
  useEffect(() => { if (me.prefs?.dock) setOrder(me.prefs.dock as Order); }, [me.prefs?.dock]);
  const reorder = useCallback((ids: string[]) => {
    setOrder({ items: ids });
    void saveDockOrder({ items: ids });
  }, []);

  // One pop-up open at a time (a cluster's fan, the project switcher or the account menu). It closes whenever the page
  // changes, however that happened, and side-panel sections fold back to the one holding the page you're on.
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  useEffect(() => { if (openMenu !== "account") setAccountView("main"); }, [openMenu]);  // it opens on its main list again
  const [sideOpen, setSideOpen] = useState<Record<string, boolean>>({});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { closeMenus(); setSideOpen({}); }, [pathname]);
  const openNow = useRef<string | null>(null);  // the pop-up open right now, known before React re-renders
  const setMenu = (id: string, open: boolean) => {
    if (open) openNow.current = id;
    else if (openNow.current === id) openNow.current = null;  // a late "closed" from the previous pop-up changes nothing
    setOpenMenu(openNow.current);
  };
  const closeMenus = () => { openNow.current = null; setOpenMenu(null); };
  /** Props for a dock pop-up: controlled, and not modal, so a click on another icon both closes it and works. */
  const menuProps = (id: string) => ({ open: openMenu === id, onOpenChange: (o: boolean) => setMenu(id, o), modal: false });
  /** When one pop-up closes because another opened, focus must not jump back to the first icon: that would close the new one. */
  const keepFocus = (id: string) => (e: Event) => { if (openNow.current && openNow.current !== id) e.preventDefault(); };

  const projectNav = PROJECT_NAV.filter((n) => !(n.id === "tables" && project?.service === "Logic Apps"));
  const appSlots: Slot[] = [
    ...GLOBAL_NAV.map<Slot>((n) => ({
      kind: "item", id: n.id, group: "apps", label: n.label,
      // the bot opens over the page you're on; everything else is a page
      ...(n.id === "bot" ? { onClick: () => openBot() } : { to: n.path("") }), active: n.match(pathname),
      badge: n.id === "home" && critical ? { n: critical, tone: "critical" } : n.id === "pipelines" && failing ? { n: failing, tone: "critical" }
        : n.id === "incidents" && fresh ? { n: fresh, tone: critical ? "critical" : "warning" } : undefined,
      tile: n.id === "bot" ? <BotAvatar mood={mood} className="dock-bot" /> : <n.icon className="dock-ico" aria-hidden />,
    })),
    ...(me.projects.length ? [{
      kind: "item" as const, id: "project", group: "apps" as const, label: project ? `${shortName(project)} · switch project` : "Choose a project",
      tile: <span className="relative grid size-full place-items-center"><PIcon className="dock-ico" aria-hidden /><span className="dock-status dock-status-project" style={{ background: dotColor(project) }} /></span>,
      menu: (trigger: ReactNode) => (
        <Menu {...menuProps("project")}>
          <MenuTrigger asChild>{trigger}</MenuTrigger>
          <MenuContent align={mode === "side" ? "start" : "center"} side={mode === "side" ? "right" : "top"} className="w-80" onCloseAutoFocus={keepFocus("project")}>
            <MenuLabel>Switch project</MenuLabel>
            {me.projects.map((p) => {
              const I = serviceIcon(p);
              return (
                <MenuItem key={p.key} className={cn("py-2", p.key === project?.key && "bg-accent-soft")}
                  onSelect={() => nav(`/p/${p.key}${pathname.match(/^\/p\/[^/]+(\/(?:incidents|runs|tables|report|alerts|ask))?/)?.[1] ?? ""}`)}>
                  <span className="relative flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-3 text-ink-2">
                    <I className="size-4" aria-hidden />
                    <span className="absolute -bottom-0.5 -right-0.5 size-2 rounded-full ring-2 ring-[var(--surface)]" style={{ background: dotColor(p) }} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">{shortName(p)}</span>
                    <span className="block truncate text-[0.6875rem] text-ink-3">{p.service ?? "Project"} · {p.needs_attention ? `${p.needs_attention} need${p.needs_attention === 1 ? "s" : ""} attention` : p.open_incidents ? `${p.open_incidents} being handled` : STATUS_TEXT[p.status] ?? "not checked yet"} · {p.role}</span>
                  </span>
                  {p.key === project?.key ? <Check className="size-4 shrink-0 text-accent" aria-hidden /> : null}
                </MenuItem>
              );
            })}
            <MenuSeparator />
            <MenuItem onSelect={() => void lookAgain(reload)}><RefreshCw className="size-4" /> Find new ones in Azure</MenuItem>
          </MenuContent>
        </Menu>
      ),
    }] : []),
    ...(project ? projectNav.map<Slot>((n) => ({
      kind: "item", id: n.id, group: "apps", label: n.label, to: n.path(project.key), active: n.match(pathname),
      // only what nobody has acknowledged yet: an acknowledged incident is being handled (it stays in the register until resolved)
      badge: n.id === "incidents" && (project.needs_attention ?? project.open_incidents)
        ? { n: project.needs_attention ?? project.open_incidents, tone: (project.attention_severity ?? project.status) === "critical" ? "critical" : "warning" } : undefined,
      tile: <n.icon className="dock-ico" aria-hidden />,
    })) : []),
  ];
  const legacyOrder = [
    ...ordered(GLOBAL_NAV, order.global).map((n) => n.id),
    ...(me.projects.length ? ["project"] : []),
    ...(project ? ordered(projectNav, order.project).map((n) => n.id) : []),
  ];
  const { slots: clustered, slotOf } = cluster(appSlots as ItemSlot[], (g, members) => {
    const current = members.find((m) => m.active);
    const face = current?.tile ?? (members[0]!.id === "bot" ? members[0]!.tile : <g.icon className="dock-ico" aria-hidden />);
    return {
      kind: "item", id: `group:${g.id}`, group: "apps", members, name: g.label, hint: g.hint,
      label: current ? `${g.label} · ${labelOf.get(current.id) ?? current.label}` : `${g.label} · ${members.length} pages`,
      active: !!current, badge: sumBadges(members),
      tile: (
        <span className="dock-cluster">
          {face}
          <span className="dock-stack" aria-hidden>{members.map((m) => <i key={m.id} className={m.active ? "on" : undefined} />)}</span>
        </span>
      ),
      menu: (trigger, i) => fan(g, members, trigger, i),
    };
  });
  // a saved order may name pages that are now inside a group: they stand for the group
  const orderedApps = ordered(clustered, [...new Set((order.items ?? legacyOrder).map((id) => slotOf.get(id) ?? id))]);
  const slots: Slot[] = [
    ...orderedApps,
    { kind: "sep", id: "sep-pinned" },
    {
      kind: "item", id: "account", label: `${me.user.name} · ${role}`,
      tile: <span className="dock-avatar">{initials}</span>,
      menu: (trigger: ReactNode) => (
        <Menu {...menuProps("account")}>
          <MenuTrigger asChild>{trigger}</MenuTrigger>
          <MenuContent align={mode === "side" ? "start" : "end"} side={mode === "side" ? "right" : "top"} className={accountView === "theme" ? "w-80" : "w-72"} onCloseAutoFocus={keepFocus("account")}>
            {accountView === "theme" ? (
              <>
                <MenuItem onSelect={(e) => { e.preventDefault(); setAccountView("main"); }} className="text-ink-2"><ChevronLeft className="size-4" /> Back</MenuItem>
                <ThemePanel />
              </>
            ) : <>
            <div className="m-1 rounded-xl border border-line bg-surface-2 p-3">
              <div className="text-sm font-semibold text-ink">{me.user.name}</div>
              <div className="truncate text-[0.71875rem] text-ink-3">{me.user.email}</div>
              <div className="mt-2 text-[0.6875rem] leading-snug text-ink-2">{role} · sees {me.projects.length} project{me.projects.length === 1 ? "" : "s"}, as your Azure access allows</div>
            </div>
            <MenuItem onSelect={() => void lookAgain(reload)}><RefreshCw className="size-4" /> Find my Azure pipelines again</MenuItem>
            <MenuSeparator />
            <MenuLabel>This app</MenuLabel>
            <MenuItem onSelect={(e) => { e.preventDefault(); setAccountView("theme"); }}>
              <Palette className="size-4" /> Appearance<span className="ml-auto text-[0.6875rem] text-ink-3">{appearance.theme === "dark" ? "Dark" : "Light"}</span>
            </MenuItem>
            <MenuItem onSelect={() => onModeChange(mode === "dock" ? "side" : "dock")}>
              {mode === "dock" ? <PanelLeft className="size-4" /> : <PanelBottom className="size-4" />}{mode === "dock" ? "Menu on the side" : "Menu at the bottom (dock)"}
            </MenuItem>
            {document.fullscreenEnabled && (
              <MenuItem onSelect={toggleFullscreen}><Maximize2 className="size-4" /> {document.fullscreenElement ? "Leave full screen" : "Full screen"}</MenuItem>
            )}
            <MenuItem onSelect={openTour}><Compass className="size-4" /> Take the tour</MenuItem>
            {mode === "dock" && <MenuItem onSelect={() => { setOrder({}); void saveDockOrder({}); toast.success("Dock order reset"); }}><LayoutGrid className="size-4" /> Reset dock order</MenuItem>}
            <MenuSeparator />
            <MenuItem onSelect={signOut} className="text-[var(--critical-ink)]"><LogOut className="size-4" /> Sign out</MenuItem>
            </>}
          </MenuContent>
        </Menu>
      ),
    },
  ];

  // ── magnification ─────────────────────────────────────────────────────────
  const row = useRef<HTMLDivElement>(null);
  const bg = useRef<HTMLDivElement>(null);
  const slotEls = useRef<(HTMLElement | null)[]>([]);
  const tileEls = useRef<(HTMLElement | null)[]>([]);
  const labelEls = useRef<(HTMLElement | null)[]>([]);
  const sim = useRef({ x: null as number | null, pointerX: null as number | null, anchor: 0, s: [] as number[], rest: [] as { left: number; w: number; sep: boolean }[],
    restW: 0, base: 50, raf: 0, last: 0, enabled: true, frozen: false,
    off: [] as number[], offT: [] as number[],  // extra x offsets: a dragged icon follows the pointer, neighbours make room
    drag: null as null | { i: number; group: Group; members: number[]; x0: number; id: number; active: boolean; target: number; moved: boolean } });
  const ids = slots.map((s) => s.id).join("|");

  const measure = useCallback(() => {
    const r = row.current;
    if (!r) return;
    const st = sim.current;
    st.base = parseFloat(getComputedStyle(r).getPropertyValue("--dock-base")) || 50;
    st.rest = slotEls.current.slice(0, slots.length).map((el, i) => ({ left: el?.offsetLeft ?? 0, w: el?.offsetWidth ?? 0, sep: slots[i]?.kind === "sep" }));
    const rightEdge = Array.from(r.children).filter((el) => el !== bg.current)
      .reduce((right, el) => Math.max(right, (el as HTMLElement).offsetLeft + (el as HTMLElement).offsetWidth), 0);
    st.restW = Math.max(r.clientWidth, rightEdge + parseFloat(getComputedStyle(r).paddingRight));
    st.s = st.rest.map((_, i) => st.s[i] ?? 1);
    st.off = st.rest.map(() => 0);
    st.offT = st.rest.map(() => 0);
    st.enabled = !window.matchMedia("(prefers-reduced-motion: reduce)").matches && window.matchMedia("(min-width: 641px)").matches;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids]);

  const frame = useCallback((t: number) => {
    const st = sim.current;
    const dt = Math.min(0.05, st.last ? (t - st.last) / 1000 : 1 / 120);
    st.last = t;
    const k = 1 - Math.exp(-SPRING * dt);
    const ks = 1 - Math.exp(-SLIDE * dt);
    let moving = !!st.drag?.active;
    for (let i = 0; i < st.rest.length; i++) {  // drag offsets: the dragged icon is exact, the others spring
      const t = st.offT[i] ?? 0, c = st.off[i] ?? 0;
      if (st.drag?.active && st.drag.i === i) st.off[i] = t;
      else {
        const n = c + (t - c) * ks;
        st.off[i] = Math.abs(t - n) < 0.3 ? t : n;
        if (st.off[i] !== t) moving = true;
      }
    }
    const ax = st.x ?? st.anchor;
    const extra: number[] = [];
    for (let i = 0; i < st.rest.length; i++) {
      const r = st.rest[i]!;
      let target = 1;
      if (!r.sep && st.x !== null) {
        const d = Math.abs(st.x - (r.left + r.w / 2));
        if (d < RANGE) target = 1 + (MAX - 1) * (0.5 + 0.5 * Math.cos((Math.PI * d) / RANGE));
      }
      const cur = st.s[i] ?? 1;
      const next = cur + (target - cur) * k;
      st.s[i] = Math.abs(target - next) < 0.0005 ? target : next;
      if (st.s[i] !== target) moving = true;
      extra.push(r.sep ? 0 : st.base * (st.s[i]! - 1));
    }
    // keep the point under the pointer still: everything left of it moves left, everything right moves right
    let before = 0;
    for (let i = 0; i < st.rest.length; i++) {
      const r = st.rest[i]!;
      before += extra[i]! * Math.min(1, Math.max(0, (ax - r.left) / (r.w || 1)));
    }
    let cum = 0;
    for (let i = 0; i < st.rest.length; i++) {
      const e = extra[i]!, s = st.s[i]!;
      const slot = slotEls.current[i], tile = tileEls.current[i], label = labelEls.current[i];
      if (slot) slot.style.transform = `translate3d(${(cum + e / 2 - before + (st.off[i] ?? 0)).toFixed(2)}px,0,0)`;
      if (tile) tile.style.transform = `scale(${s.toFixed(4)})`;
      if (label) label.style.transform = `translate3d(-50%,${(-(st.base * (s - 1)) - 14).toFixed(2)}px,0)`;
      cum += e;
    }
    if (bg.current) {
      bg.current.style.transform = `translate3d(${(-before).toFixed(2)}px,0,0)`;
      bg.current.style.width = `${(st.restW + cum).toFixed(2)}px`;
    }
    st.raf = moving ? requestAnimationFrame(frame) : 0;
    if (!moving) st.last = 0;
  }, []);

  const kick = useCallback(() => { if (!sim.current.raf) sim.current.raf = requestAnimationFrame(frame); }, [frame]);

  useLayoutEffect(() => {
    measure();
    kick();
    const ro = new ResizeObserver(() => { measure(); kick(); });
    if (row.current) ro.observe(row.current);
    return () => ro.disconnect();
  }, [measure, kick]);
  useEffect(() => () => { cancelAnimationFrame(sim.current.raf); sim.current.raf = 0; sim.current.last = 0; }, []);

  // ── rearranging: press, drag sideways, drop ───────────────────────────────
  const pitch = (members: number[]) => {
    const st = sim.current;
    return members.length > 1 ? (st.rest[members[1]!]!.left - st.rest[members[0]!]!.left) : (st.rest[members[0]!]?.w ?? 50) + 8;
  };
  const onSlotDown = (e: React.PointerEvent, i: number, group?: Group) => {
    if (!group || e.button !== 0 || e.pointerType === "touch") return;
    const members = slots.map((s, k) => (s.kind === "item" && s.group === group ? k : -1)).filter((k) => k >= 0);
    sim.current.drag = { i, group, members, x0: e.clientX, id: e.pointerId, active: false, target: members.indexOf(i), moved: false };
  };
  useEffect(() => {
    const move = (e: PointerEvent) => {
      const st = sim.current, d = st.drag;
      if (!d || e.pointerId !== d.id) return;
      const dx = e.clientX - d.x0;
      if (!d.active) {
        if (Math.abs(dx) < DRAG_START) return;
        d.active = d.moved = true;
        st.x = null;  // magnification rests while rearranging
        slotEls.current[d.i]?.classList.add("is-dragging");
        row.current?.classList.add("is-rearranging");
      }
      const from = d.members.indexOf(d.i), step = pitch(d.members);
      const target = Math.max(0, Math.min(d.members.length - 1, from + Math.round(dx / step)));
      d.target = target;
      d.members.forEach((k, pos) => {
        st.offT[k] = k === d.i ? dx : pos > from && pos <= target ? -step : pos < from && pos >= target ? step : 0;
      });
      kick();
    };
    const up = (e: PointerEvent) => {
      const st = sim.current, d = st.drag;
      if (!d || e.pointerId !== d.id) return;
      st.drag = null;
      slotEls.current[d.i]?.classList.remove("is-dragging");
      row.current?.classList.remove("is-rearranging");
      if (!d.active) return;
      const from = d.members.indexOf(d.i);
      const ids = d.members.map((k) => slots[k]!.id);
      if (d.target !== from) {
        const [moved] = ids.splice(from, 1);
        ids.splice(d.target, 0, moved!);
        reorder(ids);
      }
      st.off = st.off.map(() => 0);
      st.offT = st.offT.map(() => 0);
      suppressClick.current = true;
      setTimeout(() => { suppressClick.current = false; }, 0);
      kick();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", up); };
  });
  const suppressClick = useRef(false);
  const onKeyMove = (e: React.KeyboardEvent, s: Extract<Slot, { kind: "item" }>) => {
    if (!e.altKey || !s.group || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
    e.preventDefault();
    const ids = slots.filter((x): x is Extract<Slot, { kind: "item" }> => x.kind === "item" && x.group === s.group).map((x) => x.id);
    const from = ids.indexOf(s.id), to = Math.max(0, Math.min(ids.length - 1, from + (e.key === "ArrowLeft" ? -1 : 1)));
    if (from === to) return;
    ids.splice(from, 1);
    ids.splice(to, 0, s.id);
    reorder(ids);
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-dock-id="${s.id}"]`)?.focus());
  };

  const onMove = (e: React.PointerEvent) => {
    const st = sim.current;
    if (!st.enabled || st.frozen || e.pointerType === "touch" || !row.current || st.drag?.active) return;
    st.pointerX = e.clientX;
    st.x = e.clientX - row.current.getBoundingClientRect().left + row.current.scrollLeft;
    st.anchor = st.x;
    kick();
  };
  const onScroll = () => {
    const st = sim.current, r = row.current;
    if (st.pointerX === null || !r) return;
    st.x = st.pointerX - r.getBoundingClientRect().left + r.scrollLeft;
    st.anchor = st.x;
    kick();
  };
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    const r = row.current;
    if (!r || r.scrollWidth <= r.clientWidth || Math.abs(e.deltaX) >= Math.abs(e.deltaY)) return;
    e.preventDefault();
    r.scrollLeft += e.deltaY;
  };
  const onLeave = () => { sim.current.pointerX = null; if (sim.current.frozen) return; sim.current.x = null; kick(); };
  // While a pop-up is open the icons hold still, so it doesn't slide away as the pointer moves up into it; once it
  // closes they settle back to rest.
  useEffect(() => {
    sim.current.frozen = openMenu !== null;
    if (openMenu === null) { sim.current.x = null; kick(); }
  }, [openMenu, kick]);

  // ── click: bounce, then the page opens out of the icon ──────────────────
  const launch = (i: number, s: Extract<Slot, { kind: "item" }>) => {
    closeMenus();
    const tile = tileEls.current[i];
    const inner = tile?.firstElementChild as HTMLElement | null;
    if (inner && sim.current.enabled) {
      inner.animate([{ transform: "translateY(0)" }, { transform: "translateY(-10%)", offset: 0.4 }, { transform: "translateY(0)" }],
        { duration: 320, easing: "cubic-bezier(.25,.8,.35,1)" });
    }
    if (s.to && s.to !== pathname) {
      if (tile) window.dispatchEvent(new CustomEvent(GENIE_EVENT, { detail: tile.getBoundingClientRect() }));
      nav(s.to);
    }
    s.onClick?.();
  };

  /** A page chosen from a cluster opens out of the cluster's icon. */
  const openFrom = (i: number, to?: string) => {
    if (!to || to === pathname) return;
    const tile = tileEls.current[i];
    if (tile && mode === "dock") window.dispatchEvent(new CustomEvent(GENIE_EVENT, { detail: tile.getBoundingClientRect() }));
    nav(to);
  };

  /** A cluster's pages, fanned out above its icon (beside it in the side panel). */
  const fan = (g: NavGroup, members: ItemSlot[], trigger: ReactNode, i: number) => (
    <Menu {...menuProps(g.id)}>
      <MenuTrigger asChild>{trigger}</MenuTrigger>
      <MenuContent align={mode === "side" ? "start" : "center"} side={mode === "side" ? "right" : "top"} className="dock-fan w-80" onCloseAutoFocus={keepFocus(g.id)}>
        <MenuLabel>{g.label}</MenuLabel>
        {members.map((m) => (
          <MenuItem key={m.id} onSelect={() => openFrom(i, m.to)} className={cn("dock-fan-item", m.active && "is-active")}>
            <span className="dock-fan-tile">{m.tile}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-semibold text-ink">{labelOf.get(m.id) ?? m.label}</span>
              <span className="block truncate text-[0.6875rem] text-ink-3">{hintOf.get(m.id)}</span>
            </span>
            {m.badge ? <span className={cn("dock-side-badge", m.badge.tone)}>{m.badge.n}</span>
              : m.active ? <Check className="size-4 shrink-0 text-accent" aria-label="You are here" /> : null}
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );

  // side panel: a cluster is a section that opens in place (open by default while one of its pages is showing)
  const sideGroup = (s: ItemSlot) => {
    const expanded = sideOpen[s.id] ?? !!s.active;
    return (
      <div key={s.id} className="dock-side-slot">
        <button type="button" aria-expanded={expanded} title={s.hint}
          className={cn("dock-side-item", s.active && !expanded && "is-active")}
          onClick={() => setSideOpen((o) => ({ ...o, [s.id]: !expanded }))}>
          <span className="dock-side-icon">{s.tile}</span>
          <span className="dock-side-label">{s.name}</span>
          {s.badge && !expanded && <span className={cn("dock-side-badge", s.badge.tone)}>{s.badge.n}</span>}
          <ChevronRight className={cn("dock-side-chevron dock-side-caret", expanded && "is-open")} aria-hidden />
        </button>
        {expanded && <div className="dock-side-children" role="group" aria-label={s.name}>{s.members!.map((m) => sideItem(m))}</div>}
      </div>
    );
  };

  const sideItem = (s: Extract<Slot, { kind: "item" }>) => {
    const i = slots.findIndex((item) => item.id === s.id);
    const label = s.id === "search" ? "Search" : s.id === "account" ? "Account"
      : s.id === "project" ? (project ? shortName(project) : "Choose a project") : s.label;
    const ariaLabel = s.id === "project" ? `${label}, switch project` : label;
    const trigger = (
      <button type="button" aria-label={ariaLabel} title={s.id === "project" ? "Switch project" : undefined} aria-current={s.active ? "page" : undefined}
        className={cn("dock-side-item", s.active && "is-active", s.id === "project" && "is-project")}
        onClick={s.menu ? undefined : () => launch(i, s)}>
        <span className="dock-side-icon">{s.tile}</span>
        <span className="dock-side-label">{label}</span>
        {s.badge && <span className={cn("dock-side-badge", s.badge.tone)}>{s.badge.n}</span>}
        {s.id === "project" && <ChevronDown className="dock-side-chevron" aria-hidden />}
      </button>
    );
    return <div key={s.id} className="dock-side-slot">{s.menu ? s.menu(trigger, i) : trigger}</div>;
  };

  if (mode === "side") {
    const searchSlot = slots.find((s): s is Extract<Slot, { kind: "item" }> => s.kind === "item" && s.id === "search")!;
    const accountSlot = slots.find((s): s is Extract<Slot, { kind: "item" }> => s.kind === "item" && s.id === "account")!;
    return (
      <aside className="sidebar dock-sidebar print:hidden" aria-label="Application navigation">
        <div className="sb-glow" aria-hidden />
        <div className="dock-side-content">
          <header className="dock-side-header">
            <BrandLockup size="md" tagline />

          </header>
          <div className="dock-side-section-label">YOUR WORKSPACE</div>
          <nav className="dock-side-nav" aria-label="Pages">
            {orderedApps.map((s) => (s.members ? sideGroup(s) : sideItem(s)))}
          </nav>
          <footer className="dock-side-footer">
            {sideItem(searchSlot)}
            {sideItem(accountSlot)}
          </footer>
        </div>
      </aside>
    );
  }

  return (
    <nav className="dock-wrap print:hidden" aria-label="Pages">
      <div ref={row} className="dock-row" onPointerMove={onMove} onPointerLeave={onLeave} data-tour="dock"
        onScroll={onScroll} onWheel={onWheel}
        onClickCapture={(e) => { if (suppressClick.current) { e.stopPropagation(); e.preventDefault(); } }}>
        <div ref={bg} className="dock-bg" aria-hidden />
        {slots.map((s, i) => {
          if (s.kind === "sep") return <div key={s.id} ref={(el) => { slotEls.current[i] = el; }} className="dock-sep" aria-hidden />;
          const tile = (
            <button type="button" ref={(el) => { tileEls.current[i] = el; }} aria-label={s.label} aria-current={s.active ? "page" : undefined}
              data-dock-id={s.id} onKeyDown={(e) => onKeyMove(e, s)} aria-keyshortcuts={s.group ? "Alt+ArrowLeft Alt+ArrowRight" : undefined}
              className={cn("dock-tile", s.id === "layout" && "dock-layout-switch", s.active && "is-active", s.id === "bot" && "is-bot", s.id === "account" && "is-account")}
              onClick={s.menu ? undefined : () => launch(i, s)}>
              <span className="dock-inner">{s.tile}</span>
              {s.badge && <span className={cn("dock-badge", s.badge.tone)}>{s.badge.n}</span>}
            </button>
          );
          return (
            <div key={s.id} ref={(el) => { slotEls.current[i] = el; }} className={cn("dock-slot", s.group && "is-movable")} data-tour={`dock-${s.id}`}
              onPointerDown={(e) => onSlotDown(e, i, s.group)}>
              <span ref={(el) => { labelEls.current[i] = el; }} className="dock-label" role="presentation">{s.label}</span>
              {s.menu ? s.menu(tile, i) : tile}
              {s.active && <span className="dock-dot" aria-hidden />}
            </div>
          );
        })}
      </div>
    </nav>
  );
});

/** The genie: a window that flows out of the icon (pinched at the icon, opening from the top), then the page appears. */
export function playGenie(from: DOMRect, area: HTMLElement, ghost: HTMLElement, page: HTMLElement | null) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const box = area.getBoundingClientRect();
  const W = box.width, H = box.height;
  const ix = from.left + from.width / 2 - box.left, iy = from.top - box.top, iw = from.width, ih = from.height;
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  const ease = (v: number) => v * v * (3 - 2 * v);
  const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
  const shape = (p: number) => {
    const top = lerp(iy, 0, ease(clamp(p / 0.62)));
    const bottom = lerp(iy + ih, H, ease(clamp((p - 0.28) / 0.72)));
    const left: string[] = [], right: string[] = [];
    const rows = 16;
    for (let r = 0; r <= rows; r++) {
      const y01 = r / rows;
      const f = ease(clamp((p * 1.3 - y01 * 0.55) / 0.75));
      const y = lerp(top, bottom, y01);
      const half = lerp(iw / 2, W / 2, f), cx = lerp(ix, W / 2, f);
      left.push(`${(cx - half).toFixed(1)}px ${y.toFixed(1)}px`);
      right.unshift(`${(cx + half).toFixed(1)}px ${y.toFixed(1)}px`);
    }
    return `polygon(${[...left, ...right].join(",")})`;
  };
  const steps = 24;
  const frames = Array.from({ length: steps + 1 }, (_, i) => ({ clipPath: shape(i / steps), offset: i / steps }));
  Object.assign(ghost.style, { left: `${box.left}px`, top: `${box.top}px`, width: `${W}px`, height: `${H}px`, display: "block" });
  const dur = 560;
  const g = ghost.animate(frames, { duration: dur, easing: "cubic-bezier(.3,.75,.25,1)" });
  ghost.animate([{ opacity: 1 }, { opacity: 1, offset: 0.8 }, { opacity: 0 }], { duration: dur, easing: "linear" });
  page?.animate([{ opacity: 0, transform: "translate3d(0,14px,0) scale(.985)" }, { opacity: 0, transform: "translate3d(0,14px,0) scale(.985)", offset: 0.55 },
    { opacity: 1, transform: "none" }], { duration: dur, easing: "cubic-bezier(.2,.8,.2,1)" });
  g.onfinish = () => { ghost.style.display = "none"; };
}
