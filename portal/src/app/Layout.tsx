// App shell: a top bar (brand, where you are, live status, search, refresh, incidents, appearance), the page, and the dock
// at the bottom (every page, the project switcher, search, your account). Pages opened from the dock genie out of their
// icon. Only <main> scrolls.
import { Bell, ChevronRight, RefreshCw, Search, Sparkles } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Toaster, toast } from "sonner";
import { BrandMark, Wordmark } from "@/components/BrandMark";
import { CommandPalette } from "@/components/CommandPalette";
import { Tour, TOUR_EVENT, openTour } from "@/components/tour/Tour";
import { openBot } from "@/components/assistant/BotPanel";
import { toggleFullscreen, useFullscreenSync } from "@/lib/fullscreen";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger, Tip } from "@/components/ui/menu";
import { Skeleton } from "@/components/ui/primitives";
import { ago } from "@/lib/format";
import { api, post } from "@/lib/api";
import { refreshAll, useApi, useNow } from "@/lib/hooks";
import { applyStyle, applyTheme, STYLE_EVENT, styleChosenHere, useAppearance, type Style } from "@/lib/theme";
import { UI_EVENT } from "@/components/assistant/useAssistant";
import { BotPanel } from "@/components/assistant/BotPanel";
import type { Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { refreshEverything } from "./actions";
import { Dock, GENIE_EVENT, playGenie } from "./Dock";
import { pageOf } from "./nav";
import { serviceIcon, shortName } from "./projects";
import { ShellContext, useShell, type Feed, type PipelineBrief, type PipelineCounts, type RunEvent } from "./shell";

export { lookAgain } from "./actions";
export { useShell } from "./shell";
export const useMe = () => useShell();
export const useProject = (key: string | undefined) => useShell().me.projects.find((p) => p.key === key);

const PROJECT_KEY = "alertbot-project";
type NavLayout = "dock" | "side";
function keyFromPath(pathname: string) {
  const m = pathname.match(/^\/p\/([^/]+)/);
  return m ? decodeURIComponent(m[1]!) : null;
}

export function Layout({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const nav = useNavigate();
  const [mePoll, setMePoll] = useState(30000);
  const { data: me, error, reload } = useApi<Me>("/api/me", { refreshMs: mePoll });
  useEffect(() => { setMePoll(me?.discovery?.status === "running" ? 2500 : 30000); }, [me?.discovery?.status]);
  const { data: feed } = useApi<Feed>("/api/feed", { refreshMs: 10000 });
  useRunEvents(feed ?? null, nav);
  useReconnectDatabases(nav);
  const { data: pipes } = useApi<{ counts: PipelineCounts; items: PipelineBrief[] }>(me?.projects.length ? "/api/pipelines?days=7" : null, { refreshMs: 60000 });
  const [palette, setPalette] = useState(false);
  const [navLayout, setNavLayout] = useState<NavLayout>("dock");
  const main = useRef<HTMLElement>(null);
  useEffect(() => { if (me?.prefs?.nav_layout) setNavLayout(me.prefs.nav_layout); }, [me?.prefs?.nav_layout]);
  const changeNavLayout = useCallback((layout: NavLayout) => {
    setNavLayout(layout);
    void api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ nav_layout: layout }) }).catch(() => undefined);
  }, []);
  const pathKey = keyFromPath(loc.pathname);
  const projectKey = useMemo(() => {
    if (!me) return null;
    const stored = (() => { try { return localStorage.getItem(PROJECT_KEY); } catch { return null; } })();
    const ok = (k: string | null) => (k && me.projects.some((p) => p.key === k) ? k : null);
    return ok(pathKey) ?? ok(stored) ?? me.projects[0]?.key ?? null;
  }, [me, pathKey]);
  useEffect(() => {
    if (pathKey) try { localStorage.setItem(PROJECT_KEY, pathKey); } catch { /* ignore */ }
  }, [pathKey]);
  useEffect(() => { main.current?.scrollTo({ top: 0 }); }, [loc.pathname]);
  // the genie: a page opened from the dock flows out of the icon that was clicked
  const genieFrom = useRef<DOMRect | null>(null);
  const ghost = useRef<HTMLDivElement>(null);
  const page = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const on = (e: Event) => { genieFrom.current = (e as CustomEvent<DOMRect>).detail; };
    window.addEventListener(GENIE_EVENT, on);
    return () => window.removeEventListener(GENIE_EVENT, on);
  }, []);
  useLayoutEffect(() => {
    const from = genieFrom.current;
    genieFrom.current = null;
    if (from && main.current && ghost.current) playGenie(from, main.current, ghost.current, page.current);
  }, [loc.pathname]);
  const openPalette = useCallback(() => setPalette(true), []);
  useSearchKey(openPalette);
  useFullscreenSync();
  // the bot changes the app when asked ("dark mode", "move the menu to the side", "start the tour", "full screen")
  useEffect(() => {
    const onUi = (e: Event) => {
      const { do: what, value } = (e as CustomEvent<{ do: string; value: string }>).detail ?? {};
      if (what === "theme" && (value === "dark" || value === "light")) applyTheme(value);
      else if (what === "style" && (value === "modern" || value === "classic")) applyStyle(value);
      else if (what === "layout" && (value === "dock" || value === "side")) changeNavLayout(value);
      else if (what === "tour") openTour();
      else if (what === "search") openPalette();
      else if (what === "fullscreen" && (value === "off") === !!document.fullscreenElement) toggleFullscreen();
    };
    window.addEventListener(UI_EVENT, onUi);
    return () => window.removeEventListener(UI_EVENT, onUi);
  }, [changeNavLayout, openPalette]);
  // the tour: on someone's first sign-in, and from the top bar's Tour button
  const [tour, setTour] = useState(false);
  useEffect(() => {
    const on = () => setTour(true);
    window.addEventListener(TOUR_EVENT, on);
    return () => window.removeEventListener(TOUR_EVENT, on);
  }, []);
  const email = me?.user.email;
  const seen = me ? Boolean(me.prefs?.tour_done_at) : true;
  useEffect(() => {
    if (!email || seen) return;
    let local: string | null = null;
    try { local = localStorage.getItem(`alertbot-tour-done:${email}`) ?? sessionStorage.getItem("alertbot-tour-auto"); } catch { /* storage blocked */ }
    if (local) return;
    try { sessionStorage.setItem("alertbot-tour-auto", "1"); } catch { /* storage blocked */ }
    const t = window.setTimeout(() => setTour(true), 1100);
    return () => window.clearTimeout(t);
  }, [email, seen]);
  const closeTour = useCallback((finished: boolean) => {
    setTour(false);
    const at = new Date().toISOString();
    try { if (email) localStorage.setItem(`alertbot-tour-done:${email}`, at); } catch { /* storage blocked */ }
    void api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ tour_done_at: at, tour_version: finished ? 1 : 0 }) }).catch(() => undefined);
  }, [email]);
  const { theme, style } = useAppearance();
  // Classic is formal: always the side menu. Modern keeps the person's own choice (dock or side).
  const layout: NavLayout = style === "classic" ? "side" : navLayout;
  // The style travels with the account: a device that hasn't chosen one takes the account's; a choice made here
  // (on the sign-in page, in the menu, or by asking the bot) is saved with the account.
  useEffect(() => {
    if (!me) return;
    const saved = me.prefs?.style;
    if (!styleChosenHere() && saved) applyStyle(saved);
    else if (styleChosenHere() && saved !== document.documentElement.dataset.style) {
      void api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ style: document.documentElement.dataset.style }) }).catch(() => undefined);
    }
  }, [me]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const save = (e: Event) => { void api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ style: (e as CustomEvent<Style>).detail }) }).catch(() => undefined); };
    window.addEventListener(STYLE_EVENT, save);
    return () => window.removeEventListener(STYLE_EVENT, save);
  }, []);

  if (error && !me) {
    return <div className="flex h-screen items-center justify-center p-6"><p className="text-sm text-ink-2">Couldn't reach the API: {error.message}</p></div>;
  }
  if (!me) return <div className="flex h-screen gap-4 p-4"><Skeleton className="h-full w-64 rounded-[1.75rem]" /><Skeleton className="h-full flex-1" /></div>;

  const project = me.projects.find((p) => p.key === projectKey);
  const fullBleed = loc.pathname === "/bot";

  return (
    <ShellContext.Provider value={{ me, reload, project, feed: feed ?? null, pipelines: pipes?.counts ?? null, pipelineItems: pipes?.items ?? [], openPalette }}>
      <div className="flex h-screen overflow-hidden">
        {layout === "side" && <Dock pathname={loc.pathname} mode="side" onModeChange={changeNavLayout} />}
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <a href="#main" className="sr-only z-50 rounded bg-accent px-3 py-2 text-accent-ink focus:not-sr-only focus:fixed focus:left-3 focus:top-3">Skip to content</a>
          <TopBar pathname={loc.pathname} projectKey={projectKey} showBrand={layout === "dock"} />
          <main ref={main} id="main" tabIndex={-1} className={cn("app-main-scroll min-h-0 flex-1 outline-none", fullBleed ? "relative overflow-hidden" : "scrollbar-thin overflow-y-auto")}>
            <div className={fullBleed ? "h-full" : cn("mx-auto w-full max-w-[112rem] px-4 pt-5 sm:px-6 2xl:px-8", layout === "dock" ? "pb-36" : "pb-8")}>
              <div key={loc.pathname} ref={page} className={cn("page-in", fullBleed && "h-full")}>{children}</div>
            </div>
          </main>
        </div>
      </div>
      <div ref={ghost} className="genie-ghost" aria-hidden />
      {layout === "dock" && <Dock pathname={loc.pathname} mode="dock" onModeChange={changeNavLayout} />}
      <BotPanel />
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
      <Tour open={tour} project={projectKey} onClose={closeTour} />
      <Toaster theme={theme} position="top-right" offset={76} richColors closeButton toastOptions={{ style: { fontFamily: "var(--font-sans)" } }} />
    </ShellContext.Provider>
  );
}

/** A pipeline started, finished, failed or recovered in Azure: say so, and bring every page up to date. */
function useRunEvents(feed: Feed | null, nav: ReturnType<typeof useNavigate>) {
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!feed) return;
    const events = feed.live?.events ?? [];
    const key = (e: RunEvent) => `${e.project}/${e.run_id}/${e.status}`;
    if (seen.current === null) { seen.current = new Set(events.map(key)); return; } // what was already there isn't news
    const fresh = events.filter((e) => !seen.current!.has(key(e)));
    fresh.forEach((e) => seen.current!.add(key(e)));
    if (!fresh.length) return;
    for (const e of fresh.slice(0, 3)) {
      const what = e.status === "Succeeded" ? "succeeded" : e.status === "Failed" ? "failed" : e.status === "Cancelled" ? "was cancelled"
        : e.status === "InProgress" ? "started running" : e.status === "Queued" ? "is queued" : e.status.toLowerCase();
      const opts = { description: e.project_name, action: { label: "Open", onClick: () => nav(`/p/${e.project}/runs?pipeline=${encodeURIComponent(e.pipeline)}&run=${encodeURIComponent(e.run_id)}`) } };
      if (e.status === "Failed" || e.status === "Cancelled") toast.error(`${e.pipeline} ${what}`, opts);
      else if (e.status === "Succeeded") toast.success(`${e.pipeline} ${what}`, opts);
      else toast.message(`${e.pipeline} ${what}`, opts);
    }
    if (fresh.length > 3) toast.message(`${fresh.length - 3} more runs changed`);
    refreshAll(); // every page on screen fetches the new state now
  }, [feed, nav]);
}

/** Signing in reconnects every database this person connected before (once per browser session). */
function useReconnectDatabases(nav: ReturnType<typeof useNavigate>) {
  useEffect(() => {
    try {
      if (sessionStorage.getItem("alertbot-db-reconnected")) return;
      sessionStorage.setItem("alertbot-db-reconnected", "1");
    } catch { /* storage unavailable: reconnect anyway */ }
    post<{ connections: { name: string; status: string }[] }>("/api/databases/reconnect").then((r) => {
      const bad = r.connections.filter((c) => c.status === "error");
      if (bad.length) toast.warning(`Couldn't reconnect to ${bad.map((c) => c.name).join(", ")}`, {
        description: "Data checks on it are paused until it connects again.", action: { label: "Open", onClick: () => nav("/databases") } });
    }).catch(() => { /* not signed in yet, or offline */ });
  }, [nav]);
}

/** The search palette also opens with Ctrl/⌘ K, as in most apps (not advertised). */
function useSearchKey(openPalette: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openPalette]);
}

// ---------------------------------------------------------------- top bar
const TopBar = memo(function TopBar({ pathname, projectKey, showBrand }: { pathname: string; projectKey: string | null; showBrand: boolean }) {
  const { me, openPalette } = useShell();
  const page = pageOf(pathname) ?? (/^\/p\/[^/]+\/i\//.test(pathname) ? { label: "Incident", hint: "What happened, why, and what to do" } : undefined);
  const project = me.projects.find((p) => p.key === projectKey);
  const inProject = pathname.startsWith("/p/") && project;
  const PIcon = serviceIcon(project);
  return (
    <header className="app-header print:hidden relative z-40 mx-2 mt-2 flex h-16 shrink-0 items-center gap-3 rounded-[1.25rem] px-4 sm:mx-3 sm:mt-3 sm:px-5">
      {showBrand && <>
        <Link to="/" className="brand-lockup flex shrink-0 items-center gap-2.5" aria-label="Alert Bot home">
          <BrandMark className="size-12" />
          <span className="hidden leading-none xl:block">
            <Wordmark className="text-[1.5rem]" />
          </span>
        </Link>
        <span className="mx-1 hidden h-8 w-px bg-line md:block" aria-hidden />
      </>}

      {/* where am I: Project › Page */}
      <div className="hidden min-w-0 md:block">
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-[1.0625rem] font-bold leading-tight">
          {inProject && (
            <>
              <Link to={`/p/${project.key}`} className="flex min-w-0 items-center gap-1.5 text-ink-2 transition-colors hover:text-ink">
                <PIcon className="size-4 shrink-0 text-accent" aria-hidden /><span className="truncate">{shortName(project)}</span>
              </Link>
              <ChevronRight className="size-4 shrink-0 text-ink-3" aria-hidden />
            </>
          )}
          <h1 className="display truncate text-ink">{page?.label ?? "Alert Bot"}</h1>
        </nav>
        <p className="truncate text-[0.71875rem] text-ink-3">{page?.hint}</p>
      </div>
      {pathname !== "/" && (
        <Tip content="Opens the bot with a question about this page">
          <button type="button" onClick={() => openBot(pagePrompt(pathname))} className="hdr-ask" data-tour="ask-page">
            <Sparkles className="size-3.5" aria-hidden /><span className="hidden lg:inline">Ask about this page</span><span className="lg:hidden">Ask</span>
          </button>
        </Tip>
      )}

      <div className="ml-auto flex items-center gap-1.5 sm:gap-2">
        <button onClick={openPalette} aria-label="Search" data-tour="search"
          className="hidden h-9 w-60 items-center gap-2 rounded-xl border border-line bg-surface-2 px-3 text-[0.8125rem] text-ink-3 transition-colors hover:border-line-strong hover:text-ink lg:flex xl:w-72">
          <Search className="size-4 shrink-0" aria-hidden /><span className="truncate">Search pipelines, Logic Apps, tables…</span>
        </button>
        <button onClick={openPalette} aria-label="Search" data-tour="search-sm" className="flex size-9 items-center justify-center rounded-xl text-ink-2 hover:bg-surface-3 hover:text-ink lg:hidden">
          <Search className="size-4" />
        </button>
        <LiveIndicator />
        <div className="hdr-tools flex items-center gap-0.5 rounded-xl border border-line bg-surface-2 p-0.5" data-tour="tools">
          <RefreshButton />
          <span className="mx-0.5 hidden h-5 w-px bg-line sm:block" aria-hidden />
          <NotificationBell />
        </div>
      </div>
    </header>
  );
});

/** The question "Ask about this page" starts with: what someone on that page most likely wants to know. */
function pagePrompt(pathname: string): string {
  const inc = pathname.match(/^\/p\/[^/]+\/i\/(\d+)/);
  if (inc) return `Why did #${inc[1]} happen, and how do I fix it?`;
  const rules: [RegExp, string][] = [
    [/^\/compute/, "Why did Snowflake compute cost change yesterday?"],
    [/^\/warehouse/, "What can we safely remove to save money?"],
    [/^\/pipeline-report/, "What should I look at first in the pipeline report?"],
    [/^\/pipelines/, "Which pipelines failed this week?"],
    [/^\/lineage/, "What would a failure of the failing pipeline affect?"],
    [/^\/databases/, "Which tables are checked, and is today's data ready?"],
    [/^\/ai-usage/, "How much has the AI cost this month?"],
    [/^\/cortex/, "What did Snowflake Cortex AI cost, and who used it?"],
    [/^(\/incidents|\/p\/[^/]+\/incidents)/, "What's wrong right now?"],
    [/^(\/runs|\/p\/[^/]+\/runs)/, "Which runs failed today?"],
    [/^\/p\/[^/]+\/tables/, "Is today's data ready?"],
    [/^\/p\/[^/]+\/report/, "What's in today's morning report?"],
    [/^\/p\/[^/]+\/alerts/, "What was sent to Teams today?"],
    [/^\/p\/[^/]+$/, "How is this project doing?"],
  ];
  return rules.find(([re]) => re.test(pathname))?.[1] ?? "What's wrong right now?";
}

/** Whether the bot is watching Azure for new runs right now (it looks about every 30 seconds). */
function LiveIndicator() {
  const { feed, pipelines, me } = useShell();
  useNow();
  const live = feed?.live;
  if (!live) return null;
  const on = live.watching;
  return (
    <Tip content={on
      ? <span><b>Live</b> · watching Azure for new runs<br /><span className="text-ink-3">Last looked {ago(live.last_look_at)}. A new or finished run is checked within about 30 seconds, no refresh needed.</span></span>
      : <span><b>Not watching right now</b><br /><span className="text-ink-3">The bot's worker isn't running{live.last_look_at ? `; it last looked ${ago(live.last_look_at)}` : ""}. Refresh still reads Azure on demand.</span></span>}>
      <span className="hidden h-9 items-center gap-3 rounded-xl border border-line bg-surface-2 pl-3 pr-3.5 text-xs sm:flex" role="status" data-tour="live"
        aria-label={`${on ? "Live: watching Azure for new runs" : "Not watching for new runs"}${pipelines?.total ? `, ${plural(pipelines.total, "pipeline")} in ${plural(me.projects.length, "project")}` : ""}`}>
        <span className={cn("flex items-center gap-2 font-semibold", on ? "text-[var(--good-ink)]" : "text-ink-3")}>
          <span className={cn("relative size-2 rounded-full", on ? "sb-live bg-good" : "bg-[var(--ink-3)]")} aria-hidden />
          {on ? "Live" : "Paused"}
        </span>
      </span>
    </Tip>
  );
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Read Azure now: the latest runs of every pipeline (a run that failed and then succeeded after a fix shows as fixed),
 * the list of pipelines, and every check. */
function RefreshButton() {
  const { feed } = useShell();
  const [busy, setBusy] = useState(false);
  useNow();
  const run = async () => {
    if (busy) return;
    setBusy(true);
    try { await refreshEverything(); } finally { setBusy(false); }
  };
  return (
    <Tip content={<span><b>Refresh everything</b><br /><span className="text-ink-3">Re-reads Azure, every project's pipelines and your databases, then runs every check · last check {feed ? ago(feed.bot.last_cycle_at) : "…"}</span></span>}>
      <button onClick={() => void run()} disabled={busy} aria-busy={busy}
        className={cn("flex h-8 items-center gap-1.5 rounded-[0.625rem] px-2.5 text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:cursor-progress",
          busy && "bg-surface-3 text-accent")} aria-label={busy ? "Refreshing" : "Refresh everything"}>
        <RefreshCw className={cn("size-4", busy && "animate-spin")} aria-hidden />
        <span className="hidden text-[0.75rem] font-medium sm:inline">{busy ? "Refreshing…" : "Refresh"}</span>
      </button>
    </Tip>
  );
}

function NotificationBell() {
  const { feed } = useShell();
  const nav = useNavigate();
  const items = feed?.active ?? [];
  const [seen, setSeen] = useState<string>(() => { try { return localStorage.getItem("alertbot-seen") ?? ""; } catch { return ""; } });
  const unread = items.filter((i) => i.status === "open" && i.opened_at > seen).length;  // acknowledged: someone's on it
  return (
    <Menu onOpenChange={(o) => { if (!o) { const now = new Date().toISOString(); setSeen(now); try { localStorage.setItem("alertbot-seen", now); } catch { /* ignore */ } } }}>
      <Tip content="Open incidents">
        <MenuTrigger className="relative flex size-8 items-center justify-center rounded-[0.625rem] text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink" aria-label={`Notifications, ${unread} new`}>
          <Bell className="size-4" />
          {unread > 0 && <span className="num absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-critical px-1 text-[0.625rem] font-semibold text-white">{unread}</span>}
        </MenuTrigger>
      </Tip>
      <MenuContent className="w-96">
        <MenuLabel>Open incidents · all your projects</MenuLabel>
        {items.slice(0, 8).map((e) => (
          <MenuItem key={`${e.project}-${e.id}`} onSelect={() => nav(`/p/${e.project}/i/${e.id}`)}>
            <span className={cn("size-2 shrink-0 rounded-full", e.severity === "critical" ? "bg-critical" : "bg-warning")} />
            <div className="min-w-0">
              <div className="truncate text-[0.8125rem]">{e.title}</div>
              <div className="truncate text-[0.6875rem] text-ink-3">{e.project_name} · {e.status === "acknowledged" ? `acknowledged by ${e.acknowledged_by ?? "someone"}` : e.likely_cause}</div>
            </div>
            <span className="num ml-auto shrink-0 text-[0.625rem] text-ink-3">{ago(e.opened_at)}</span>
          </MenuItem>
        ))}
        {!items.length && <p className="px-2 py-3 text-sm text-ink-3">No open incidents. Everything the bot checks is passing.</p>}
      </MenuContent>
    </Menu>
  );
}
