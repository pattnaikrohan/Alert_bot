// The bot, opened from anywhere ("Ask the bot"): it covers the whole page below the top bar, so a conversation has the
// full height and nothing behind it scrolls. Save the conversation as a document, clear it, or close (✕ or Esc).
// Going to another page from the chat closes it; the conversation is kept for the browser session until cleared.
import { Download, FileCode2, FileText, RotateCcw, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { toast } from "sonner";
import { useShell } from "@/app/shell";
import { BotAvatar } from "@/components/BotAvatar";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger } from "@/components/ui/menu";
import { AssistantChat } from "./Assistant";
import { download, fileBase, toHtml, toMarkdown } from "./chatExport";
import { ExampleCards, ExampleStrip, useExampleGroups } from "./examples";
import { UI_EVENT, type Msg } from "./useAssistant";

const OPEN_BOT = "alertbot:open-bot";

/** Open the bot over the page, optionally saying something to it straight away. */
export const openBot = (prompt?: string) => window.dispatchEvent(new CustomEvent(OPEN_BOT, { detail: { prompt } }));

export function BotPanel() {
  const { me, project } = useShell();
  const loc = useLocation();
  const groups = useExampleGroups();
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState<{ text: string; n: number } | null>(null);
  const [box, setBox] = useState<{ top: number; left: number; width: number } | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  const close = useCallback(() => {
    setOpen(false);
    setPrompt(null);  // a prompt already said must not be said again when the panel next opens
    window.setTimeout(() => opener.current?.focus?.(), 0);
  }, []);

  useEffect(() => {
    const onOpen = (e: Event) => {
      const text = (e as CustomEvent<{ prompt?: string }>).detail?.prompt;
      opener.current = document.activeElement as HTMLElement | null;
      setOpen(true);
      setPrompt((p) => (text ? { text, n: (p?.n ?? 0) + 1 } : null));
    };
    window.addEventListener(OPEN_BOT, onOpen);
    return () => window.removeEventListener(OPEN_BOT, onOpen);
  }, []);

  // another page (asked of the bot, or a link in its answer): show that page
  const path = useRef(loc.pathname);
  useEffect(() => { if (path.current !== loc.pathname) { path.current = loc.pathname; setOpen(false); setPrompt(null); } }, [loc.pathname]);

  // the tour and search need the page: step aside for them
  useEffect(() => {
    const onUi = (e: Event) => { const d = (e as CustomEvent<{ do: string }>).detail; if (d?.do === "tour" || d?.do === "search") close(); };
    window.addEventListener(UI_EVENT, onUi);
    return () => window.removeEventListener(UI_EVENT, onUi);
  }, [close]);

  // cover exactly the page area under the top bar (beside the side menu, when it's on the side)
  useLayoutEffect(() => {
    if (!open) return;
    const main = document.getElementById("main");
    const measure = () => { const r = main?.getBoundingClientRect(); if (r) setBox({ top: r.top, left: r.left, width: r.width }); };
    measure();
    const ro = new ResizeObserver(measure);
    if (main) ro.observe(main);
    window.addEventListener("resize", measure);
    return () => { ro.disconnect(); window.removeEventListener("resize", measure); };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented && !document.querySelector("[role=menu]")) { e.preventDefault(); close(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!open || !box) return null;
  const first = me.user.name.split(" ")[0];
  const say = (text: string) => setPrompt((p) => ({ text, n: (p?.n ?? 0) + 1 }));

  return (
    <div role="dialog" aria-modal="true" aria-label="AlertBot assistant" className="bot-panel" style={{ top: box.top, left: box.left, width: box.width }}>
      <div className="bot-panel-inner">
        <AssistantChat project={project?.key ?? null} prompt={prompt} starters={[]} autoFocus className="bot-panel-chat"
          empty={<ExampleCards groups={groups} onSay={say} title={`Hi ${first}, what would you like to do?`} subtitle="Type below, or pick something to try." steps />}
          tray={<ExampleStrip groups={groups} onSay={say} />}
          toolbar={(c) => <PanelBar {...c} person={me.user.name} email={me.user.email} project={project?.display_name ?? null} onClose={close} />} />
      </div>
    </div>
  );
}

function PanelBar({ msgs, clear, busy, person, email, project, onClose }: {
  msgs: Msg[]; clear: () => void; busy: boolean; person: string; email: string; project: string | null; onClose: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  useEffect(() => { if (!msgs.length) setConfirming(false); }, [msgs.length]);
  const save = (kind: "html" | "md") => {
    const meta = { person, email, project };
    const name = `${fileBase()}.${kind}`;
    if (kind === "html") download(name, toHtml(msgs, meta), "text/html;charset=utf-8");
    else download(name, toMarkdown(msgs, meta), "text/markdown;charset=utf-8");
    toast.success(`Saved the conversation as ${name}`, { description: "It's in your Downloads folder." });
  };
  return (
    <div className="bot-panel-bar">
      <div className="flex min-w-0 items-center gap-3">
        <BotAvatar mood="ok" className="size-9 shrink-0" />
        <div className="min-w-0">
          <h2 className="display text-[1rem] font-semibold leading-tight text-ink">AlertBot assistant</h2>
          <p className="truncate text-[0.71875rem] text-ink-3">{msgs.length ? `${msgs.length} message${msgs.length === 1 ? "" : "s"} in this conversation` : "Runs the whole app, in plain words"}</p>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {confirming ? (
          <span className="flex items-center gap-1.5 rounded-xl bg-[var(--warning-soft)] py-1 pl-3 pr-1 text-[0.8125rem] text-ink" role="alertdialog" aria-label="Clear this conversation?">
            Clear this conversation?
            <Button size="sm" variant="primary" onClick={() => { clear(); setConfirming(false); toast.message("Conversation cleared"); }}>Clear</Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Keep</Button>
          </span>
        ) : (
          <>
            <Menu>
              <MenuTrigger asChild>
                <Button size="sm" variant="secondary" disabled={!msgs.length || busy} title={msgs.length ? "Save this conversation as a document" : "Nothing to save yet"}>
                  <Download />Save
                </Button>
              </MenuTrigger>
              <MenuContent align="end" className="w-72">
                <MenuLabel>Save this conversation</MenuLabel>
                <MenuItem onSelect={() => save("html")}>
                  <FileText className="size-4 text-accent" />
                  <span><span className="block">As a document (.html)</span><span className="block text-[0.6875rem] text-ink-3">Opens in any browser or Word; print to PDF</span></span>
                </MenuItem>
                <MenuItem onSelect={() => save("md")}>
                  <FileCode2 className="size-4 text-accent" />
                  <span><span className="block">As text (.md)</span><span className="block text-[0.6875rem] text-ink-3">Markdown: for wikis, tickets and notes</span></span>
                </MenuItem>
              </MenuContent>
            </Menu>
            <Button size="sm" variant="ghost" disabled={!msgs.length || busy} onClick={() => setConfirming(true)} title="Start a new conversation">
              <RotateCcw />Clear chat
            </Button>
          </>
        )}
        <Button size="icon-sm" variant="ghost" onClick={onClose} aria-label="Close the assistant (Esc)" title="Close (Esc)"><X /></Button>
      </div>
    </div>
  );
}
