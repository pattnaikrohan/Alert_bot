// Appearance: the style (modern or classic), light / dark, and a shade per theme (modern only; same sets as Team
// Tesseract). Stored on this device (and the style with the account, for other devices); public/theme-init.js applies
// it before first paint.
import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light";
/** Modern: the floating dock, gradients and motion. Classic: calm and formal, for management: a side menu, flat panels. */
export type Style = "modern" | "classic";
export const STYLES: { id: Style; label: string; hint: string }[] = [
  { id: "modern", label: "Modern", hint: "Floating dock, rich colour and motion" },
  { id: "classic", label: "Classic", hint: "Calm and formal: side menu, flat panels, for management" },
];
export type DarkShade = "ocean" | "midnight" | "champagne" | "graphite" | "emerald" | "aurora";
export type LightShade = "daylight" | "sand" | "mint" | "lavender" | "rose";
export type Shade = DarkShade | LightShade;

export const SHADES: { id: DarkShade; label: string; hint: string; swatch: [string, string] }[] = [
  { id: "ocean", label: "Ocean", hint: "Deep sea blue, sky-blue accent", swatch: ["#0f1a25", "#5ab0ff"] },
  { id: "midnight", label: "Midnight", hint: "Near-black navy, indigo accent", swatch: ["#0c1020", "#8b93ff"] },
  { id: "champagne", label: "Champagne", hint: "Warm charcoal, gold accent", swatch: ["#1a1713", "#e6c27a"] },
  { id: "graphite", label: "Graphite", hint: "Neutral graphite grey, teal accent", swatch: ["#16181b", "#4fd1c5"] },
  { id: "emerald", label: "Emerald", hint: "Deep forest green, mint accent", swatch: ["#0b1a14", "#34d399"] },
  { id: "aurora", label: "Aurora", hint: "Deep violet, orchid accent", swatch: ["#141020", "#c084fc"] },
];
export const LIGHT_SHADES: { id: LightShade; label: string; hint: string; swatch: [string, string] }[] = [
  { id: "daylight", label: "Daylight", hint: "Crisp white, blue accent", swatch: ["#f4f7fb", "#1f6fd1"] },
  { id: "sand", label: "Sand", hint: "Warm paper tones, amber accent", swatch: ["#faf6ee", "#b7791f"] },
  { id: "mint", label: "Mint", hint: "Fresh pale green, emerald accent", swatch: ["#f1f9f5", "#0f9d6e"] },
  { id: "lavender", label: "Lavender", hint: "Soft lilac, violet accent", swatch: ["#f6f4fd", "#6d4fd8"] },
  { id: "rose", label: "Rose", hint: "Blush tones, raspberry accent", swatch: ["#fdf4f6", "#d6336c"] },
];

const isLight = (s: Shade): s is LightShade => LIGHT_SHADES.some((x) => x.id === s);
const store = (k: string, v: string) => {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* storage unavailable: the choice still applies for this session */
  }
};

function read(): { theme: Theme; shade: Shade; style: Style } {
  const el = document.documentElement;
  const style: Style = el.dataset.style === "classic" ? "classic" : "modern";
  const theme: Theme = el.dataset.theme === "light" ? "light" : "dark";
  const s = el.dataset.shade as Shade | undefined;
  const shade: Shade = s && (SHADES.some((x) => x.id === s) || isLight(s)) ? s : theme === "light" ? "daylight" : "ocean";
  return { theme, shade, style };
}

let snapshot = read();
const listeners = new Set<() => void>();
const emit = () => {
  snapshot = read();
  listeners.forEach((l) => l());
};

export function applyTheme(t: Theme) {
  const el = document.documentElement;
  el.dataset.theme = t;
  store("alertbot-theme", t);
  // a shade belongs to its theme: switching theme returns to that theme's base shade
  const sh = el.dataset.shade as Shade | undefined;
  if (sh && isLight(sh) !== (t === "light")) delete el.dataset.shade;
  emit();
}

export function applyShade(shade: Shade) {
  const el = document.documentElement;
  if (shade === "ocean" || shade === "daylight") delete el.dataset.shade;
  else el.dataset.shade = shade;
  store("alertbot-shade", shade);
  const theme: Theme = isLight(shade) ? "light" : "dark";
  if (el.dataset.theme !== theme) {
    el.dataset.theme = theme;
    store("alertbot-theme", theme);
  }
  emit();
}

/** The style, everywhere at once (the page restyles in place); the shell saves it with the account. */
export function applyStyle(style: Style) {
  document.documentElement.dataset.style = style;
  store("alertbot-style", style);
  emit();
  window.dispatchEvent(new CustomEvent(STYLE_EVENT, { detail: style }));
}
/** Fired when the style changes, so the shell can save it with the account. */
export const STYLE_EVENT = "alertbot:style";
/** Whether this device has chosen a style yet (if not, the account's choice applies). */
export const styleChosenHere = () => { try { return !!localStorage.getItem("alertbot-style"); } catch { return false; } };

export function useAppearance() {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => snapshot,
  );
}
