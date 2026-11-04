// The Alert Bot mark: the bot itself, the same robot as on the home page. It keeps all of its own
// life at logo size: it floats on its beam, blinks, waves now and then, its beacon pings, its heartbeat runs and its
// eyes follow the pointer; pointing at the logo makes it hop. Its lights take the theme's accent.
import { useCallback, useEffect, useRef, useState } from "react";
import { RobotSVG } from "@/bot/RobotSVG";
import { cn } from "@/lib/utils";

export function BrandMark({ className }: { className?: string }) {
  const tile = useRef<HTMLSpanElement>(null);
  const [poke, setPoke] = useState(0);
  const hop = useCallback(() => setPoke((n) => n + 1), []);
  useEffect(() => {
    // hop when the pointer reaches the logo or its wordmark
    const host = tile.current?.closest(".brand-lockup") ?? tile.current;
    host?.addEventListener("pointerenter", hop);
    return () => host?.removeEventListener("pointerenter", hop);
  }, [hop]);
  return (
    <span ref={tile} className={cn("brand-mark relative block size-10 shrink-0", className)} role="img" aria-label="AlertBot">
      <span className="brand-mark-bot" aria-hidden>
        <RobotSVG mood="ok" speaking={false} pokeSignal={poke} onPoke={hop} />
      </span>
    </span>
  );
}

/** The mark with its wordmark ("Alert" + a blue "Bot", as in the logo), for the top bar and the sign-in page. */
export function BrandLockup({ className, size = "md", tagline = false }: { className?: string; size?: "md" | "lg"; tagline?: boolean }) {
  return (
    <span className={cn("brand-lockup flex items-center", size === "lg" ? "gap-2" : "gap-2.5", className)}>
      <BrandMark className={size === "lg" ? "size-24" : "size-9"} />
      <span className="leading-none">
        <Wordmark className={size === "lg" ? "text-[2.5rem]" : "text-[1.25rem]"} />
        {tagline && <span className="brand-tag mt-1 block text-[0.5625rem] font-semibold uppercase">Data reliability</span>}
      </span>
    </span>
  );
}

export const Wordmark = ({ className }: { className?: string }) => (
  <span className={cn("brand-word display block font-extrabold tracking-[-0.045em]", className)}>Alert<span className="brand-bot">Bot</span></span>
);
