// Full screen: the browser's own (Esc leaves it too). One toggle for the account menu and the bot, and the page marked
// while it's on (some layouts tighten up in full screen).
import { useEffect } from "react";
import { toast } from "sonner";

export function toggleFullscreen() {
  if (document.fullscreenElement) void document.exitFullscreen();
  else document.documentElement.requestFullscreen({ navigationUI: "hide" }).catch(() => toast.error("This browser didn't allow full screen"));
}

/** Keep <html data-fullscreen> in step with the browser, whoever turned it on or off. */
export function useFullscreenSync() {
  useEffect(() => {
    const sync = () => document.documentElement.toggleAttribute("data-fullscreen", !!document.fullscreenElement);
    sync();
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);
}
