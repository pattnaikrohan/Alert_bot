// The alert bot's head as a small live avatar (sidebar): blinking eyes whose shape follows the mood and an antenna
// beacon in the mood colour (the theme accent when calm). CSS-only animation, so it costs nothing per frame.
import { useId } from "react";
import { cn } from "@/lib/utils";

export type AvatarMood = "ok" | "warning" | "critical" | "idle";
const LIGHT: Record<AvatarMood, string> = { ok: "var(--accent)", idle: "var(--ink-3)", warning: "#ffae1f", critical: "#ff4d4d" };

export function BotAvatar({ mood, className }: { mood: AvatarMood; className?: string }) {
  const id = useId().replace(/:/g, "");
  const g = (n: string) => `${id}-${n}`;
  const light = LIGHT[mood];
  return (
    <svg viewBox="0 0 64 64" className={cn("ba shrink-0 overflow-visible", `ba-${mood}`, className)} role="img" aria-label={`Alert bot, ${mood === "ok" ? "all calm" : mood === "idle" ? "idle" : mood}`}>
      <defs>
        <radialGradient id={g("shell")} cx="38%" cy="28%" r="85%"><stop offset="0" stopColor="#ffffff" /><stop offset="0.7" stopColor="#eef2f8" /><stop offset="1" stopColor="#c9d3e3" /></radialGradient>
        <radialGradient id={g("visor")} cx="40%" cy="30%" r="80%"><stop offset="0" stopColor="#1f2a44" /><stop offset="1" stopColor="#03050a" /></radialGradient>
        <radialGradient id={g("glow")}><stop offset="0" style={{ stopColor: light, stopOpacity: 0.7 }} /><stop offset="1" style={{ stopColor: light, stopOpacity: 0 }} /></radialGradient>
        <linearGradient id={g("eye")} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#e3feff" /><stop offset="1" stopColor="#4fe6ff" /></linearGradient>
      </defs>
      {/* antenna + beacon */}
      <circle className="ba-beacon-glow" cx="32" cy="7" r="7" fill={`url(#${g("glow")})`} />
      <rect x="30.6" y="8" width="2.8" height="8" rx="1.4" fill="#141924" />
      <circle className="ba-beacon" cx="32" cy="7" r="3.2" style={{ fill: light }} />
      <circle cx="31" cy="6" r="1" fill="#fff" opacity="0.8" />
      {/* ears */}
      <ellipse cx="8.5" cy="35" rx="4" ry="8.5" fill={`url(#${g("shell")})`} />
      <ellipse cx="55.5" cy="35" rx="4" ry="8.5" fill={`url(#${g("shell")})`} />
      <ellipse cx="7.6" cy="35" rx="2" ry="5.6" fill="none" style={{ stroke: light }} strokeWidth="1.2" />
      <ellipse cx="56.4" cy="35" rx="2" ry="5.6" fill="none" style={{ stroke: light }} strokeWidth="1.2" />
      {/* head + visor */}
      <rect x="9" y="15" width="46" height="38" rx="17" fill={`url(#${g("shell")})`} />
      <rect x="13" y="20.5" width="38" height="26" rx="11.5" fill={`url(#${g("visor")})`} />
      <path d="M16 29 C16.5 24.5 21 22.5 30 22.3 C24 23.8 20 25.6 18.4 30.4 C17.8 32 16 31.6 16 29 Z" fill="#fff" opacity="0.22" />
      <g className="ba-eyes" fill="none" stroke={`url(#${g("eye")})`} strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
        {mood === "critical" ? (<><path d="M21.5 30 L26.5 33.5 L21.5 37" /><path d="M42.5 30 L37.5 33.5 L42.5 37" /></>)
          : mood === "warning" ? (<><path d="M21 34 L27.5 33.4" /><path d="M36.5 33.4 L43 34" /></>)
          : mood === "idle" ? (<><path d="M21.5 34.5 L27.5 34.5" /><path d="M36.5 34.5 L42.5 34.5" /></>)
          : (<><path d="M21 36 A3.6 3.6 0 0 1 28.2 36" /><path d="M35.8 36 A3.6 3.6 0 0 1 43 36" /></>)}
      </g>
      <rect x="9" y="15" width="46" height="38" rx="17" fill="none" style={{ stroke: light }} strokeOpacity="0.55" strokeWidth="1" />
    </svg>
  );
}
